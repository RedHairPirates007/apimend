"""Machine-readable (JSON) output for `apimend scan` and `apimend fix`.

With `--format json`, apimend writes one JSON document to standard output and nothing
else, so CI jobs, scripts and AI agents can read the result without parsing text.

The document always has these fields:

- schema_version: bumped whenever a field is renamed or removed, so readers can check it.
- tool, command, inputs: what ran, and with which arguments.
- result: a short word for the outcome, for example "affected" or "verified".
- exit_code: the same exit code the command returns.
- error: only present when result is "error".

The rest depends on the command and is described in the README.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apimend import __version__
from apimend.fix import Edit
from apimend.scan import Change, Usage
from apimend.verify import Endpoint, RecordedRequest, Report

SCHEMA_VERSION = 1
TEST_OUTPUT_LINES = 50  # how much of the test output to include


def document(command: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Start the JSON document for one command."""
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "apimend", "version": __version__},
        "command": command,
        "inputs": inputs,
        "result": "",
    }


def path_json(file: Path) -> str:
    """File paths use forward slashes on every operating system."""
    return file.as_posix()


def change_json(change: Change) -> dict[str, Any]:
    return {
        "kind": change.kind,
        "method": change.method,
        "path": change.path,
        "property": change.prop or None,
        "description": change.describe(),
        "hint": change.hint or None,
        "rename": (
            {
                "partner": change.partner,
                "status": change.rename,
                "unconfirmed_because": list(change.rename_reasons),
            }
            if change.rename
            else None
        ),
    }


def usage_json(usage: Usage) -> dict[str, Any]:
    return {
        "file": path_json(usage.file),
        "line": usage.line,
        "code": usage.code,
        "confidence": usage.confidence,
        "reasons": list(usage.reasons),
    }


def unreadable_json(files: list[tuple[Path, str]]) -> list[dict[str, str]]:
    return [{"file": path_json(file), "reason": reason} for file, reason in files]


def endpoint_json(endpoint: Endpoint) -> dict[str, str]:
    method, path = endpoint
    return {"method": method, "path": path}


def endpoints_or_null(endpoints: list[Endpoint] | None) -> list[dict[str, str]] | None:
    return None if endpoints is None else [endpoint_json(e) for e in endpoints]


def edit_json(edit: Edit) -> dict[str, Any]:
    return {
        "file": path_json(edit.file),
        "line": edit.line,
        "before": edit.before,
        "after": edit.after,
        "endpoint": endpoint_json(edit.endpoint) if edit.endpoint[0] else None,
    }


def request_json(request: RecordedRequest) -> dict[str, Any]:
    return {
        "method": request.method,
        "path": request.path,
        "status": request.status,
        "matches_spec": request.matches_spec,
    }


def report_json(report: Report, kept: bool) -> dict[str, Any]:
    """Everything verification checked, so a reader can judge the evidence itself."""
    output_lines = report.test_output.strip().splitlines()
    # Without the mock no requests are recorded, so whether an endpoint was called is
    # unknown (null), not "untested".
    untested_affected = report.untested_affected if report.mock_used else None
    untested_changed = report.untested_changed if report.mock_used else None
    return {
        "verdict": report.verdict,
        "fix_kept": kept,
        "tests_passed": report.tests_passed,
        "mock_used": report.mock_used,
        "requests": [request_json(r) for r in report.requests],
        "mismatched_requests": len(report.mismatches),
        "affected_endpoints": [endpoint_json(e) for e in report.affected],
        "untested_affected_endpoints": endpoints_or_null(untested_affected),
        "changed_endpoints": [endpoint_json(e) for e in report.changed],
        "untested_changed_endpoints": endpoints_or_null(untested_changed),
        "remaining_lines": report.remaining,
        "test_output_tail": "\n".join(output_lines[-TEST_OUTPUT_LINES:]),
    }
