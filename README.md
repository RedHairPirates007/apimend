<div align="center">

<img src="assets/logo.svg" width="96" height="96" alt="apimend logo">

# apimend

**Catch breaking OpenAPI changes in your code — and fix them safely.**

[![CI](https://github.com/sarathpanicker07/apimend/actions/workflows/ci.yml/badge.svg)](https://github.com/sarathpanicker07/apimend/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/sarathpanicker07/apimend)](https://github.com/sarathpanicker07/apimend/releases)
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
> apimend is in **early development**. Today it is a command-line tool: you give it two
> versions of an OpenAPI spec, and it finds and fixes affected **Python** code. See
> [what works today](#what-works-today) for the exact scope.
>
> **Vision:** a "Dependabot for APIs" that watches the APIs you depend on and opens verified
> pull requests automatically. That experience is on the [roadmap](#roadmap), not built yet.

## Why apimend?

Your app depends on outside APIs: payments, AI models, messaging and more. When a provider
changes its API, your app can break **without a single line of your code changing** — and
teams usually find out in production.

apimend aims to close that gap.

### What works today

| Capability | Status | Scope today |
|---|:-:|---|
| Compare two OpenAPI specs | ✅ Available | Specs you provide (YAML or JSON) |
| Detect breaking changes | ✅ Available | Via [oasdiff](https://github.com/oasdiff/oasdiff) |
| Find affected code | ✅ Available | **Python only**; request bodies and removed endpoints; paths written as plain strings; every line rated high / medium / low confidence |
| Detect renamed properties | 🟡 Limited | One removed + one new required property on the same endpoint |
| Fix code automatically | 🟡 Limited | Renamed request properties only |
| Verify fixes | ✅ Available | Records every request your tests send to a **mock generated from the new spec** and checks it matches — not the live API |
| JavaScript / TypeScript and other languages | 🗺️ Roadmap | |
| Response-change detection | 🗺️ Roadmap | |
| Automatic pull requests | 🗺️ Roadmap | |
| Provider monitoring | 🗺️ Roadmap | You currently supply both spec versions yourself |

> [!TIP]
> **What makes apimend different:** a fix is only kept after your tests pass against a mock
> of the new API. If they fail, apimend undoes the fix and shows you why, instead of leaving
> broken code behind.

## Quick start

### Option 1: GitHub Codespaces (recommended)

Click **[Open in Codespaces](https://codespaces.new/sarathpanicker07/apimend)**. The dev
container installs Python, Node.js, [Prism](https://github.com/stoplightio/prism),
[oasdiff](https://github.com/oasdiff/oasdiff) and apimend for you. Then run:

```bash
apimend doctor
apimend diff lab/pizza-api/v1.yaml lab/pizza-api/v2.yaml
apimend scan lab/pizza-api/v1.yaml lab/pizza-api/v2.yaml lab/pizza-api/app
apimend fix  lab/pizza-api/v1.yaml lab/pizza-api/v2.yaml lab/pizza-api/app   # preview only
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
apimend 0.1.1
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
  [high]   lab/pizza-api/app/pizza_client.py:34: return _request("POST", "/orders", {"pizza": pizza, "size": size})

POST /orders: request property 'pizza_size' is now required
  hint: looks like the new name for 'size'
  [high]   lab/pizza-api/app/pizza_client.py:34: return _request("POST", "/orders", {"pizza": pizza, "size": size})

Found 1 affected line(s) in 1 file(s): 1 high, 0 medium, 0 low confidence.
```

- The folder defaults to the current directory. Skip files with `--exclude "tests/*"`
  (repeatable).
- Every line gets a **confidence level**, with the reasons whenever it is not high (see
  below). Show only the lines you trust with `--min-confidence medium` or `high`.
- Code is read with Python's `ast` module, so comments are ignored, and only files that
  reference the endpoint are checked.
- Exit codes: `0` not affected, `1` affected code found, `2` could not run.

**Confidence levels.** apimend checks each affected line for three signs that it really
talks to the changed endpoint:

| Check | Example that passes |
|---|---|
| The property is used in the **same function** as the call to the endpoint | `requests.post("/orders", json={"size": size})` |
| The property is used as a **dictionary key** (not just mentioned in some text) | `{"size": size}` or `body["size"]` |
| The **path matches exactly** (paths with `{parameters}` can only partly match) | `"/orders"` for `/orders` |

| Level | Meaning |
|---|---|
| `high` | Every check passed |
| `medium` | One check failed — probably affected, worth a look |
| `low` | Two or more checks failed, or the code calls a **different HTTP method** (for example `GET /orders` when `POST /orders` changed) |

For lines that call the endpoint (a removed endpoint, or a property that became required),
only the path and HTTP method checks apply. apimend reads the HTTP method from calls such as
`requests.post(...)` or `request("POST", ...)`; when it cannot tell, the confidence is not
lowered.

> [!IMPORTANT]
> Current limits: scans **Python** files only, checks **request bodies and removed endpoints**
> (not responses yet), and finds endpoints written as plain strings in the code.

### `apimend fix <old-spec> <new-spec> [folder] --test "<command>"`

Fixes affected code and **checks the fix**: it applies the change, runs your tests against a
[Prism](https://github.com/stoplightio/prism) mock of the **new** API, and records every request
your tests send. The fix is kept only if the tests pass **and** every request matches the new
spec.

```console
$ apimend fix lab/pizza-api/v1.yaml lab/pizza-api/v2.yaml lab/pizza-api/app --test "python -m pytest lab/pizza-api/app -q"
Proposed fix (1 line(s), high confidence or above):

--- a/lab/pizza-api/app/pizza_client.py
+++ b/lab/pizza-api/app/pizza_client.py
@@ -31,4 +31,4 @@
 
 def place_order(pizza: str, size: str) -> dict:
     # Written for API v1, which expects the field name "size".
-    return _request("POST", "/orders", {"pizza": pizza, "size": size})
\ No newline at end of file
+    return _request("POST", "/orders", {"pizza": pizza, "pizza_size": size})
\ No newline at end of file

Applying the fix and verifying it with: python -m pytest lab/pizza-api/app -q
Tests will run against a mock of lab/pizza-api/v2.yaml on port 4010.

Verification report
  [passed] Tests
  [passed] Contract: 2 request(s) reached the mock, all matched the spec
             GET /menu -> 200: matches the new spec
             POST /orders -> 201: matches the new spec
  [passed] Re-scan: no affected code remains
  [not checked] Live API behaviour: authentication, rate limits, real data and errors

VERIFIED against the new spec: the fix has been kept.
Review the change with 'git diff' before committing it.
```

| Option | Meaning |
|---|---|
| `--test "<command>"` | Command that runs your tests. **Without it, `fix` only previews** and changes nothing. |
| `--port <number>` | Port for the mock server (default `4010`). The URL is also passed to your tests as `APIMEND_MOCK_URL`. |
| `--no-mock` | Run the tests without starting a mock server (requests are then not checked). |
| `--allow-unverified` | Keep the fix even if your tests never called the mock API. |
| `--min-confidence <level>` | Only change lines found with at least this confidence (default `high`). Lines below it are listed for you to review. |
| `--exclude "<pattern>"` | Skip matching files, as in `scan`. |

**Safety first:**

- By default only lines found with **high confidence** are changed. Anything less certain is
  listed with the reason, for you to review (or to include with `--min-confidence medium`).
- Only the exact characters that need changing are edited — quote style, comments, spacing,
  line endings and non-English text are preserved.
- If the tests fail, a setup problem occurs (busy port, Prism missing), or you press
  <kbd>Ctrl</kbd>+<kbd>C</kbd>, every file is restored byte for byte.
- Exit codes: `0` fix kept (or nothing to fix), `1` preview only, or the fix was undone,
  `2` could not run.

**The verification report** lists exactly what was checked:

| Check | Passes when |
|---|---|
| Tests | Your test command succeeds |
| Contract | Your tests sent at least one request to the mock, and **every** request matched the new spec |
| Re-scan | No affected code remains after the fix |
| Live API behaviour | **Never checked** — authentication, rate limits, real data and errors can differ |

| Result | Meaning | Fix kept? |
|---|---|:-:|
| `VERIFIED` | Tests passed and every request matched the new spec | ✅ |
| `TESTS PASSED` | Tests passed; requests were not checked (`--no-mock`) | ✅ |
| `UNVERIFIED` | Tests passed but never called the API | ❌ (✅ with `--allow-unverified`) |
| `NOT VERIFIED` | Tests failed, or a request did not match the new spec | ❌ |

> [!NOTE]
> `fix` currently repairs **renamed request properties** in Python code (dictionary keys such
> as `{"size": ...}` and lookups such as `body["size"]`). It only acts when the rename is
> unambiguous.

> [!WARNING]
> **What `VERIFIED` means:** your tests passed, and every request they sent matched the new
> spec. It does **not** prove the fix works against the live provider — authentication, rate
> limits, real error responses and data can differ from what the spec describes.

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
    class B,C,D,E done
    class A,F,G planned
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

Stop Prism again, then let apimend repair the app and check the repair against the v2 mock:

```bash
apimend fix lab/pizza-api/v1.yaml lab/pizza-api/v2.yaml lab/pizza-api/app --test "python -m pytest lab/pizza-api/app -q"
git restore lab/pizza-api/app/pizza_client.py   # put the lab back for next time
```

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
│   ├── cli.py            Command-line interface (doctor, diff, scan, fix)
│   ├── fix.py            Plans and applies fixes, undoing them unless verified
│   ├── scan.py           Finds code affected by breaking changes
│   └── verify.py         Mock server, request recording and verification reports
├── tests/                Tests for apimend itself
└── pyproject.toml        Package metadata and tool settings
```

</details>

## Roadmap

**v0.1 — Core loop (done)**

- [x] Detect breaking changes between two OpenAPI specs (`apimend diff`)
- [x] Find affected Python code (`apimend scan`) and fix renamed request properties (`apimend fix`)
- [x] Test suite, dev container, CI and honest documentation of scope

**v0.2 — Trustworthy foundations**

- [x] Verification reports that show what was actually checked (requests that reached the
      mock, whether they matched the spec) and never over-claim
- [x] Confidence scores for affected code (high / medium / low)
- [ ] Language-adapter architecture, so new languages plug into the same core
- [ ] Real-world benchmark on public API specs and repositories, with published results

**v0.3 — Reach**

- [ ] JavaScript / TypeScript support
- [ ] Response-change detection
- [ ] Smarter rename detection (types, enums, several renames at once)

**v0.4 — Automation ("Dependabot for APIs")**

- [ ] GitHub Action: comments on pull requests and opens verified fix pull requests
- [ ] Provider monitoring: detect new API versions automatically

**Later**

- [ ] Go, Java and C# support
- [ ] Optional verification against a provider's sandbox environment
- [ ] More automatic fixes (including AI-assisted), kept behind the same verification gate

## Contributing

Ideas, bug reports and pull requests are welcome!

1. Open an [issue](https://github.com/sarathpanicker07/apimend/issues) to discuss larger changes first.
2. Fork the repo and open it in Codespaces — everything is set up for you.
3. Make sure `pytest` and `ruff check .` pass before opening a pull request.

## Security

Found a security problem? Please report it privately, as described in
[SECURITY.md](SECURITY.md), which also explains what apimend does and does not do on your
machine.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

## License

Distributed under the [Apache License 2.0](LICENSE).

<div align="center">
<br>
<sub>If apimend saves you from a broken deploy, consider giving it a ⭐</sub>
</div>
