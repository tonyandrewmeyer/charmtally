# Security policy

This is a small, personal project that scans publicly-available code; the
likely impact surface of a vulnerability is narrow. Even so, if you find a
security issue please report it privately rather than opening a public issue.

## Reporting a vulnerability

Preferred: open a [private vulnerability report][gh-pvr] on this repository.
GitHub will notify the maintainer and provide a private channel for triage.

Alternative: email <charmtally@aotearoa.dev>.

Please include enough detail to reproduce the issue (affected version or
commit, inputs, expected vs. observed behaviour). If you have a fix in mind,
suggestions are welcome.

## Expected response

This is a low-traffic personal project, so response times are best-effort. I
will acknowledge a valid report within a week, give a target remediation
date, and credit you in the release notes unless you ask otherwise.

## Scope

In scope: the `charmtally` Python package itself and the GitHub Actions
workflows in this repository (`.github/workflows/`).

Out of scope: vulnerabilities in upstream dependencies (please report those
to the dependency's maintainers); issues in the charms `charmtally` scans
(report those to the charm's authors); the corpus CSV's contents.

[gh-pvr]: https://github.com/tonyandrewmeyer/charmtally/security/advisories/new
