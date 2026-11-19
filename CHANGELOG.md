# Changelog

All notable changes to apimend are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `apimend fix` prints a verification report listing exactly what was checked: tests,
  every request your tests sent to the mock (and whether it matched the new spec), a re-scan
  for remaining affected code, and what was not checked (the live API).
- `--allow-unverified` keeps a fix even when the tests never called the mock API.
- Every affected line found by `scan` and `fix` has a confidence level: high (proven),
  medium (probable) or low (uncertain), with the reasons when it is not high.
- `scan` and `fix` list every Python file they could not analyse (for example, because of a
  syntax error) with the reason, instead of skipping it silently.
- Request-level analysis (`apimend.callsites`): apimend finds each request in the code, its
  HTTP method, its path and the body it sends, following variables, keys added later,
  helper functions in the same file and `json.dumps(...)`. Works with `requests`, `httpx`,
  `aiohttp`, `urllib` and your own wrapper functions.
- `--min-confidence` on `scan` hides less certain lines; on `fix` it sets which lines may be
  changed.

### Fixed

- A dictionary in the same function as a request (for example a log message) was reported,
  and could be changed by `fix`, even though it was not part of the request.
- A newly required property was considered present when any other request in the file sent
  it, so a request missing it could go unreported.
- A key added to a request body *after* the request was sent was treated as part of it, so
  `fix` could rename it and a missing required property could go unreported. Only what
  happens before the request now counts; keys added only in some cases (inside an `if`, or
  later in a retry loop) are `medium` at most.

### Changed

- A fix is now kept only when the evidence supports it. Tests that pass without calling the
  API give `UNVERIFIED`, and requests that do not match the new spec give `NOT VERIFIED`; in
  both cases the fix is undone.
- With `--no-mock`, a passing run is reported as `TESTS PASSED` instead of `VERIFIED`.
- Requests with a different HTTP method from the changed operation are no longer reported,
  and a path that no request uses is reported as `low` instead of being ignored.
- `fix` only changes lines found with high confidence by default, and lists the others for
  review instead of editing them.

### Security

- Added a security policy (`SECURITY.md`) with private vulnerability reporting.
- Dependabot now monitors Python dependencies and GitHub Actions.
- CI runs with read-only permissions and does not keep a GitHub token on disk.
- The development setup pins Prism to an exact version and verifies the oasdiff download
  against its official SHA-256 checksum before installing it.
- The verification proxy removes line breaks from the headers it passes on from the mock,
  so a header can never be split into extra headers (HTTP response splitting, reported by
  CodeQL). It also no longer sends the `Date` and `Server` headers twice.

## [0.1.1] - 2026-11-09

### Changed

- Clarified positioning: the tagline now describes what apimend does today; "Dependabot for
  APIs" is presented as the project's vision.
- The README lists the exact current scope of each capability (available, limited, roadmap).
- The README explains what fix verification does and does not prove.
- The package description and `apimend --help` use the same, accurate tagline.
- The roadmap is reorganised into releases: trustworthy foundations (v0.2), reach (v0.3) and
  automation (v0.4).

## [0.1.0] - 2026-11-08

First public release. apimend can detect breaking API changes, find the code they affect,
and fix renamed request properties, keeping a fix only after your tests pass against a mock
of the new API.

### Added

- `apimend doctor` checks that the required tools (oasdiff, Prism) are installed.
- `apimend diff` reports breaking changes between two OpenAPI specs, with exit codes for CI.
- `apimend scan` finds the exact lines of Python code affected by removed or newly required
  request properties and removed endpoints, and detects likely renames.
- `apimend fix` repairs renamed request properties, previews the change as a diff, verifies
  it by running your tests against a Prism mock of the new API, and undoes it automatically
  if anything fails.
- Pizza API lab: a reproducible breaking change with a customer app and tests.
- Dev container for one-click setup in GitHub Codespaces.
- Continuous integration running lint, formatting checks and the full test suite.

[Unreleased]: https://github.com/RedHairPirates007/apimend/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/RedHairPirates007/apimend/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/RedHairPirates007/apimend/releases/tag/v0.1.0
