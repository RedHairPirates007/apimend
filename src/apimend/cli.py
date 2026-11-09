"""Command-line interface for apimend.

Commands:
  apimend doctor             Check that the required tools are installed.
  apimend diff OLD NEW       Show breaking changes between two OpenAPI specs.
  apimend scan OLD NEW [DIR] Find the lines of code affected by those changes.
  apimend fix OLD NEW [DIR]  Fix affected code, and prove the fix with your tests.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from apimend import __version__
from apimend.fix import FixError, apply_and_verify, plan_fixes, render_diff
from apimend.scan import SpecError, find_changes, find_usages, load_spec
from apimend.verify import FAILED, TESTS_ONLY, UNVERIFIED, VERIFIED, Report, VerifyError

# External tools apimend relies on, and how to install each one.
REQUIRED_TOOLS = {
    "oasdiff": "bash .devcontainer/setup.sh",
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


def cmd_scan(args: argparse.Namespace) -> int:
    try:
        old, new = load_spec(args.old), load_spec(args.new)
    except SpecError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if not Path(args.path).is_dir():
        print(f"Error: {args.path} is not a folder", file=sys.stderr)
        return 2

    changes = find_changes(old, new)
    if not changes:
        print("No breaking request changes found. Nothing to scan for.")
        return 0

    print(f"Scanning {args.path} for code affected by {len(changes)} breaking change(s)...")
    affected: set[tuple[Path, int]] = set()
    for change in changes:
        print(f"\n{change.describe()}")
        if change.hint:
            print(f"  hint: {change.hint}")
        usages = find_usages(args.path, change, args.exclude)
        if not usages:
            print("  no affected code found")
        for usage in usages:
            print(f"  {usage.file}:{usage.line}: {usage.code}")
            affected.add((usage.file, usage.line))

    if not affected:
        print("\nYour code does not appear to be affected.")
        return 0
    files = {file for file, _ in affected}
    print(f"\nFound {len(affected)} affected line(s) in {len(files)} file(s).")
    return 1


def cmd_fix(args: argparse.Namespace) -> int:
    try:
        old, new = load_spec(args.old), load_spec(args.new)
    except SpecError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if not Path(args.path).is_dir():
        print(f"Error: {args.path} is not a folder", file=sys.stderr)
        return 2

    changes = find_changes(old, new)
    if not changes:
        print("No breaking request changes found. Nothing to fix.")
        return 0

    edits = plan_fixes(changes, args.path, args.exclude)
    if not edits:
        if any(find_usages(args.path, change, args.exclude) for change in changes):
            print("Your code is affected, but apimend has no automatic fix for these changes yet.")
            print(f"Run 'apimend scan {args.old} {args.new} {args.path}' to see where.")
            return 1
        print("Your code does not appear to be affected. Nothing to fix.")
        return 0

    print(f"Proposed fix ({len(edits)} line(s)):\n")
    print(render_diff(edits))
    if not args.test:
        print("Preview only: no files were changed.")
        print('To apply and verify the fix, add --test "<command that runs your tests>".')
        return 1

    print(f"Applying the fix and verifying it with: {args.test}")
    if not args.no_mock:
        print(f"Tests will run against a mock of {args.new} on port {args.port}.")

    def remaining_lines() -> int:
        found = {(u.file, u.line) for c in changes for u in find_usages(args.path, c, args.exclude)}
        return len(found)

    try:
        report, kept = apply_and_verify(
            edits,
            args.test,
            args.new,
            args.port,
            use_mock=not args.no_mock,
            allow_unverified=args.allow_unverified,
            recheck=remaining_lines,
        )
    except (FixError, VerifyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("No files were changed.", file=sys.stderr)
        return 2

    print_report(report, args.new)
    return print_outcome(report, kept)


def print_report(report: Report, spec: str) -> None:
    """Print exactly what verification checked, and what it found."""
    print("\nVerification report")
    print(f"  [{'passed' if report.tests_passed else 'FAILED'}] Tests")
    if not report.mock_used:
        print("  [skipped] Contract: --no-mock was used, so requests were not checked")
    elif not report.requests:
        print(f"  [FAILED] Contract: your tests made no requests to the mock of {spec}")
    else:
        total, bad = len(report.requests), len(report.mismatches)
        if bad:
            print(f"  [FAILED] Contract: {bad} of {total} request(s) did not match the new spec")
        else:
            print(f"  [passed] Contract: {total} request(s) reached the mock, all matched the spec")
        for request in report.requests[:10]:
            print(f"             {request.describe()}")
        if total > 10:
            print(f"             ... and {total - 10} more")
    if report.remaining:
        print(f"  [note] Re-scan: {report.remaining} affected line(s) remain (no automatic fix)")
    else:
        print("  [passed] Re-scan: no affected code remains")
    print("  [not checked] Live API behaviour: authentication, rate limits, real data and errors")


def print_outcome(report: Report, kept: bool) -> int:
    """Explain the decision and return the exit code."""
    verdict = report.verdict
    if verdict == VERIFIED:
        print("\nVERIFIED against the new spec: the fix has been kept.")
    elif verdict == TESTS_ONLY:
        print("\nTESTS PASSED (requests not checked against the spec): the fix has been kept.")
    elif verdict == UNVERIFIED and kept:
        print("\nUNVERIFIED: kept anyway because --allow-unverified was used.")
    elif verdict == UNVERIFIED:
        print("\nUNVERIFIED: your tests passed but never called the API. The fix was undone.")
        print("Point your API client at $APIMEND_MOCK_URL during tests, or re-run with")
        print("--allow-unverified to keep the fix without verification.")
        return 1
    elif verdict == FAILED and report.tests_passed:
        print("\nNOT VERIFIED: some requests did not match the new spec. The fix was undone.")
        return 1
    else:
        print("\nNOT VERIFIED: the tests failed, so the fix was undone. No files were changed.")
        print("\nLast lines of the test output:")
        print("\n".join(report.test_output.strip().splitlines()[-20:]))
        return 1
    print("Review the change with 'git diff' before committing it.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apimend",
        description="Catch breaking OpenAPI changes in your code, and fix them safely.",
    )
    parser.add_argument("--version", action="version", version=f"apimend {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check that required tools are installed")
    doctor.set_defaults(func=cmd_doctor)

    diff = sub.add_parser("diff", help="show breaking changes between two OpenAPI specs")
    diff.add_argument("old", help="path to the old spec (e.g. lab/pizza-api/v1.yaml)")
    diff.add_argument("new", help="path to the new spec (e.g. lab/pizza-api/v2.yaml)")
    diff.set_defaults(func=cmd_diff)

    scan = sub.add_parser("scan", help="find the lines of code affected by breaking changes")
    scan.add_argument("old", help="path to the old spec")
    scan.add_argument("new", help="path to the new spec")
    scan.add_argument("path", nargs="?", default=".", help="folder to scan (default: .)")
    scan.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help='skip files matching a pattern, e.g. "tests/*" (can be repeated)',
    )
    scan.set_defaults(func=cmd_scan)

    fix = sub.add_parser("fix", help="fix affected code and prove the fix with your tests")
    fix.add_argument("old", help="path to the old spec")
    fix.add_argument("new", help="path to the new spec")
    fix.add_argument("path", nargs="?", default=".", help="folder to fix (default: .)")
    fix.add_argument(
        "--test",
        metavar="COMMAND",
        help="command that runs your tests; without it apimend only previews the fix",
    )
    fix.add_argument(
        "--port", type=int, default=4010, help="port for the mock API server (default: 4010)"
    )
    fix.add_argument(
        "--no-mock", action="store_true", help="run the tests without starting a mock server"
    )
    fix.add_argument(
        "--allow-unverified",
        action="store_true",
        help="keep the fix even if your tests never called the mock API",
    )
    fix.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help='skip files matching a pattern, e.g. "tests/*" (can be repeated)',
    )
    fix.set_defaults(func=cmd_fix)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
