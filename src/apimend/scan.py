"""Find the lines of code affected by breaking API changes.

The scan compares two OpenAPI specs, works out which request properties were removed
or became required, and then looks at every request in your Python files
(`apimend.callsites`) to find the ones that are affected.

Every affected line gets a confidence level, so you know how much to trust it:

- high: proven. The line is part of a request to the changed endpoint, with the same
  HTTP method and exactly the same path.
- medium: probable. It belongs to a request to the endpoint, but the HTTP method could
  not be determined, or the path only partly matches.
- low: uncertain. apimend could not connect it to the request with certainty, for
  example because the request body is built in a way apimend cannot follow.

Requests that use a different HTTP method, and dictionaries that are proven to belong to
other requests, are not reported.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from apimend.callsites import NO_BODY, TRACED, CallSite, analyse

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache"}

REMOVED_PROPERTY = "removed-property"
NEW_REQUIRED_PROPERTY = "new-required-property"
REMOVED_ENDPOINT = "removed-endpoint"

HIGH, MEDIUM, LOW = "high", "medium", "low"
CONFIDENCE_LEVELS = (LOW, MEDIUM, HIGH)  # weakest first, so a higher index is stronger


class SpecError(Exception):
    """Raised when a spec file cannot be read or understood."""


@dataclass(frozen=True)
class Change:
    """One breaking change in an API's requests."""

    method: str
    path: str
    kind: str
    prop: str = ""
    hint: str = ""

    def describe(self) -> str:
        endpoint = f"{self.method} {self.path}"
        if self.kind == REMOVED_PROPERTY:
            return f"{endpoint}: request property '{self.prop}' was removed"
        if self.kind == NEW_REQUIRED_PROPERTY:
            return f"{endpoint}: request property '{self.prop}' is now required"
        return f"{endpoint}: endpoint was removed"


@dataclass(frozen=True)
class Usage:
    """A line of code that is affected by a change, and how sure apimend is about it."""

    file: Path
    line: int
    code: str
    confidence: str = HIGH
    reasons: tuple[str, ...] = ()  # why the confidence is not high


def confidence_rank(level: str) -> int:
    """Order confidence levels: low < medium < high."""
    return CONFIDENCE_LEVELS.index(level)


def load_spec(path: str | Path) -> dict:
    """Read an OpenAPI spec file (YAML or JSON) into a dictionary."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SpecError(f"cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"{path} is not valid YAML/JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        raise SpecError(f"{path} does not look like an OpenAPI spec (no 'paths' section)")
    return data


def _resolve(node: object, spec: dict) -> object:
    """Follow local '$ref' links such as '#/components/schemas/Order'."""
    for _ in range(20):  # a limit protects against reference loops
        if not (isinstance(node, dict) and isinstance(node.get("$ref"), str)):
            return node
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return {}
        target: object = spec
        for part in ref[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        node = target
    return {}


def _request_schema(operation: dict, spec: dict) -> dict:
    """Return the JSON request-body schema of an operation, or {} if it has none."""
    body = _resolve(operation.get("requestBody", {}), spec)
    if not isinstance(body, dict):
        return {}
    content = body.get("content", {})
    media = content.get("application/json", {}) if isinstance(content, dict) else {}
    schema = _resolve(media.get("schema", {}), spec) if isinstance(media, dict) else {}
    return schema if isinstance(schema, dict) else {}


def find_changes(old: dict, new: dict) -> list[Change]:
    """Compare two specs and list breaking changes to request bodies and endpoints."""
    changes: list[Change] = []
    for path, old_item in old["paths"].items():
        if not isinstance(old_item, dict):
            continue
        new_item = new["paths"].get(path)
        new_item = new_item if isinstance(new_item, dict) else {}
        for method in HTTP_METHODS:
            old_op = old_item.get(method)
            if not isinstance(old_op, dict):
                continue
            name = method.upper()
            new_op = new_item.get(method)
            if not isinstance(new_op, dict):
                changes.append(Change(name, path, REMOVED_ENDPOINT))
                continue

            old_schema = _request_schema(old_op, old)
            new_schema = _request_schema(new_op, new)
            old_props = set(old_schema.get("properties") or {})
            new_props = set(new_schema.get("properties") or {})
            old_required = set(old_schema.get("required") or [])
            new_required = set(new_schema.get("required") or [])

            removed = sorted(old_props - new_props)
            newly_required = sorted(new_required - old_required)
            brand_new = [p for p in newly_required if p not in old_props]

            # One property disappeared and one new required property appeared:
            # that is very likely a rename.
            rename = len(removed) == 1 and len(brand_new) == 1
            for prop in removed:
                hint = f"looks like it was renamed to '{brand_new[0]}'" if rename else ""
                changes.append(Change(name, path, REMOVED_PROPERTY, prop, hint))
            for prop in newly_required:
                is_new_name = rename and prop in brand_new
                hint = f"looks like the new name for '{removed[0]}'" if is_new_name else ""
                changes.append(Change(name, path, NEW_REQUIRED_PROPERTY, prop, hint))
    return changes


def _path_matches(text: str, path: str) -> bool:
    """True if a string in the code refers to this API path."""
    if "{" in path:  # e.g. /orders/{id}: match the fixed part before the parameter
        prefix = path.split("{", 1)[0]
        return len(prefix) > 1 and prefix in text
    return text == path or text.endswith(path)


def _python_files(root: Path, exclude: Iterable[str] = ()) -> list[Path]:
    """Python files under `root`, skipping tool folders and any `exclude` patterns."""
    files = []
    for file in root.rglob("*.py"):
        relative = file.relative_to(root)
        if SKIP_DIRS.intersection(relative.parts):
            continue
        if any(PurePosixPath(relative.as_posix()).match(pattern) for pattern in exclude):
            continue
        files.append(file)
    return sorted(files)


def _exact_path(text: str, path: str) -> bool:
    """True if a string names the path exactly (paths with {parameters} only partly match)."""
    return "{" not in path and _path_matches(text, path)


Grade = tuple[str, tuple[str, ...]]


def _grade(problems: list[str]) -> Grade:
    """Turn the checks that could not be confirmed into a confidence level."""
    level = HIGH if not problems else MEDIUM if len(problems) == 1 else LOW
    return level, tuple(problems)


def _endpoint_checks(call: CallSite, change: Change) -> list[str]:
    """What could not be confirmed about a request to the changed endpoint."""
    problems = []
    if not any(_exact_path(text, change.path) for text in call.paths):
        problems.append(f"the path only partly matches {change.path}")
    if not call.method:
        problems.append("apimend could not tell which HTTP method this request uses")
    return problems


def _rate_file(source: str, change: Change) -> list[tuple[int, Grade]]:
    """Every affected line in one file, with its confidence. Raises SyntaxError."""
    analysis = analyse(source)
    calls = [c for c in analysis.calls if any(_path_matches(t, change.path) for t in c.paths)]
    loose = [line for line, text in analysis.loose_strings if _path_matches(text, change.path)]
    # A request with another HTTP method goes to a different operation: not affected.
    relevant = [c for c in calls if not c.method or c.method == change.method]
    unclear = ("apimend found no request that uses it, so it cannot check what is sent",)
    found: list[tuple[int, Grade]] = []

    if change.kind == REMOVED_PROPERTY:
        for call in relevant:
            for line in call.keys.get(change.prop, []):
                found.append((line, _grade(_endpoint_checks(call, change))))
        # Only when a request's body cannot be fully traced can other dictionaries in
        # the file still reach it. Then report them as uncertain, never as proven.
        if loose or any(c.body not in (TRACED, NO_BODY) for c in relevant):
            reason = (
                f"'{change.prop}' is not in any request body apimend could trace, but a "
                f"request to {change.path} here sends data apimend cannot follow"
            )
            found += [
                (line, (LOW, (reason,))) for line, key in analysis.loose_keys if key == change.prop
            ]
    else:
        for call in relevant:
            problems = _endpoint_checks(call, change)
            if change.kind == NEW_REQUIRED_PROPERTY:
                if change.prop in call.keys:
                    continue  # this request already sends it
                if call.body not in (TRACED, NO_BODY):
                    problems.append(
                        f"apimend cannot see everything this request sends ({call.why}),"
                        f" so it may already include '{change.prop}'"
                    )
                    found.append((call.line, (LOW, tuple(problems))))
                    continue
            found.append((call.line, _grade(problems)))
        found += [(line, (LOW, (f"mentions {change.path}, but {unclear[0]}",))) for line in loose]
    return found


def find_usages(root: str | Path, change: Change, exclude: Iterable[str] = ()) -> list[Usage]:
    """Find lines in Python files under `root` that are affected by `change`.

    Each request in each file is checked on its own, so a property sent by one request
    never makes another request look correct. Files matching any of the `exclude` glob
    patterns (for example "tests/*") are skipped. Each line found gets a confidence
    level explaining how sure apimend is that it is affected.
    """
    usages: list[Usage] = []
    for file in _python_files(Path(root), exclude):
        try:
            source = file.read_text(encoding="utf-8")
            found = _rate_file(source, change)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue  # skip files we cannot read or parse
        best: dict[int, Grade] = {}
        for line, grade in found:  # a line can match more than once: keep the strongest
            if line not in best or confidence_rank(grade[0]) > confidence_rank(best[line][0]):
                best[line] = grade
        lines = source.splitlines()
        usages.extend(
            Usage(file, line, lines[line - 1].strip(), level, reasons)
            for line, (level, reasons) in sorted(best.items())
        )
    return usages
