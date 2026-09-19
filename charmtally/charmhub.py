"""Whether each charm is publicly listed on Charmhub.

A charm can be in the corpus — hyrum lists its repo, it builds, someone
maintains it — and still not be findable by anyone who did not already know
it existed. Charmhub calls that *unlisted*: published, installable by exact
name, absent from search and from the catalogue pages. The gap between "we
build charms" and "you can find our charms" is the thing charmtally#70 wants
tracked, and nothing in a git checkout can answer it, because the answer
lives in the store.

`https://api.charmhub.io/v2/charms/info/<name>?fields=result.unlisted` answers
it in one request per charm, so that is what this does. The alternative —
pulling the whole listed catalogue once and testing membership — looked
cheaper and is not usable: `find?q=` truncates at a few hundred results and
takes no page parameter, so a name missing from it cannot be told from a name
the endpoint simply stopped short of.

Unlike `workflows.py` and `rocks.py` this holds no disk cache, and the
difference is not an oversight. Those two fetch from inside the scan's worker
*processes*, where a cache is the only way to stop the same file being fetched
once per worker; this runs as one pass in the parent, over the distinct charm
names, after the scan has finished. There is nobody to share a fetch with. A
cache would also be actively wrong at the weekly cadence: a charm that was
listed last week and is not now is precisely the event the metric exists to
show.

Opt-in, like every other network access in this package: nothing here runs
unless `scan --charmhub` asks for it, which the weekly workflow does and a
test or a local run does not. The switch matters more here than it does for
`workflows.configure()`, which only fires for a charm whose CI actually
delegates somewhere: this fires once per charm in every scan, so a default-on
version would have put 760 requests into the test suite.
"""

from __future__ import annotations

import concurrent.futures
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

API_BASE = "https://api.charmhub.io/v2/charms/info"

#: The charm is on Charmhub and appears in search and the catalogue.
LISTED = "listed"
#: The charm is on Charmhub but hidden from search: installable only by name.
UNLISTED = "unlisted"
#: No charm of that name is registered on Charmhub at all.
ABSENT = "absent"

#: How many names to have in flight. Threads rather than processes: the work
#: is one socket read each. Modest, because the store is someone else's
#: service and 760 names is not a reason to lean on it.
_WORKERS = 8

#: Seconds between the two attempts `lookup` makes, matching `workflows`.
_RETRY_DELAY = 1.0


def _url(name: str) -> str:
    return f"{API_BASE}/{urllib.parse.quote(name, safe='')}?fields=result.unlisted"


def _get(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
    """GET `url`, returning (status, body). Raises on anything but an answer."""
    try:
        # ruff: ignore[suspicious-url-open-usage] — URL is built from API_BASE, always https.
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # The store answered, and the answer was "no such charm" or "not now".
        return exc.code, ""


def lookup(
    name: str, *, get: Callable[[str], tuple[int, str]] = _get, attempts: int = 2
) -> str | None:
    """Return `LISTED`, `UNLISTED`, `ABSENT`, or None if the store didn't say.

    None and `ABSENT` are different findings and must not be collapsed: the
    first is "we asked and got no usable answer", the second is "we asked and
    there is no such charm". A run that folded a flaky lookup into `ABSENT`
    would report a store outage as a corpus that had fallen off Charmhub.

    A 404 is final — the store knows its own names — so it is not retried.
    Anything else that fails gets one more go, for the same reason
    `workflows.fetch_text` does: a dropped connection here does not cost one
    row, it costs a charm's place in a denominator.
    """
    url = _url(name)
    for attempt in range(attempts):
        try:
            status, body = get(url)
        except (urllib.error.URLError, OSError, ValueError):
            status, body = 0, ""
        if status == 404:
            return ABSENT
        if status == 200:
            try:
                data = json.loads(body)
            except ValueError:
                return None
            result = data.get("result")
            if not isinstance(result, dict) or "unlisted" not in result:
                # A 200 that does not carry the field is not an answer about
                # listing, and reading it as `LISTED` would invent one.
                return None
            return UNLISTED if result["unlisted"] else LISTED
        if attempt < attempts - 1:
            time.sleep(_RETRY_DELAY)
    return None


def listings(
    names: Iterable[str],
    *,
    lookup_one: Callable[[str], str | None] = lookup,
    workers: int = _WORKERS,
) -> dict[str, str | None]:
    """Look up every name, concurrently. Every name gets a key, value or not.

    Key presence is the "we looked" signal every downstream reader of
    `__meta__` relies on, so a name whose lookup failed is present with None
    rather than omitted.
    """
    wanted = sorted(set(names))
    if not wanted:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(wanted, pool.map(lookup_one, wanted), strict=True))
