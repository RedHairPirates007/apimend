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
- Every affected line found by `scan` and `fix` has a confidence level (high, medium or
  low) with the reasons when it is not high: whether the property is used in the same
  function as the call, whether it is used as a dictionary key, whether the path matches
  exactly, and whether the call uses the right HTTP method.
- `--min-confidence` on `scan` hides less certain lines; on `fix` it sets which lines may be
  changed.

### Changed

- A fix is now kept only when the evidence supports it. Tests that pass without calling the
  API give `UNVERIFIED`, and requests that do not match the new spec give `NOT VERIFIED`; in
  both cases the fix is undone.
- With `--no-mock`, a passing run is reported as `TESTS PASSED` instead of `VERIFIED`.
- `fix` only changes lines found with high confidence by default, and lists the others for
  review instead of editing them.

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

[Unreleased]: https://github.com/sarathpanicker07/apimend/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/sarathpanicker07/apimend/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/sarathpanicker07/apimend/releases/tag/v0.1.0
