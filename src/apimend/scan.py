"""Find the lines of code affected by breaking API changes.

The scan compares two OpenAPI specs, works out which request properties were removed
or became required, and then searches Python files for code that uses them.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache"}

REMOVED_PROPERTY = "removed-property"
NEW_REQUIRED_PROPERTY = "new-required-property"
REMOVED_ENDPOINT = "removed-endpoint"


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
    """A line of code that is affected by a change."""

    file: Path
    line: int
    code: str


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


def _string_constants(source: str) -> list[tuple[int, str]]:
    """All string literals in Python source, with their line numbers (comments excluded)."""
    tree = ast.parse(source)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def find_usages(root: str | Path, change: Change, exclude: Iterable[str] = ()) -> list[Usage]:
    """Find lines in Python files under `root` that are affected by `change`.

    Only files that mention the endpoint's path are searched, to avoid matching
    unrelated code that happens to use the same word. Files matching any of the
    `exclude` glob patterns (for example "tests/*") are skipped.
    """
    usages: list[Usage] = []
    for file in _python_files(Path(root), exclude):
        try:
            source = file.read_text(encoding="utf-8")
            strings = _string_constants(source)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue  # skip files we cannot read or parse
        endpoint_lines = {ln for ln, text in strings if _path_matches(text, change.path)}
        if not endpoint_lines:
            continue
        if change.kind == REMOVED_PROPERTY:
            hits = {ln for ln, text in strings if text == change.prop}
        elif change.kind == NEW_REQUIRED_PROPERTY:
            if any(text == change.prop for _, text in strings):
                continue  # this file already sends the property
            hits = endpoint_lines  # every call to the endpoint must now send it
        else:  # removed endpoint: every call to it is affected
            hits = endpoint_lines
        lines = source.splitlines()
        usages.extend(Usage(file, ln, lines[ln - 1].strip()) for ln in sorted(hits))
    return usages
