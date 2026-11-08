# Changelog

All notable changes to apimend are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/sarathpanicker07/apimend/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sarathpanicker07/apimend/releases/tag/v0.1.0