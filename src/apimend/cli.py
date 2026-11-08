"""Command-line interface for apimend.

Commands:
  apimend doctor             Check that the required tools are installed.
  apimend diff OLD NEW       Show breaking changes between two OpenAPI specs.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from apimend import __version__

# External tools apimend relies on, and how to install each one.
REQUIRED_TOOLS = {
    "oasdiff": "curl -fsSL https://raw.githubusercontent.com/oasdiff/oasdiff/main/install.sh | sudo sh",
    "prism": "npm install -g @stoplight/prism-cli",
}


def check_tools() -> dict[str, bool]:
    """Return {tool_name: is_installed} for every required tool."""
    return {name: shutil.which(name) is not None for name in REQUIRED_TOOLS}


def cmd_doctor(_args: argparse.Namespace) -> int:
    print(f"apimend {__version__}")
    print(f"python  {sys.version.split()[0]}")
    all_ok = True
    for name, ok in check_tools().items():
        if ok:
            print(f"  [ok]      {name}")
        else:
            all_ok = False
            print(f"  [missing] {name}  ->  install with: {REQUIRED_TOOLS[name]}")
    print("Everything is ready." if all_ok else "Some tools are missing (see above).")
    return 0 if all_ok else 1


def cmd_diff(args: argparse.Namespace) -> int:
    if shutil.which("oasdiff") is None:
        print("oasdiff is not installed. Run 'apimend doctor' for help.", file=sys.stderr)
        return 2
    # --fail-on ERR makes oasdiff exit with 1 when it finds a definite breaking change.
    result = subprocess.run(
        ["oasdiff", "breaking", args.old, args.new, "--fail-on", "ERR"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout.strip()
    if result.returncode == 0:
        print("No breaking changes found.")
        if output:
            print(output)
        return 0
    if result.returncode == 1:
        print("BREAKING CHANGES FOUND:\n")
        print(output)
        return 1
    print(result.stderr.strip() or output, file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apimend",
        description="Dependabot for APIs: find breaking API changes and fix them safely.",
    )
    parser.add_argument("--version", action="version", version=f"apimend {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check that required tools are installed")
    doctor.set_defaults(func=cmd_doctor)

    diff = sub.add_parser("diff", help="show breaking changes between two OpenAPI specs")
    diff.add_argument("old", help="path to the old spec (e.g. lab/pizza-api/v1.yaml)")
    diff.add_argument("new", help="path to the new spec (e.g. lab/pizza-api/v2.yaml)")
    diff.set_defaults(func=cmd_diff)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())