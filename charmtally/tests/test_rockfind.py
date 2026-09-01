"""Tests for the rockcraft.yaml corpus builder."""

from __future__ import annotations

import base64
import urllib.error

import pytest

from charmtally.tools import rockfind


class FakeClient:
    """Scripted GitHub API: search results keyed by query, repos by full name."""

    def __init__(self, *, searches=None, repos=None, contents=None):
        self.searches = searches or {}
        self.repos = repos or {}
        self.contents = contents or {}
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/search/code":
            query = params["q"]
            items = self.searches.get(query, [])
            per_page = int(params.get("per_page", rockfind.PER_PAGE))
            page = int(params.get("page", 1))
            start = (page - 1) * per_page
            return {"total_count": len(items), "items": items[start : start + per_page]}
        if path.startswith("/repos/") and "/contents/" in path:
            full, rel = path[len("/repos/") :].split("/contents/", 1)
            if (full, rel) not in self.contents:
                raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
            return {
                "encoding": "base64",
                "content": base64.b64encode(self.contents[full, rel].encode()).decode(),
            }
        if path.startswith("/repos/"):
            full = path[len("/repos/") :]
            if full not in self.repos:
                raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
            return self.repos[full]
        raise AssertionError(f"unexpected path {path}")


def item(full_name, path, *, fork=False):
    owner = full_name.split("/")[0]
    return {
        "path": path,
        "repository": {"full_name": full_name, "fork": fork, "owner": {"login": owner}},
    }


def test_size_query():
    assert rockfind._size_query("filename:x", 0, 10) == "filename:x size:0..10"
    assert rockfind._size_query("filename:x", 11, None) == "filename:x size:>=11"


def test_search_pages_paginates():
    query = "q"
    items = [item(f"o/r{i}", "rockcraft.yaml") for i in range(150)]
    client = FakeClient(searches={query: items})
    got = list(rockfind.search_pages(client, query))
    assert len(got) == 150
    assert [c[1]["page"] for c in client.calls] == [1, 2]


def test_search_pages_stops_at_cap():
    query = "q"
    items = [item(f"o/r{i}", "rockcraft.yaml") for i in range(rockfind.SEARCH_CAP + 250)]
    client = FakeClient(searches={query: items})
    assert len(list(rockfind.search_pages(client, query))) == rockfind.SEARCH_CAP


def test_search_pages_stops_at_the_offset_cap_when_pages_come_back_short():
    """A short page must not push the loop past the 1000-result window.

    ``total_count`` is an estimate and GitHub may return fewer items than
    ``per_page``, so counting items would ask for page 11 — which is a 422,
    not an empty page.
    """

    class ShortPageClient:
        """Returns 95 items a page against an over-stated total_count."""

        def __init__(self):
            self.pages = []

        def get_json(self, path, params=None):
            page = int(params["page"])
            self.pages.append(page)
            if page > rockfind.SEARCH_CAP // rockfind.PER_PAGE:
                raise urllib.error.HTTPError(path, 422, "Unprocessable Entity", None, None)
            start = (page - 1) * 95
            return {
                "total_count": 990,
                "items": [item(f"o/r{i}", "rockcraft.yaml") for i in range(start, start + 95)],
            }

    client = ShortPageClient()
    got = list(rockfind.search_pages(client, "q"))
    assert client.pages == list(range(1, rockfind.SEARCH_CAP // rockfind.PER_PAGE + 1))
    assert len(got) == 95 * len(client.pages)


def test_search_partitioned_covers_both_buckets():
    base = "path:rockcraft.yaml"
    small = rockfind._size_query(base, 0, rockfind.INITIAL_MAX_SIZE)
    big = rockfind._size_query(base, rockfind.INITIAL_MAX_SIZE + 1, None)
    client = FakeClient(
        searches={
            small: [item("o/a", "rockcraft.yaml")],
            big: [item("o/b", "rocks/b/rockcraft.yaml")],
        }
    )
    items = rockfind.search_partitioned(client, base)
    assert {i["repository"]["full_name"] for i in items} == {"o/a", "o/b"}


def test_search_partitioned_splits_over_cap():
    """A bucket over the cap is halved rather than silently truncated."""
    base = "b"
    over = [item(f"o/r{i}", "rockcraft.yaml") for i in range(rockfind.SEARCH_CAP + 1)]
    searches = {
        rockfind._size_query(base, 0, 4): over,
        rockfind._size_query(base, 0, 2): [item("o/low", "rockcraft.yaml")],
        rockfind._size_query(base, 3, 4): [item("o/high", "rockcraft.yaml")],
        rockfind._size_query(base, 5, None): [],
    }
    client = FakeClient(searches=searches)
    items = rockfind.search_partitioned(client, base, initial_max=4)
    assert {i["repository"]["full_name"] for i in items} == {"o/low", "o/high"}


def test_search_partitioned_reports_unsplittable_bucket():
    base = "b"
    over = [item(f"o/r{i}", "rockcraft.yaml") for i in range(rockfind.SEARCH_CAP + 1)]
    client = FakeClient(searches={rockfind._size_query(base, 0, 0): over, "b size:>=1": []})
    logged = []
    items = rockfind.search_partitioned(client, base, initial_max=0, log=logged.append)
    assert len(items) == rockfind.SEARCH_CAP
    assert any("cannot be split further" in line for line in logged)


def test_search_partitioned_dedupes_repeated_hits():
    base = "b"
    client = FakeClient(
        searches={
            rockfind._size_query(base, 0, 4): [item("o/a", "rockcraft.yaml")] * 2,
            rockfind._size_query(base, 5, None): [],
        }
    )
    assert len(rockfind.search_partitioned(client, base, initial_max=4)) == 1


@pytest.mark.parametrize(
    ("full_name", "path", "expected"),
    [
        ("canonical/rock", "rockcraft.yaml", "rock"),
        ("canonical/rocks", "mysql/rockcraft.yaml", "mysql"),
        ("canonical/rocks", "rocks/redis/rockcraft.yaml", "redis"),
    ],
)
def test_name_from_path(full_name, path, expected):
    assert rockfind._name_from_path(full_name, path) == expected


def test_refs_from_items():
    refs = rockfind.refs_from_items([
        item("canonical/rocks", "mysql/rockcraft.yaml"),
        {"path": "x"},
        item("", "y"),
    ])
    assert len(refs) == 1
    ref = refs[0]
    assert ref.team == "canonical"
    assert ref.name == "mysql"
    assert ref.repo_url == "https://github.com/canonical/rocks"
    assert ref.full_name == "canonical/rocks"


def test_enrich_one_request_per_repo():
    refs = rockfind.refs_from_items([
        item("o/r", "a/rockcraft.yaml"),
        item("o/r", "b/rockcraft.yaml"),
    ])
    client = FakeClient(
        repos={
            "o/r": {
                "stargazers_count": 7,
                "pushed_at": "2026-08-01T10:11:12Z",
                "archived": True,
                "fork": False,
            }
        }
    )
    out = rockfind.enrich(client, refs)
    assert [r.stars for r in out] == [7, 7]
    assert out[0].last_push == "2026-08-01"
    assert out[0].archived is True
    assert len([c for c in client.calls if c[0] == "/repos/o/r"]) == 1


def test_enrich_keeps_ref_when_lookup_fails():
    refs = rockfind.refs_from_items([item("o/gone", "rockcraft.yaml")])
    logged = []
    out = rockfind.enrich(FakeClient(), refs, log=logged.append)
    assert out == refs
    assert logged and "could not read" in logged[0]


def test_resolve_names_prefers_declared_name():
    refs = rockfind.refs_from_items([item("o/r", "src/rockcraft.yaml")])
    client = FakeClient(contents={("o/r", "src/rockcraft.yaml"): "name: my-rock\nversion: '1'\n"})
    assert rockfind.resolve_names(client, refs)[0].name == "my-rock"


def test_resolve_names_falls_back_on_bad_yaml():
    refs = rockfind.refs_from_items([item("o/r", "src/rockcraft.yaml")])
    client = FakeClient(contents={("o/r", "src/rockcraft.yaml"): "just a string"})
    assert rockfind.resolve_names(client, refs)[0].name == "src"


def test_csv_round_trip(tmp_path):
    refs = rockfind.enrich(
        FakeClient(repos={"o/r": {"stargazers_count": 3, "pushed_at": "2026-01-02T00:00:00Z"}}),
        rockfind.refs_from_items([item("o/r", "rockcraft.yaml")]),
    )
    out = tmp_path / "rocks.csv"
    assert rockfind.write(out, refs) == 1
    assert rockfind.load(out) == refs


def test_load_skips_rows_without_repository(tmp_path):
    csv_path = tmp_path / "rocks.csv"
    csv_path.write_text(
        "Team,Rock Name,Repository,Rockcraft Path\n"
        "o,a,https://github.com/o/a,rockcraft.yaml\n"
        "o,b,,rockcraft.yaml\n"
    )
    assert [r.name for r in rockfind.load(csv_path)] == ["a"]


def test_merge_preserves_curated_columns():
    existing = [
        rockfind.RockRef(
            team="charm-tech",
            name="hand-named",
            repo_url="https://github.com/o/r",
            branch="main",
            path="rockcraft.yaml",
            notes="reviewed",
        ),
        rockfind.RockRef(
            team="", name="gone", repo_url="https://github.com/o/gone", branch=None, path="r.yaml"
        ),
    ]
    found = [
        rockfind.RockRef(
            team="o",
            name="r",
            repo_url="https://github.com/o/r",
            branch=None,
            path="rockcraft.yaml",
            stars=12,
        ),
        rockfind.RockRef(
            team="o",
            name="new",
            repo_url="https://github.com/o/new",
            branch=None,
            path="rockcraft.yaml",
        ),
    ]
    merged = {ref.key: ref for ref in rockfind.merge(existing, found)}
    kept = merged["https://github.com/o/r", "rockcraft.yaml"]
    assert (kept.team, kept.notes, kept.branch) == ("charm-tech", "reviewed", "main")
    assert kept.stars == 12  # fresh data still wins for the search-derived columns
    assert kept.name == "r"
    assert ("https://github.com/o/new", "rockcraft.yaml") in merged
    assert ("https://github.com/o/gone", "r.yaml") in merged  # not deleted


def test_token_lookup(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "from-env")
    assert rockfind._token(None) == "from-env"
    assert rockfind._token("explicit") == "explicit"
    monkeypatch.delenv("GH_TOKEN")
    with pytest.raises(SystemExit):
        rockfind._token(None)


def _http_error(code, headers):
    return urllib.error.HTTPError("http://x", code, "nope", headers, None)


def test_rate_limit_delay_uses_retry_after():
    client = rockfind.GitHubClient(token="t", now=lambda: 100.0)
    assert client._rate_limit_delay(_http_error(429, {"retry-after": "30"})) == 30.0


def test_rate_limit_delay_uses_reset_header():
    client = rockfind.GitHubClient(token="t", now=lambda: 100.0)
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "160"}
    assert client._rate_limit_delay(_http_error(403, headers)) == 60.0


def test_rate_limit_delay_ignores_other_errors():
    client = rockfind.GitHubClient(token="t", now=lambda: 100.0)
    assert client._rate_limit_delay(_http_error(404, {})) is None
    assert client._rate_limit_delay(_http_error(403, {})) is None


def test_client_retries_after_rate_limit(monkeypatch):
    slept = []
    client = rockfind.GitHubClient(
        token="t", sleep_between=0, core_sleep=0, now=lambda: 0.0, sleeper=slept.append
    )
    attempts = []

    def fake_request(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise _http_error(429, {"retry-after": "5"})
        return {"ok": True}

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.get_json("/rate") == {"ok": True}
    assert slept == [5.0]


def test_client_gives_up_after_retries(monkeypatch):
    client = rockfind.GitHubClient(
        token="t",
        sleep_between=0,
        core_sleep=0,
        retries=2,
        now=lambda: 0.0,
        sleeper=lambda _s: None,
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda _url: (_ for _ in ()).throw(_http_error(429, {"retry-after": "1"})),
    )
    with pytest.raises(urllib.error.HTTPError):
        client.get_json("/rate")


def test_cmd_search_end_to_end(tmp_path, monkeypatch, capsys):
    base = rockfind.DEFAULT_QUERY
    searches = {
        rockfind._size_query(base, 0, rockfind.INITIAL_MAX_SIZE): [
            item("canonical/rocks", "mysql/rockcraft.yaml"),
            item("someone/fork-of-rocks", "rockcraft.yaml", fork=True),
        ],
        rockfind._size_query(base, rockfind.INITIAL_MAX_SIZE + 1, None): [],
    }
    repos = {
        "canonical/rocks": {"stargazers_count": 42, "pushed_at": "2026-08-20T00:00:00Z"},
        "someone/fork-of-rocks": {"fork": True, "pushed_at": "2020-01-01T00:00:00Z"},
    }
    contents = {("canonical/rocks", "mysql/rockcraft.yaml"): "name: mysql-rock\n"}
    client = FakeClient(searches=searches, repos=repos, contents=contents)
    monkeypatch.setattr(rockfind, "GitHubClient", lambda **_kwargs: client)
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    out = tmp_path / "rocks.csv"
    assert rockfind.main(["search", "--out", str(out), "--names"]) == 0

    refs = rockfind.load(out)
    assert len(refs) == 1  # the fork is dropped
    assert refs[0].name == "mysql-rock"
    assert refs[0].stars == 42
    assert refs[0].team == "canonical"
    assert "wrote 1 rows" in capsys.readouterr().err


def test_search_and_core_requests_are_paced_separately():
    """Code search is 30/min; the core API is 5000/hr — don't crawl for both."""
    slept = []
    client = rockfind.GitHubClient(
        token="t", sleep_between=2.0, core_sleep=0.1, sleeper=slept.append
    )
    calls = iter([{"total_count": 0, "items": []}, {}, {}])
    client._request = lambda _url: next(calls)
    client.get_json("/search/code", {"q": "x"})
    client.get_json("/repos/o/r")
    client.get_json("/repos/o/r2")
    assert slept == [0.1, 0.1]  # first request is unpaced


def test_search_requests_use_the_search_pace():
    slept = []
    client = rockfind.GitHubClient(
        token="t", sleep_between=2.0, core_sleep=0.1, sleeper=slept.append
    )
    client._request = lambda _url: {"total_count": 0, "items": []}
    client.get_json("/search/code", {"q": "x"})
    client.get_json("/search/code", {"q": "y"})
    assert slept == [2.0]


def test_private_repos_are_dropped_from_search_results():
    """A token that can see private code must not leak it into the corpus."""
    items = [
        item("canonical/rocks", "rockcraft.yaml"),
        {
            "path": "rockcraft.yaml",
            "repository": {
                "full_name": "canonical/secret-thing",
                "private": True,
                "owner": {"login": "canonical"},
            },
        },
    ]
    refs = rockfind.refs_from_items(items)
    assert [r.visibility for r in refs] == ["public", "private"]
    assert [r.full_name for r in rockfind.public_only(refs)] == ["canonical/rocks"]


def test_internal_repos_are_dropped_at_enrichment():
    """`private` can't tell internal from private; the repos endpoint can."""
    refs = rockfind.refs_from_items([item("bigcorp/rocks", "rockcraft.yaml")])
    client = FakeClient(repos={"bigcorp/rocks": {"visibility": "internal", "private": True}})
    assert rockfind.public_only(rockfind.enrich(client, refs)) == []


def test_unknown_visibility_is_dropped():
    """A repo we couldn't look up is not assumed public."""
    ref = rockfind.RockRef(
        team="o",
        name="r",
        repo_url="https://github.com/o/r",
        branch=None,
        path="rockcraft.yaml",
        visibility="",
    )
    assert rockfind.public_only([ref]) == []


def test_visibility_prefers_the_explicit_field():
    assert (
        rockfind._visibility({"visibility": "internal", "private": True}, "public") == "internal"
    )
    assert rockfind._visibility({"private": False}, "private") == "public"
    assert rockfind._visibility({}, "private") == "private"  # failed lookup keeps what we knew


def test_cmd_search_drops_private_repos(tmp_path, monkeypatch):
    base = rockfind.DEFAULT_QUERY
    private_item = {
        "path": "rockcraft.yaml",
        "repository": {"full_name": "o/secret", "private": True, "owner": {"login": "o"}},
    }
    searches = {
        rockfind._size_query(base, 0, rockfind.INITIAL_MAX_SIZE): [
            item("o/open", "rockcraft.yaml"),
            private_item,
            item("o/internal", "rockcraft.yaml"),
        ],
        rockfind._size_query(base, rockfind.INITIAL_MAX_SIZE + 1, None): [],
    }
    repos = {
        "o/open": {"visibility": "public", "stargazers_count": 1},
        "o/internal": {"visibility": "internal", "private": True},
    }
    client = FakeClient(searches=searches, repos=repos)
    monkeypatch.setattr(rockfind, "GitHubClient", lambda **_kwargs: client)
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    out = tmp_path / "rocks.csv"
    assert rockfind.main(["search", "--out", str(out)]) == 0
    assert [r.full_name for r in rockfind.load(out)] == ["o/open"]
    assert "secret" not in out.read_text()


def _single_result_client():
    base = rockfind.DEFAULT_QUERY
    return FakeClient(
        searches={
            rockfind._size_query(base, 0, rockfind.INITIAL_MAX_SIZE): [
                item("o/open", "rockcraft.yaml")
            ],
            rockfind._size_query(base, rockfind.INITIAL_MAX_SIZE + 1, None): [],
        },
        repos={"o/open": {"visibility": "public"}},
    )


def test_min_rows_fails_without_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rockfind, "GitHubClient", lambda **_kwargs: _single_result_client())
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    out = tmp_path / "rocks.csv"
    assert rockfind.main(["search", "--out", str(out), "--min-rows", "50"]) == 1
    assert not out.exists()  # the old CSV, if any, is left alone
    assert "expected at least 50" in capsys.readouterr().err


def test_min_rows_satisfied(tmp_path, monkeypatch):
    monkeypatch.setattr(rockfind, "GitHubClient", lambda **_kwargs: _single_result_client())
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    out = tmp_path / "rocks.csv"
    assert rockfind.main(["search", "--out", str(out), "--min-rows", "1"]) == 0
    assert len(rockfind.load(out)) == 1


def test_near_miss_filenames_are_dropped():
    """`filename:rockcraft.yaml` also matches rockcraft.yaml.j2 and friends."""
    refs = rockfind.refs_from_items([
        item("o/r", "rockcraft.yaml"),
        item("o/r", "rock/rockcraft.yaml"),
        item("o/r", "template/rockcraft.yaml.j2"),
        item("o/r", "docs/rockcraft.yaml.md"),
        item("o/r", "not-rockcraft.yaml"),
    ])
    assert [r.path for r in refs] == ["rock/rockcraft.yaml", "rockcraft.yaml"]
