"""Find the lines of code affected by breaking API changes.

The scan compares two OpenAPI specs, works out which request properties were removed
or became required, and then searches Python files for code that uses them.

Every affected line gets a confidence level, so you know how much to trust it:

- high: every check passed (see `_grade`)
- medium: one check failed
- low: two or more checks failed, or the code calls a different HTTP method
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

HIGH, MEDIUM, LOW = "high", "medium", "low"
CONFIDENCE_LEVELS = (LOW, MEDIUM, HIGH)  # weakest first, so a higher index is stronger

_METHOD_NAMES = {method.upper() for method in HTTP_METHODS}
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


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


@dataclass(frozen=True)
class _Literal:
    """A string literal in Python code, with what apimend knows about where it sits."""

    line: int
    value: str
    scope: int  # the innermost function it is in (0 for module level)
    is_key: bool  # used as a dictionary key, e.g. {"size": ...} or body["size"]
    method: str  # HTTP method of the call it is passed to, "" if unknown


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Map every node in the tree to the node that contains it."""
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> int:
    """Identify the innermost function that contains `node` (0 for module level)."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, _FUNCTIONS):
            return id(current)
        current = parents.get(current)
    return 0


def _is_key(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True if a string literal is used as a dictionary key."""
    parent = parents.get(node)
    if isinstance(parent, ast.Dict):
        return any(key is node for key in parent.keys)
    return isinstance(parent, ast.Subscript) and parent.slice is node


def _call_method(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """The HTTP method of the call a string is passed to, or "" if it cannot be told.

    Understands calls such as requests.post("/orders") and request("POST", "/orders"),
    including paths built with f-strings or `+`.
    """
    current, parent = node, parents.get(node)
    while isinstance(parent, (ast.JoinedStr, ast.FormattedValue, ast.BinOp, ast.keyword)):
        current, parent = parent, parents.get(parent)
    if not isinstance(parent, ast.Call) or current is parent.func:
        return ""
    func = parent.func
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        name = ""
    if name.upper() in _METHOD_NAMES:
        return name.upper()
    for arg in [*parent.args, *(keyword.value for keyword in parent.keywords)]:
        if isinstance(arg, ast.Constant) and str(arg.value).upper() in _METHOD_NAMES:
            return str(arg.value).upper()
    return ""


def _literals(source: str) -> list[_Literal]:
    """All string literals in Python source (comments are not literals, so are excluded)."""
    tree = ast.parse(source)
    parents = _parents(tree)
    return [
        _Literal(
            node.lineno,
            node.value,
            _scope(node, parents),
            _is_key(node, parents),
            _call_method(node, parents),
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _exact_path(text: str, path: str) -> bool:
    """True if a string names the path exactly (paths with {parameters} only partly match)."""
    return "{" not in path and _path_matches(text, path)


def _grade(problems: list[str], wrong_method: bool) -> tuple[str, tuple[str, ...]]:
    """Turn the checks that failed into a confidence level."""
    if wrong_method or len(problems) >= 2:
        return LOW, tuple(problems)
    return (MEDIUM if problems else HIGH), tuple(problems)


def _rate_endpoint_call(call: _Literal, change: Change) -> tuple[str, tuple[str, ...]]:
    """How sure we are that a line calling the endpoint is affected by `change`."""
    problems = []
    if not _exact_path(call.value, change.path):
        problems.append(f"the path only partly matches {change.path}")
    wrong_method = bool(call.method) and call.method != change.method
    if wrong_method:
        problems.append(f"this line calls {call.method} {change.path}, not {change.method}")
    return _grade(problems, wrong_method)


def _rate_property(
    use: _Literal, calls: list[_Literal], change: Change
) -> tuple[str, tuple[str, ...]]:
    """How sure we are that a line using a removed property is affected by `change`."""
    problems = []
    nearby = [call for call in calls if call.scope == use.scope]
    if not nearby:
        problems.append(f"'{change.prop}' is not in the same function as the call to {change.path}")
        nearby = calls
    if not use.is_key:
        problems.append(f"'{change.prop}' is not used as a dictionary key")
    if not any(_exact_path(call.value, change.path) for call in nearby):
        problems.append(f"the path only partly matches {change.path}")
    methods = {call.method for call in nearby}
    wrong_method = "" not in methods and change.method not in methods
    if wrong_method:
        used = ", ".join(sorted(methods))
        problems.append(f"the nearby call uses {used} {change.path}, not {change.method}")
    return _grade(problems, wrong_method)


def find_usages(root: str | Path, change: Change, exclude: Iterable[str] = ()) -> list[Usage]:
    """Find lines in Python files under `root` that are affected by `change`.

    Only files that mention the endpoint's path are searched, to avoid matching
    unrelated code that happens to use the same word. Files matching any of the
    `exclude` glob patterns (for example "tests/*") are skipped. Each line found
    gets a confidence level explaining how sure apimend is that it is affected.
    """
    usages: list[Usage] = []
    for file in _python_files(Path(root), exclude):
        try:
            source = file.read_text(encoding="utf-8")
            literals = _literals(source)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue  # skip files we cannot read or parse
        calls = [lit for lit in literals if _path_matches(lit.value, change.path)]
        if not calls:
            continue
        rated: list[tuple[int, tuple[str, tuple[str, ...]]]] = []
        if change.kind == REMOVED_PROPERTY:
            rated = [
                (lit.line, _rate_property(lit, calls, change))
                for lit in literals
                if lit.value == change.prop
            ]
        elif change.kind == NEW_REQUIRED_PROPERTY:
            if any(lit.value == change.prop for lit in literals):
                continue  # this file already sends the property
            rated = [(call.line, _rate_endpoint_call(call, change)) for call in calls]
        else:  # removed endpoint: every call to it is affected
            rated = [(call.line, _rate_endpoint_call(call, change)) for call in calls]

        best: dict[int, tuple[str, tuple[str, ...]]] = {}
        for line, grade in rated:  # a line can match more than once: keep the strongest
            if line not in best or confidence_rank(grade[0]) > confidence_rank(best[line][0]):
                best[line] = grade
        lines = source.splitlines()
        usages.extend(
            Usage(file, line, lines[line - 1].strip(), level, reasons)
            for line, (level, reasons) in sorted(best.items())
        )
    return usages
