"""Find the HTTP requests in Python code, and the data each one sends.

A *call site* is a request in the code, such as:

    requests.post("/orders", json={"pizza": "margherita"})
    _request("POST", "/orders", body)

For each call, apimend works out:

- the URL it calls (the first argument, or `url=`), following simple variables;
- the HTTP method, when the code makes it clear (`.post(...)`, or a "POST" argument);
- the request body, traced to the dictionary written in the code.

Tracing is deliberately simple and deterministic. It follows:

- a dictionary passed directly:          post("/orders", {"size": 1})
- a variable assigned once:              body = {"size": 1}; post("/orders", json=body)
- keys added to that variable:           body["size"] = 1
- a function in the same file that
  returns a dictionary:                  post("/orders", json=make_body(1))
- JSON encoding:                         Request(url, data=json.dumps(body).encode())

Anything else (function parameters, other files, a dictionary changed with `update()`
or merged with `**`) is marked as not fully traced, so apimend reports it as uncertain
instead of guessing.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field

METHODS = ("GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE")
BODY_KEYWORDS = ("json", "data", "body", "payload", "content")
URL_KEYWORDS = ("url", "path", "endpoint", "route", "uri")

# How completely a request body was traced
TRACED = "traced"  # every key it sends is known
PARTLY = "partly-traced"  # some keys are known, but more may be added
UNTRACED = "untraced"  # apimend cannot see what it sends
NO_BODY = "no-body"  # the request sends no body

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_MUTATORS = {"update", "setdefault", "pop", "popitem", "clear", "__setitem__"}
_MAX_DEPTH = 5  # how many variables or functions to follow before giving up


@dataclass
class CallSite:
    """One HTTP request in the code."""

    line: int
    method: str  # "POST", or "" when the code does not make it clear
    paths: list[str]  # the strings that make up the URL
    body: str  # TRACED, PARTLY, UNTRACED or NO_BODY
    keys: dict[str, list[int]] = field(default_factory=dict)  # body key -> lines setting it
    why: str = ""  # why the body is not fully traced


@dataclass
class Analysis:
    """What apimend found in one Python file."""

    calls: list[CallSite]
    loose_strings: list[tuple[int, str]]  # strings not used as part of any request's URL
    loose_keys: list[tuple[int, str]]  # dictionary keys that are not part of a traced body


@dataclass
class _Body:
    status: str
    keys: dict[str, list[int]] = field(default_factory=dict)
    nodes: set[int] = field(default_factory=set)  # ids of the key nodes
    why: str = ""


class _Module:
    """A parsed file, with helpers to look up where names get their values."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.parents = {
            child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }
        self.functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def scope_of(self, node: ast.AST) -> ast.AST:
        """The innermost function containing `node`, or the module."""
        current = self.parents.get(node)
        while current is not None and not isinstance(current, _FUNCTIONS):
            current = self.parents.get(current)
        return current if current is not None else self.tree

    @staticmethod
    def own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
        """Every node in a scope, without looking inside nested functions or classes."""
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            yield node
            if not isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
                stack.extend(ast.iter_child_nodes(node))

    @staticmethod
    def _parameters(scope: ast.AST) -> set[str]:
        if not isinstance(scope, _FUNCTIONS):
            return set()
        args = scope.args
        names = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        names += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
        return {arg.arg for arg in names}

    def resolve(self, name: str, scope: ast.AST) -> tuple[ast.expr | None, ast.AST, str]:
        """The value a name was given, if it was assigned exactly once.

        Looks in the function first, then the module. Returns the value, the scope it
        was found in, and a reason when it cannot be resolved.
        """
        if name in self._parameters(scope):
            return None, scope, f"'{name}' is passed in from outside the function"
        for where in (scope,) if scope is self.tree else (scope, self.tree):
            values: list[ast.expr | None] = []
            for node in self.own_nodes(where):
                if isinstance(node, ast.Assign):
                    if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                        values.append(node.value)
                elif (
                    isinstance(node, (ast.AnnAssign, ast.AugAssign))
                    and isinstance(node.target, ast.Name)
                    and node.target.id == name
                ):
                    values.append(None)  # annotated or changed in place: not followed
            if len(values) == 1 and values[0] is not None:
                return values[0], where, ""
            if values:
                return None, where, f"'{name}' is assigned more than once"
        return None, scope, f"'{name}' is not assigned in this file"

    def strings(
        self, node: ast.AST | None, scope: ast.AST, depth: int = 0, calls: bool = True
    ) -> list[ast.Constant]:
        """The string literals that make up a value such as a URL.

        `calls` is False when following a variable: `req = Request("/orders")` is a
        request of its own, so `urlopen(req)` must not count as a second one.
        """
        if node is None or depth > _MAX_DEPTH:
            return []
        if isinstance(node, ast.Constant):
            return [node] if isinstance(node.value, str) else []
        if isinstance(node, ast.JoinedStr):  # f"/orders/{order_id}"
            return [part for part in node.values if isinstance(part, ast.Constant)]
        if isinstance(node, ast.BinOp):  # BASE + "/orders"
            return self.strings(node.left, scope, depth + 1, calls) + self.strings(
                node.right, scope, depth + 1, calls
            )
        if isinstance(node, ast.Name):  # URL = "/orders"; post(URL)
            value, where, _ = self.resolve(node.id, scope)
            return self.strings(value, where, depth + 1, calls=False)
        if isinstance(node, ast.Call) and calls:  # urljoin(BASE, "/orders")
            found: list[ast.Constant] = []
            for arg in node.args:
                found += self.strings(arg, scope, depth + 1)
            return found
        return []

    def trace_body(self, node: ast.expr | None, scope: ast.AST, depth: int = 0) -> _Body:
        """Work out which keys a request body contains."""
        if node is None:
            return _Body(NO_BODY)
        if depth > _MAX_DEPTH:
            return _Body(UNTRACED, why="the body is built through too many steps")
        if isinstance(node, ast.Dict):
            body = _Body(TRACED)
            for key in node.keys:
                if key is None:  # {**other, ...}
                    body.status, body.why = PARTLY, "it merges another dictionary with **"
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    body.keys.setdefault(key.value, []).append(key.lineno)
                    body.nodes.add(id(key))
            return body
        if isinstance(node, ast.Name):
            value, where, why = self.resolve(node.id, scope)
            if value is None:
                return _Body(UNTRACED, why=why)
            body = self.trace_body(value, where, depth + 1)
            self._add_changes(body, node.id, where)
            return body
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "encode":  # json.dumps(body).encode()
                return self.trace_body(node.func.value, scope, depth + 1)
            if node.func.attr == "dumps" and node.args:  # json.dumps(body)
                return self.trace_body(node.args[0], scope, depth + 1)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            function = self.functions.get(node.func.id)
            if function is None:
                return _Body(UNTRACED, why=f"it comes from {node.func.id}(), outside this file")
            returns = [n for n in self.own_nodes(function) if isinstance(n, ast.Return)]
            if len(returns) != 1 or returns[0].value is None:
                return _Body(UNTRACED, why=f"{node.func.id}() does not have one clear return")
            return self.trace_body(returns[0].value, function, depth + 1)
        return _Body(UNTRACED, why="apimend cannot follow how the body is built")

    def _add_changes(self, body: _Body, name: str, scope: ast.AST) -> None:
        """Include keys added later, such as body["size"] = 1, and notice other changes."""
        for node in self.own_nodes(scope):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name)
                and node.value.id == name
            ):
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    body.keys.setdefault(key.value, []).append(key.lineno)
                    body.nodes.add(id(key))
                elif body.status == TRACED:
                    body.status, body.why = PARTLY, f"keys are added to '{name}' by name"
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == name
                and node.func.attr in _MUTATORS
                and body.status == TRACED
            ):
                body.status, body.why = PARTLY, f"'{name}' is changed with .{node.func.attr}()"

    @staticmethod
    def method_of(call: ast.Call) -> str:
        """The HTTP method of a call, or "" when the code does not make it clear."""
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name.upper() in METHODS:
            return name.upper()
        candidates = [*call.args, *(k.value for k in call.keywords if k.arg == "method")]
        for arg in candidates:
            if isinstance(arg, ast.Constant) and str(arg.value).upper() in METHODS:
                return str(arg.value).upper()
        return ""


def _is_method_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and str(node.value).upper() in METHODS


def analyse(source: str) -> Analysis:
    """Find every request in Python source, and what it sends. Raises SyntaxError."""
    module = _Module(ast.parse(source))
    calls: list[CallSite] = []
    used_strings: set[int] = set()
    body_keys: set[int] = set()

    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        scope = module.scope_of(node)
        args = [arg for arg in node.args if not _is_method_name(arg)]
        url_parts = [args[0]] if args else []
        url_parts += [k.value for k in node.keywords if k.arg in URL_KEYWORDS]
        strings = [s for part in url_parts for s in module.strings(part, scope)]
        if not strings:
            continue  # not a request to a URL written in this file
        body_node = next((k.value for k in node.keywords if k.arg in BODY_KEYWORDS), None)
        if body_node is None and len(args) > 1:
            body_node = args[1]
        body = module.trace_body(body_node, scope)
        used_strings.update(id(s) for s in strings)
        body_keys.update(body.nodes)
        calls.append(
            CallSite(
                line=node.lineno,
                method=module.method_of(node),
                paths=[str(s.value) for s in strings],
                body=body.status,
                keys=body.keys,
                why=body.why,
            )
        )

    docstrings = {
        id(node.value)
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    loose_strings, loose_keys = [], []
    for node in ast.walk(module.tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) not in used_strings and id(node) not in docstrings:
            loose_strings.append((node.lineno, node.value))
        parent = module.parents.get(node)
        is_key = (isinstance(parent, ast.Dict) and any(k is node for k in parent.keys)) or (
            isinstance(parent, ast.Subscript) and parent.slice is node
        )
        if is_key and id(node) not in body_keys:
            loose_keys.append((node.lineno, node.value))
    return Analysis(calls, loose_strings, loose_keys)
