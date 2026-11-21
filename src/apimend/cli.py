"""Command-line interface for apimend.

Commands:
  apimend doctor             Check that the required tools are installed.
  apimend diff OLD NEW       Show breaking changes between two OpenAPI specs.
  apimend scan OLD NEW [DIR] Find the lines of code affected by those changes.
  apimend fix OLD NEW [DIR]  Fix affected code, and prove the fix with your tests.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

from apimend import __version__
from apimend.fix import FixError, apply_and_verify, plan_fixes, rename_usages, render_diff
from apimend.output import (
    change_json,
    document,
    edit_json,
    report_json,
    unreadable_json,
    usage_json,
)
from apimend.safety import UnsafePathError
from apimend.scan import (
    CONFIDENCE_LEVELS,
    HIGH,
    LOW,
    MEDIUM,
    REMOVED_ENDPOINT,
    SpecError,
    Usage,
    confidence_rank,
    find_changes,
    find_usages,
    load_spec,
    unreadable_files,
)
from apimend.verify import (
    FAILED,
    PARTIAL,
    TESTS_ONLY,
    UNVERIFIED,
    VERIFIED,
    Endpoint,
    Report,
    VerifyError,
)

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


def print_usage(usage: Usage) -> None:
    """Print one affected line with its confidence, and why it is not high."""
    label = f"[{usage.confidence}]".ljust(8)
    print(f"  {label} {usage.file}:{usage.line}: {usage.code}")
    for reason in usage.reasons:
        print(f"           - {reason}")


def print_unreadable(files: list[tuple[Path, str]]) -> None:
    """Say which files could not be analysed, so nothing is skipped silently."""
    print(f"\nWarning: {len(files)} file(s) could not be analysed, so they were not checked:")
    for file, reason in files:
        print(f"  {file}: {reason}")


def fail(args: argparse.Namespace, message: str) -> int:
    """Report a problem that stopped the command (exit code 2)."""
    print(f"Error: {message}", file=sys.stderr)
    args.out.update(result="error", error=message)
    return 2


def cmd_scan(args: argparse.Namespace) -> int:
    out = args.out
    try:
        old, new = load_spec(args.old), load_spec(args.new)
    except SpecError as exc:
        return fail(args, str(exc))
    if not Path(args.path).is_dir():
        return fail(args, f"{args.path} is not a folder")

    changes = find_changes(old, new)
    out.update(
        changes=[],
        unreadable_files=[],
        summary={
            "affected_lines": 0,
            "affected_files": 0,
            "by_confidence": dict.fromkeys(reversed(CONFIDENCE_LEVELS), 0),
            "hidden_lines": 0,
        },
    )
    if not changes:
        print("No breaking request changes found. Nothing to scan for.")
        out["result"] = "no-changes"
        return 0

    print(f"Scanning {args.path} for code affected by {len(changes)} breaking change(s)...")
    threshold = confidence_rank(args.min_confidence)
    affected: dict[tuple[Path, int], str] = {}  # line -> strongest confidence shown
    hidden: set[tuple[Path, int]] = set()
    for change in changes:
        print(f"\n{change.describe()}")
        if change.hint:
            print(f"  hint: {change.hint}")
        usages = find_usages(args.path, change, args.exclude)
        shown = [u for u in usages if confidence_rank(u.confidence) >= threshold]
        hidden.update((u.file, u.line) for u in usages if u not in shown)
        out["changes"].append(
            {
                **change_json(change),
                "usages": [usage_json(u) for u in shown],
                "hidden_usages": len(usages) - len(shown),
            }
        )
        if not usages:
            print("  no affected code found")
        elif not shown:
            print(f"  no affected code with {args.min_confidence} confidence or above")
        for usage in shown:
            print_usage(usage)
            key = (usage.file, usage.line)
            strongest = affected.get(key, usage.confidence)
            affected[key] = max(strongest, usage.confidence, key=confidence_rank)

    unreadable = unreadable_files(args.path, args.exclude)
    out["unreadable_files"] = unreadable_json(unreadable)
    if unreadable:
        print_unreadable(unreadable)
    hidden -= affected.keys()
    out["summary"] = {
        "affected_lines": len(affected),
        "affected_files": len({file for file, _ in affected}),
        "by_confidence": {
            level: list(affected.values()).count(level) for level in reversed(CONFIDENCE_LEVELS)
        },
        "hidden_lines": len(hidden),
    }
    if hidden:
        print(
            f"\n{len(hidden)} line(s) with lower confidence were hidden"
            f" by --min-confidence {args.min_confidence}."
        )
    if not affected:
        where = " in the files apimend could analyse" if unreadable else ""
        print(f"\nYour code does not appear to be affected{where}.")
        out["result"] = "not-affected"
        return 0
    out["result"] = "affected"
    files = {file for file, _ in affected}
    counts = ", ".join(
        f"{list(affected.values()).count(level)} {level}" for level in reversed(CONFIDENCE_LEVELS)
    )
    print(f"\nFound {len(affected)} affected line(s) in {len(files)} file(s): {counts} confidence.")
    return 1


def cmd_fix(args: argparse.Namespace) -> int:
    out = args.out
    try:
        old, new = load_spec(args.old), load_spec(args.new)
    except SpecError as exc:
        return fail(args, str(exc))
    if not Path(args.path).is_dir():
        return fail(args, f"{args.path} is not a folder")

    changes = find_changes(old, new)
    out.update(
        changes=[change_json(c) for c in changes],
        unreadable_files=[],
        edits=[],
        skipped=[],
        files_changed=False,
        verification=None,
    )
    if not changes:
        print("No breaking request changes found. Nothing to fix.")
        out["result"] = "no-changes"
        return 0

    unreadable = unreadable_files(args.path, args.exclude)
    out["unreadable_files"] = unreadable_json(unreadable)
    if unreadable:
        print_unreadable(unreadable)
        print()
    edits = plan_fixes(changes, args.path, args.exclude, args.min_confidence)
    threshold = confidence_rank(args.min_confidence)
    skipped = [
        usage
        for _, usage in rename_usages(changes, args.path, args.exclude)
        if confidence_rank(usage.confidence) < threshold
    ]
    out.update(edits=[edit_json(e) for e in edits], skipped=[usage_json(u) for u in skipped])
    if not edits:
        if skipped:
            print(f"apimend found lines to fix, but none with {args.min_confidence} confidence.")
            print_skipped(skipped, args.min_confidence)
            out["result"] = "below-confidence"
            return 1
        if any(find_usages(args.path, change, args.exclude) for change in changes):
            print("Your code is affected, but apimend has no automatic fix for these changes yet.")
            print(f"Run 'apimend scan {args.old} {args.new} {args.path}' to see where.")
            out["result"] = "no-automatic-fix"
            return 1
        where = " in the files apimend could analyse" if unreadable else ""
        print(f"Your code does not appear to be affected{where}. Nothing to fix.")
        out["result"] = "not-affected"
        return 0

    print(f"Proposed fix ({len(edits)} line(s), {args.min_confidence} confidence or above):\n")
    print(render_diff(edits))
    if skipped:
        print_skipped(skipped, args.min_confidence)
        print()
    if not args.test:
        print("Preview only: no files were changed.")
        print('To apply and verify the fix, add --test "<command that runs your tests>".')
        out["result"] = "preview"
        return 1

    print(f"Applying the fix and verifying it with: {args.test}")
    if not args.no_mock:
        print(f"Tests will run against a mock of {args.new} on port {args.port}.")

    def remaining_lines() -> int:
        found = {(u.file, u.line) for c in changes for u in find_usages(args.path, c, args.exclude)}
        return len(found)

    # Endpoints with affected code, worked out before the fix: the tests must call each
    # of them for the result to count as fully verified. A removed endpoint cannot be
    # called on the new API, so it is reported by the re-scan instead.
    affected: list[Endpoint] = sorted(
        {
            (change.method, change.path)
            for change in changes
            if change.kind != REMOVED_ENDPOINT and find_usages(args.path, change, args.exclude)
        }
    )

    try:
        report, kept = apply_and_verify(
            edits,
            args.test,
            args.new,
            args.port,
            use_mock=not args.no_mock,
            allow_unverified=args.allow_unverified,
            recheck=remaining_lines,
            affected=affected,
            root=args.path,
        )
    except (FixError, VerifyError, UnsafePathError) as exc:
        code = fail(args, str(exc))
        print("No files were changed.", file=sys.stderr)
        return code

    out.update(result=report.verdict, files_changed=kept, verification=report_json(report, kept))
    print_report(report, args.new)
    return print_outcome(report, kept)


def print_skipped(skipped: list[Usage], min_confidence: str) -> None:
    """List lines apimend is not sure enough about to change on its own."""
    print(f"Not changed, because the confidence is below {min_confidence} (review these yourself):")
    for usage in skipped:
        print_usage(usage)
    if min_confidence == HIGH and any(usage.confidence == MEDIUM for usage in skipped):
        print("If the medium ones look right, re-run with --min-confidence medium.")


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
    if report.mock_used and report.affected:
        untested = report.untested_affected
        if untested:
            names = ", ".join(f"{method} {path}" for method, path in untested)
            print(f"  [missing] Coverage: your tests never called {names}")
        else:
            names = ", ".join(f"{method} {path}" for method, path in report.affected)
            print(f"  [passed] Coverage: your tests called every affected endpoint ({names})")
    if report.remaining:
        print(
            f"  [incomplete] Re-scan: {report.remaining} affected line(s) remain"
            " with the fix applied, for you to review"
        )
    else:
        print("  [passed] Re-scan: no affected code remains with the fix applied")
    print("  [not checked] Live API behaviour: authentication, rate limits, real data and errors")


def print_outcome(report: Report, kept: bool) -> int:
    """Explain the decision and return the exit code."""
    verdict = report.verdict
    if verdict == VERIFIED:
        print("\nVERIFIED against the new spec: the fix has been kept.")
    elif verdict == TESTS_ONLY:
        print("\nTESTS PASSED (requests not checked against the spec): the fix has been kept.")
    elif verdict == PARTIAL:
        return print_partial(report, kept)
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


def print_partial(report: Report, kept: bool) -> int:
    """Explain a partial verification. Always exit code 1: work remains."""
    untested = ", ".join(f"{method} {path}" for method, path in report.untested_changed)
    if not kept:
        print(f"\nPARTIALLY VERIFIED: your tests never called {untested}, which the fix changes,")
        print("so there is no evidence the change is right. The fix was undone.")
        print("Add a test that calls it, or re-run with --allow-unverified to keep the fix.")
        return 1
    if untested:
        print("\nPARTIALLY VERIFIED: kept anyway because --allow-unverified was used,")
        print(f"although your tests never called {untested}, which the fix changes.")
    else:
        print("\nPARTIALLY VERIFIED: every request the fix changed was tested and matched")
        print("the new spec, so the fix has been kept. It is not complete yet:")
    if report.remaining:
        print(f"  - {report.remaining} affected line(s) remain: run 'apimend scan' to see them")
    for method, path in report.untested_affected:
        print(f"  - your tests never called {method} {path}")
    print("Review the change with 'git diff' before committing it.")
    return 1


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
    scan.add_argument(
        "--min-confidence",
        choices=CONFIDENCE_LEVELS,
        default=LOW,
        help="only show lines found with at least this confidence (default: low, show all)",
    )
    scan.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format: text for people, json for scripts and agents (default: text)",
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
    fix.add_argument(
        "--min-confidence",
        choices=CONFIDENCE_LEVELS,
        default=HIGH,
        help="only change lines found with at least this confidence (default: high)",
    )
    fix.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format: text for people, json for scripts and agents (default: text)",
    )
    fix.set_defaults(func=cmd_fix)
    return parser


def json_inputs(args: argparse.Namespace) -> dict[str, object]:
    """The arguments a command ran with, for the JSON document."""
    inputs: dict[str, object] = {
        "old_spec": args.old,
        "new_spec": args.new,
        "path": args.path,
        "exclude": list(args.exclude),
        "min_confidence": args.min_confidence,
    }
    if args.command == "fix":
        inputs.update(
            test_command=args.test,
            mock=not args.no_mock,
            port=args.port,
            allow_unverified=args.allow_unverified,
        )
    return inputs


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.out = {}
    if getattr(args, "format", "text") != "json":
        return args.func(args)

    # JSON mode: standard output carries the JSON document and nothing else. The usual
    # text is discarded; error messages still go to standard error.
    args.out = document(args.command, json_inputs(args))
    with contextlib.redirect_stdout(io.StringIO()):
        code = args.func(args)
    args.out["exit_code"] = code
    print(json.dumps(args.out, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
