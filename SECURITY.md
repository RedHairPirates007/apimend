# Security Policy

## Supported versions

apimend is in early development. Security fixes are made for the **latest release only**.

| Version | Supported |
|---|:-:|
| Latest release | ✅ |
| Older releases | ❌ |

## Reporting a vulnerability

**Please do not report security problems in public issues.**

Report them privately through GitHub instead:
[**Report a vulnerability**](https://github.com/sarathpanicker07/apimend/security/advisories/new)
(Security tab → *Report a vulnerability*).

Please include:

- what the problem is and what an attacker could do with it,
- the steps (or a small example spec and code) to reproduce it,
- the apimend version (`apimend --version`) and your operating system.

apimend is maintained by one person, so please allow some time. You can expect a first
reply within **7 days**. Once the problem is confirmed, a fix is released as soon as
possible and you are credited in the release notes, unless you prefer not to be.

## Security model

What apimend does, so you can judge the risks:

- **It runs your test command.** `apimend fix --test "<command>"` runs that command as you,
  on your machine, exactly like running it yourself. The command is not passed through a
  shell, so characters such as `;` or `&&` in it are not interpreted.
- **It reads specs as data only.** YAML is loaded with `yaml.safe_load`, which never runs
  code. Only local `$ref` links (`#/...`) are followed; apimend never downloads anything a
  spec points to.
- **It stays on your machine.** The mock server and the request-recording proxy listen on
  `127.0.0.1` only, so they cannot be reached from other computers. apimend does not send
  your code or specs anywhere.
- **It only changes files you point it at.** `fix` edits files in the folder you give it, and
  restores them exactly if verification fails or is interrupted.

### Out of scope

- Problems in your own test command or the code it runs.
- Vulnerabilities in [oasdiff](https://github.com/oasdiff/oasdiff) or
  [Prism](https://github.com/stoplightio/prism) themselves; please report those to their
  projects.

## How this repository is protected

- Dependencies are monitored by Dependabot, and code is scanned by GitHub CodeQL.
- CI runs with read-only permissions.
- Development tools are pinned to exact versions, and the oasdiff download is checked
  against its official SHA-256 checksum before it is installed.
