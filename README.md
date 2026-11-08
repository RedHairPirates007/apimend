<div align="center">

<img src="assets/logo.svg" width="96" height="96" alt="apimend logo">

# apimend

**Dependabot for APIs** — catch breaking API changes before your users do.

[![CI](https://github.com/sarathpanicker07/apimend/actions/workflows/ci.yml/badge.svg)](https://github.com/sarathpanicker07/apimend/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Open in GitHub Codespaces](https://img.shields.io/badge/Open%20in-Codespaces-0F766E?logo=github)](https://codespaces.new/sarathpanicker07/apimend)

[Quick start](#quick-start) •
[Usage](#usage) •
[How it works](#how-it-works) •
[Roadmap](#roadmap) •
[Contributing](#contributing)

</div>

---

> [!NOTE]
> apimend is in **early development**. Detecting breaking changes and locating affected code
> work today; automatic, test-verified fixes are on the [roadmap](#roadmap).

## Why apimend?

Your app depends on outside APIs: payments, AI models, messaging and more. When a provider
changes its API, your app can break **without a single line of your code changing** — and
teams usually find out in production.

apimend closes that gap:

| | Capability | Status |
|:-:|---|:-:|
| 🔍 | **Detect** breaking changes between two versions of an API spec | ✅ Available |
| 📍 | **Locate** the exact lines of your code each change affects | ✅ Available |
| 🛠️ | **Fix** the affected code automatically | 🚧 Planned |
| ✅ | **Verify** every fix against a mock of the new API before proposing it | 🚧 Planned |

> [!TIP]
> **What makes apimend different:** a fix is only proposed after it passes tests against a
> mock of the new API. If apimend can't produce a working fix, it explains the problem
> instead of opening a broken pull request.

## Quick start

### Option 1: GitHub Codespaces (recommended)

Click **[Open in Codespaces](https://codespaces.new/sarathpanicker07/apimend)**. The dev
container installs Python, Node.js, [Prism](https://github.com/stoplightio/prism),
[oasdiff](https://github.com/oasdiff/oasdiff) and apimend for you. Then run:

```bash
apimend doctor
apimend diff lab/pizza-api/v1.yaml lab/pizza-api/v2.yaml
apimend scan lab/pizza-api/v1.yaml lab/pizza-api/v2.yaml lab/pizza-api/app
```

<details>
<summary><strong>Option 2: Install locally</strong></summary>

<br>

Requirements: Python 3.10+, Node.js, and [oasdiff](https://github.com/oasdiff/oasdiff#installation).

```bash
git clone https://github.com/sarathpanicker07/apimend.git
cd apimend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
npm install -g @stoplight/prism-cli
apimend doctor
```

</details>

## Usage

### `apimend doctor`

Checks that every tool apimend needs is installed.

```console
$ apimend doctor
apimend 0.0.1
python  3.12.11
  [ok]      oasdiff
  [ok]      prism
Everything is ready.
```

### `apimend diff <old-spec> <new-spec>`

Compares two versions of an OpenAPI spec and reports changes that break existing apps.

```console
$ apimend diff lab/pizza-api/v1.yaml lab/pizza-api/v2.yaml
BREAKING CHANGES FOUND:

2 changes: 1 error, 1 warning, 0 info
error   [new-required-request-property] at lab/pizza-api/v2.yaml
        in API POST /orders
                added the new required request property `pizza_size`

warning [request-property-removed] at lab/pizza-api/v2.yaml
        in API POST /orders
                removed the request property `size`
```

#### Exit codes

Designed for scripts and CI pipelines:

| Code | Meaning |
|:-:|---|
| `0` | No breaking changes |
| `1` | Breaking changes found |
| `2` | Could not run (for example, oasdiff is missing or a spec is invalid) |

### `apimend scan <old-spec> <new-spec> [folder]`

Finds the exact lines of Python code affected by breaking changes to request bodies and
endpoints, and spots likely renames.

```console
$ apimend scan lab/pizza-api/v1.yaml lab/pizza-api/v2.yaml lab/pizza-api/app
Scanning lab/pizza-api/app for code affected by 2 breaking change(s)...

POST /orders: request property 'size' was removed
  hint: looks like it was renamed to 'pizza_size'
  lab/pizza-api/app/pizza_client.py:34: return _request("POST", "/orders", {"pizza": pizza, "size": size})

POST /orders: request property 'pizza_size' is now required
  hint: looks like the new name for 'size'
  lab/pizza-api/app/pizza_client.py:34: return _request("POST", "/orders", {"pizza": pizza, "size": size})

Found 1 affected line(s) in 1 file(s).
```

- The folder defaults to the current directory. Skip files with `--exclude "tests/*"`
  (repeatable).
- Code is read with Python's `ast` module, so comments are ignored, and only files that
  reference the endpoint are checked.
- Exit codes: `0` not affected, `1` affected code found, `2` could not run.

> [!IMPORTANT]
> Current limits: scans **Python** files only, checks **request bodies and removed endpoints**
> (not responses yet), and finds endpoints written as plain strings in the code.

## How it works

```mermaid
flowchart LR
    A[Watch providers] --> B[Diff and classify]
    B --> C[Find call sites]
    C --> D[Generate fix]
    D --> E{Verify on mock API}
    E -- pass --> F[Open pull request]
    E -- fail --> G[Explain in a comment]

    classDef done fill:#0F766E,stroke:#0F766E,color:#ffffff
    classDef planned stroke-dasharray:5 5
    class B,C done
    class A,D,E,F,G planned
```

<sub>Solid: available today · Dashed: planned</sub>

## The Pizza API lab

`lab/pizza-api/` is a small, controlled test bed for developing apimend:

| Path | Purpose |
|---|---|
| `v1.yaml` | The original API: orders take a `size` field |
| `v2.yaml` | The new API: `size` was renamed to `pizza_size` — a realistic breaking change |
| `app/` | A tiny customer app written for v1, with tests |

<details>
<summary><strong>Reproduce the breakage yourself</strong></summary>

<br>

Use two terminals:

```bash
# Terminal 1 — run a fake v1 API
prism mock lab/pizza-api/v1.yaml

# Terminal 2 — the app works
python -m pytest lab/pizza-api/app -v     # 2 passed
```

Stop Prism (<kbd>Ctrl</kbd>+<kbd>C</kbd>), start it again with `v2.yaml`, and rerun the tests.
The order test now fails with `HTTP Error 422` — the app broke without any change to its code.
Fixing that automatically is apimend's job.

</details>

## Development

```bash
pip install -e ".[dev]"   # already done in Codespaces
pytest -v                 # run the test suite
ruff check .              # lint
```

Every push and pull request runs the same checks in
[GitHub Actions](.github/workflows/ci.yml).

<details>
<summary><strong>Project layout</strong></summary>

<br>

```text
apimend/
├── .devcontainer/        Codespaces setup (installs everything automatically)
├── .github/workflows/    CI configuration
├── assets/               Logo and images
├── lab/pizza-api/        Test bed: fake API versions and a customer app
├── src/apimend/          The apimend package
│   ├── __init__.py
│   ├── cli.py            Command-line interface (doctor, diff, scan)
│   └── scan.py           Finds code affected by breaking changes
├── tests/                Tests for apimend itself
└── pyproject.toml        Package metadata and tool settings
```

</details>

## Roadmap

- [x] Detect breaking changes between two OpenAPI specs (`apimend diff`)
- [x] Environment checks (`apimend doctor`), test suite, dev container and CI
- [x] `apimend scan` — find the exact lines of Python code a breaking change affects
- [ ] `apimend fix` — generate fixes and verify them against a mock of the new API
- [ ] `scan` support for more languages (JavaScript/TypeScript) and response changes
- [ ] Real-world benchmark using past API migrations from public repositories
- [ ] GitHub Action / App that opens verified pull requests automatically

## Contributing

Ideas, bug reports and pull requests are welcome!

1. Open an [issue](https://github.com/sarathpanicker07/apimend/issues) to discuss larger changes first.
2. Fork the repo and open it in Codespaces — everything is set up for you.
3. Make sure `pytest` and `ruff check .` pass before opening a pull request.

## License

Distributed under the [Apache License 2.0](LICENSE).

<div align="center">
<br>
<sub>If apimend saves you from a broken deploy, consider giving it a ⭐</sub>
</div>
