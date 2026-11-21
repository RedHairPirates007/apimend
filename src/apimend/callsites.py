"""Find the HTTP requests in Python code, and the data each one sends.

A *call site* is a request in the code, such as:

    requests.post("/orders", json={"pizza": "margherita"})
    _request("POST", "/orders", body)

A call only counts as a request when there is evidence that it is one:

- a known HTTP client, recognised through the file's imports: `requests`, `httpx`,
  `aiohttp`, `urllib.request` and `http.client`, including their session and client
  objects (`s = requests.Session()`, `with httpx.Client() as c`, `self.session = ...`,
  or a parameter annotated `client: httpx.Client`);
- a wrapper function in the same file that passes one of its own parameters into the URL
  of such a request, like `_request(method, path, body)` above (proven, not guessed);
- a function you name with `--request-function` or in `pyproject.toml`.

Anything else, such as `logger.info("POST", "/orders", {...})`, is not a request. Calls to
other functions that pass an API path are listed separately, so the scan can say that it
could not tell whether they send requests.

For each request, apimend works out:

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

Only what happens *before* the request counts: a key added after `post(...)` was never
sent. A key added only in some cases (inside an `if`, or later in a loop that repeats
the request) may be sent, so it is marked as uncertain rather than proven.

Anything else (function parameters, other files, a dictionary changed with `update()`
or merged with `**`) is marked as not fully traced, so apimend reports it as uncertain
instead of guessing.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

METHODS = ("GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE")
BODY_KEYWORDS = ("json", "data", "body", "payload", "content")
URL_KEYWORDS = ("url", "path", "endpoint", "route", "uri")

# Known HTTP clients. Each style says where the method, URL and body are in the call.
_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")
CLIENT_FUNCTIONS = {
    **{f"{library}.{verb}": "verb" for library in ("requests", "httpx") for verb in _VERBS},
    "requests.request": "request",
    "httpx.request": "request",
    "httpx.stream": "request",
    "aiohttp.request": "request",
    "urllib.request.Request": "urllib",
    "urllib.request.urlopen": "urlopen",
}
CLIENT_CLASSES = {
    "requests.Session": "session",
    "requests.session": "session",
    "requests.sessions.Session": "session",
    "httpx.Client": "session",
    "httpx.AsyncClient": "session",
    "aiohttp.ClientSession": "session",
    "http.client.HTTPConnection": "connection",
    "http.client.HTTPSConnection": "connection",
}
CLIENT_METHODS = {
    "session": {**dict.fromkeys(_VERBS, "verb"), "request": "request", "stream": "request"},
    "connection": {"request": "connection"},
}
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}

# How completely a request body was traced
TRACED = "traced"  # every key it sends is known
PARTLY = "partly-traced"  # some keys are known, but more may be added
UNTRACED = "untraced"  # apimend cannot see what it sends
NO_BODY = "no-body"  # the request sends no body

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_MUTATORS = {"update", "setdefault", "pop", "popitem", "clear", "__setitem__"}
_MAX_DEPTH = 5  # how many variables or functions to follow before giving up
_BRANCHES = (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.Match, ast.ExceptHandler)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)

# When a line runs, compared with the request
BEFORE = "before"  # always runs before the request
MAYBE = "maybe"  # runs before the request only in some cases
AFTER = "after"  # runs after the request, so it cannot change what was sent


@dataclass
class CallSite:
    """One HTTP request in the code."""

    line: int
    method: str  # "POST", or "" when the code does not make it clear
    paths: list[str]  # the strings that make up the URL
    body: str  # TRACED, PARTLY, UNTRACED or NO_BODY
    keys: dict[str, list[int]] = field(default_factory=dict)  # body key -> lines setting it
    maybe: set[tuple[str, int]] = field(default_factory=set)  # (key, line) set only sometimes
    why: str = ""  # why the body is not fully traced


@dataclass
class UnknownCall:
    """A call that passes an API path to a function apimend does not recognise."""

    line: int
    function: str  # as written in the code, e.g. "send" or "self.api.call"
    paths: list[str]


@dataclass
class Analysis:
    """What apimend found in one Python file."""

    calls: list[CallSite]
    loose_strings: list[tuple[int, str]]  # strings not used as part of any request's URL
    loose_keys: list[tuple[int, str]]  # dictionary keys that are not part of a traced body
    unknown_calls: list[UnknownCall] = field(default_factory=list)


@dataclass
class _Parts:
    """Where the method, URL and body of one request are in the code."""

    method: str  # "POST", or "" when unknown
    url: list[ast.expr]
    body: ast.expr | None
    method_node: ast.expr | None = None  # where the method comes from, when not constant


@dataclass
class _Wrapper:
    """A function in this file that sends a request using its parameters."""

    is_method: bool  # defined in a class, so called as obj.name(...)
    params: list[str]  # without self or cls
    defaults: dict[str, ast.expr]
    url_params: list[str]  # parameters that become part of the URL
    method: str  # the HTTP method, when the wrapper always uses the same one
    method_param: str  # the parameter that holds the method, if any
    body_param: str  # the parameter that becomes the body, if any


@dataclass
class _Body:
    status: str
    keys: dict[str, list[int]] = field(default_factory=dict)
    nodes: set[int] = field(default_factory=set)  # ids of the key nodes
    maybe: set[tuple[str, int]] = field(default_factory=set)
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
        self.imports = self._find_imports()
        self.client_names: dict[str, str] = {}  # variable -> "session" or "connection"
        self.client_attrs: dict[str, str] = {}  # self.<attribute> -> kind
        self._find_clients()
        self.wrappers: dict[str, _Wrapper] = {}
        self._find_wrappers()

    # --- recognising requests ---------------------------------------------------

    def _find_imports(self) -> dict[str, str]:
        """Local name -> what it was imported as, e.g. {"rq": "requests"}."""
        names: dict[str, str] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        names[alias.asname] = alias.name
                    else:
                        top = alias.name.split(".")[0]
                        names[top] = top
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        return names

    def qualname(self, node: ast.expr) -> str:
        """The imported name an expression refers to, e.g. "requests.post", or ""."""
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) and node.id in self.imports:
            return ".".join([self.imports[node.id], *reversed(parts)])
        return ""

    def _client_kind(self, value: ast.AST | None) -> str:
        """ "session" or "connection" if `value` creates an HTTP client object."""
        if isinstance(value, ast.Call):
            return CLIENT_CLASSES.get(self.qualname(value.func), "")
        return ""

    def _annotation_kind(self, annotation: ast.expr | None) -> str:
        """The client kind of a type annotation such as httpx.Client | None, or ""."""
        for node in ast.walk(annotation) if annotation is not None else ():
            if isinstance(node, (ast.Name, ast.Attribute)):
                kind = CLIENT_CLASSES.get(self.qualname(node), "")
                if kind:
                    return kind
        return ""

    def _find_clients(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.arg) and self._annotation_kind(node.annotation):
                self.client_names[node.arg] = self._annotation_kind(node.annotation)
            pairs: list[tuple[ast.expr, ast.expr]] = []
            if isinstance(node, ast.Assign):
                pairs = [(target, node.value) for target in node.targets]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                pairs = [(node.target, node.value)]
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                pairs = [
                    (item.optional_vars, item.context_expr)
                    for item in node.items
                    if item.optional_vars is not None
                ]
            for target, value in pairs:
                kind = self._client_kind(value)
                if not kind:
                    continue
                if isinstance(target, ast.Name):
                    self.client_names[target.id] = kind
                elif _is_self_attribute(target):
                    assert isinstance(target, ast.Attribute)
                    self.client_attrs[target.attr] = kind

    def _receiver_kind(self, receiver: ast.expr) -> str:
        """The kind of client object a method is called on, or ""."""
        if isinstance(receiver, ast.Call):  # requests.Session().post(...)
            return self._client_kind(receiver)
        if isinstance(receiver, ast.Name):
            return self.client_names.get(receiver.id, "")
        if _is_self_attribute(receiver):
            assert isinstance(receiver, ast.Attribute)
            return self.client_attrs.get(receiver.attr, "")
        return ""

    def client_parts(self, call: ast.Call) -> _Parts | None:
        """The parts of a request made with a known HTTP client, or None."""
        func = call.func
        style = CLIENT_FUNCTIONS.get(self.qualname(func), "")
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if not style and isinstance(func, ast.Attribute):
            style = CLIENT_METHODS.get(self._receiver_kind(func.value), {}).get(func.attr, "")
        if not style:
            return None
        if style == "verb":
            body = _keyword(call, "json", "data", "content")
            if body is None and name in ("post", "put", "patch"):
                body = _positional(call, 1)  # requests.post(url, data)
            return _Parts(name.upper(), _present(_argument(call, 0, "url")), body)
        if style in ("request", "connection"):  # conn.request("POST", "/orders", body)
            method_node = _argument(call, 0, "method")
            if style == "request":
                body = _keyword(call, "json", "data", "content")
            else:
                body = _argument(call, 2, "body")
            urls = _present(_argument(call, 1, "url"))
            return _Parts(_constant_method(method_node), urls, body, method_node)
        url = _argument(call, 0, "url")
        if style == "urlopen" and self._is_request_object(url, self.scope_of(call)):
            return None  # urlopen(Request(...)): the Request is the request
        body = _argument(call, 1, "data")
        method_node = _keyword(call, "method")
        method = _constant_method(method_node)
        if method_node is None:
            method = "GET" if body is None or _is_none(body) else "POST"  # urllib's rule
        return _Parts(method, _present(url), body, method_node)

    def _is_request_object(self, node: ast.expr | None, scope: ast.AST) -> bool:
        """True if `node` is a urllib Request, directly or through a variable."""
        values: list[ast.expr] = [] if node is None else [node]
        if isinstance(node, ast.Name):
            values = self._assigned(node.id, scope) or self._assigned(node.id, self.tree)
        return any(
            isinstance(value, ast.Call) and self.qualname(value.func) == "urllib.request.Request"
            for value in values
        )

    def _assigned(self, name: str, scope: ast.AST) -> list[ast.expr]:
        """Every value assigned to `name` in a scope."""
        values: list[ast.expr] = []
        for node in self.own_nodes(scope):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            else:
                continue
            if any(isinstance(t, ast.Name) and t.id == name for t in targets) and node.value:
                values.append(node.value)
        return values

    def flows(self, node: ast.AST | None, scope: ast.AST, depth: int = 0) -> set[str]:
        """Names whose values flow into `node`, following local variables."""
        if node is None or depth > _MAX_DEPTH:
            return set()
        found: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id not in found:
                found.add(child.id)
                for value in self._assigned(child.id, scope):
                    found |= self.flows(value, scope, depth + 1)
        return found

    def _find_wrappers(self) -> None:
        """Find functions that send a request with their parameters (proven wrappers)."""
        candidates: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool]] = []
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                candidates.append((node, False))
            elif isinstance(node, ast.ClassDef):
                candidates += [
                    (item, True)
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
        names = [function.name for function, _ in candidates]
        ambiguous = {name for name in names if names.count(name) > 1}
        for _ in range(_MAX_DEPTH):  # a wrapper may call another wrapper
            found = False
            for function, is_method in candidates:
                if function.name in self.wrappers or function.name in ambiguous:
                    continue
                wrapper = self._as_wrapper(function, is_method)
                if wrapper:
                    self.wrappers[function.name] = wrapper
                    found = True
            if not found:
                break

    def _as_wrapper(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, is_method: bool
    ) -> _Wrapper | None:
        args = function.args
        positional = [*args.posonlyargs, *args.args]
        defaults = dict(zip([a.arg for a in positional[::-1]], args.defaults[::-1]))
        defaults.update(
            (a.arg, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
        )
        params = [a.arg for a in [*positional, *args.kwonlyargs]]
        static = any(getattr(d, "id", "") == "staticmethod" for d in function.decorator_list)
        if is_method and not static and params:
            params = params[1:]  # self or cls
        for node in self.own_nodes(function):
            if not isinstance(node, ast.Call):
                continue
            parts = self.client_parts(node) or self.wrapper_parts(node)
            if parts is None:
                continue
            into_url = set().union(*(self.flows(u, function) for u in parts.url))
            url_params = [p for p in params if p in into_url]
            if not url_params:
                continue  # the URL does not come from the caller
            method_param = ""
            if not parts.method:
                into_method = self.flows(parts.method_node, function)
                method_param = next((p for p in params if p in into_method), "")
            into_body = self.flows(parts.body, function)
            body_param = next((p for p in params if p in into_body), "")
            return _Wrapper(
                is_method, params, defaults, url_params, parts.method, method_param, body_param
            )
        return None

    def wrapper_parts(self, call: ast.Call) -> _Parts | None:
        """The parts of a call to a proven wrapper function in this file, or None."""
        func = call.func
        if isinstance(func, ast.Name):
            wrapper = self.wrappers.get(func.id)
            if wrapper is None or wrapper.is_method:
                return None
        elif isinstance(func, ast.Attribute):
            wrapper = self.wrappers.get(func.attr)
            if wrapper is None or not wrapper.is_method:
                return None
        else:
            return None
        if any(isinstance(a, ast.Starred) for a in call.args):
            return None  # arguments cannot be matched to parameters
        bound = {**wrapper.defaults, **dict(zip(wrapper.params, call.args))}
        bound.update((k.arg, k.value) for k in call.keywords if k.arg in wrapper.params)
        method_node = bound.get(wrapper.method_param) if wrapper.method_param else None
        method = wrapper.method or _constant_method(method_node)
        url = [bound[p] for p in wrapper.url_params if p in bound]
        body = bound.get(wrapper.body_param) if wrapper.body_param else None
        body = None if body is None or _is_none(body) else body
        return _Parts(method, url, body, method_node)

    def configured_parts(self, call: ast.Call, functions: frozenset[str]) -> _Parts | None:
        """The parts of a call to a function the user named as a request function."""
        written = _written(call.func)
        if not any(written == name or written.endswith(f".{name}") for name in functions):
            return None
        args = [arg for arg in call.args if not _is_method_name(arg)]
        url = [args[0]] if args else []
        url += [k.value for k in call.keywords if k.arg in URL_KEYWORDS]
        body = next((k.value for k in call.keywords if k.arg in BODY_KEYWORDS), None)
        if body is None and len(args) > 1:
            body = args[1]
        return _Parts(self.method_of(call), url, body)

    def is_logging(self, call: ast.Call) -> bool:
        """True for print(), logging and warnings: these never send API requests."""
        func = call.func
        qualname = self.qualname(func)
        if qualname.startswith("logging.") or qualname == "warnings.warn":
            return True
        if isinstance(func, ast.Name):
            return func.id == "print"
        if isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS:
            return "log" in _written(func.value).lower()
        return False

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

    def _ancestors(self, node: ast.AST, scope: ast.AST) -> list[ast.AST]:
        """The nodes around `node`, from the inside out, stopping at its scope."""
        found = []
        current = self.parents.get(node)
        while current is not None and current is not scope:
            found.append(current)
            current = self.parents.get(current)
        return found

    def timing(self, node: ast.AST, at: ast.AST | None, scope: ast.AST) -> str:
        """When `node` runs compared with `at` (the request): BEFORE, MAYBE or AFTER."""
        if at is None or self.scope_of(at) is not scope:
            # No request to compare with, or a module-level value used inside a
            # function: the module has finished running before the function is called.
            return BEFORE
        around_request = {id(a) for a in self._ancestors(at, scope)}
        around_node = self._ancestors(node, scope)
        here = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        if here < (getattr(at, "lineno", 0), getattr(at, "col_offset", 0)):
            only_sometimes = any(
                isinstance(a, _BRANCHES) and id(a) not in around_request for a in around_node
            )
            return MAYBE if only_sometimes else BEFORE
        # Written after the request: it only matters if a loop runs the request again.
        repeats = any(isinstance(a, _LOOPS) and id(a) in around_request for a in around_node)
        return MAYBE if repeats else AFTER

    def resolve(
        self, name: str, scope: ast.AST, at: ast.AST | None = None
    ) -> tuple[ast.expr | None, ast.AST, str]:
        """The value a name has when `at` runs, if it was clearly assigned once.

        Looks in the function first, then the module. Assignments after `at` are
        ignored. Returns the value, the scope it was found in, and a reason when it
        cannot be resolved.
        """
        if name in self._parameters(scope):
            return None, scope, f"'{name}' is passed in from outside the function"
        for where in (scope,) if scope is self.tree else (scope, self.tree):
            values: list[tuple[ast.expr | None, str]] = []
            for node in self.own_nodes(where):
                if isinstance(node, ast.Assign):
                    if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                        values.append((node.value, self.timing(node, at, where)))
                elif isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name) and node.target.id == name:
                        values.append((node.value, self.timing(node, at, where)))
                elif (
                    isinstance(node, ast.AugAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == name
                ):
                    values.append((None, self.timing(node, at, where)))  # x |= ...
            values = [(value, when) for value, when in values if when != AFTER]
            if len(values) == 1 and values[0][0] is not None:
                if values[0][1] == MAYBE:
                    return None, where, f"'{name}' is only assigned in some cases"
                return values[0][0], where, ""
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

    def trace_body(
        self, node: ast.expr | None, scope: ast.AST, at: ast.AST | None, depth: int = 0
    ) -> _Body:
        """Work out which keys a request body contains when `at` (the request) runs."""
        if node is None or _is_none(node):
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
            value, where, why = self.resolve(node.id, scope, at)
            if value is None:
                return _Body(UNTRACED, why=why)
            body = self.trace_body(value, where, at, depth + 1)
            self._add_changes(body, node.id, where, at)
            return body
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "encode":  # json.dumps(body).encode()
                return self.trace_body(node.func.value, scope, at, depth + 1)
            if node.func.attr == "dumps" and node.args:  # json.dumps(body)
                return self.trace_body(node.args[0], scope, at, depth + 1)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            function = self.functions.get(node.func.id)
            if function is None:
                return _Body(UNTRACED, why=f"it comes from {node.func.id}(), outside this file")
            returns = [n for n in self.own_nodes(function) if isinstance(n, ast.Return)]
            if len(returns) != 1 or returns[0].value is None:
                return _Body(UNTRACED, why=f"{node.func.id}() does not have one clear return")
            # Inside the helper, what matters is what happens before it returns.
            return self.trace_body(returns[0].value, function, returns[0], depth + 1)
        return _Body(UNTRACED, why="apimend cannot follow how the body is built")

    def _add_changes(self, body: _Body, name: str, scope: ast.AST, at: ast.AST | None) -> None:
        """Include keys added before `at`, such as body["size"] = 1, and other changes."""
        for node in self.own_nodes(scope):
            stores_key = (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name)
                and node.value.id == name
            )
            mutates = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == name
                and node.func.attr in _MUTATORS
            )
            if not (stores_key or mutates):
                continue
            when = self.timing(node, at, scope)
            if when == AFTER:
                continue  # changes made after the request cannot change what it sent
            if isinstance(node, ast.Subscript):
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    body.keys.setdefault(key.value, []).append(key.lineno)
                    body.nodes.add(id(key))
                    if when == MAYBE:
                        body.maybe.add((key.value, key.lineno))
                elif body.status == TRACED:
                    body.status, body.why = PARTLY, f"keys are added to '{name}' by name"
            elif body.status == TRACED and isinstance(node, ast.Call):
                attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
                body.status, body.why = PARTLY, f"'{name}' is changed with .{attr}()"

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


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_self_attribute(node: ast.AST) -> bool:
    """True for self.x or cls.x."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("self", "cls")
    )


def _written(node: ast.expr) -> str:
    """A function as written in the code, such as "self.api.send"."""
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):  # pragma: no cover - unusual syntax trees
        return ""


def _keyword(call: ast.Call, *names: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg in names), None)


def _positional(call: ast.Call, index: int) -> ast.expr | None:
    """The positional argument at `index`, or None if absent or after a *args."""
    for position, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred):
            return None
        if position == index:
            return arg
    return None


def _argument(call: ast.Call, index: int, *names: str) -> ast.expr | None:
    keyword = _keyword(call, *names)
    return keyword if keyword is not None else _positional(call, index)


def _present(node: ast.expr | None) -> list[ast.expr]:
    return [] if node is None else [node]


def _constant_method(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and str(node.value).upper() in METHODS:
        return str(node.value).upper()
    return ""


def _looks_like_api_path(text: str) -> bool:
    return text.startswith("/") or "://" in text


def analyse(source: str, request_functions: Iterable[str] = ()) -> Analysis:
    """Find every request in Python source, and what it sends. Raises SyntaxError.

    `request_functions` names your own functions that send requests, for wrappers
    apimend cannot prove on its own (for example because they are in another file).
    """
    module = _Module(ast.parse(source))
    configured = frozenset(request_functions)
    calls: list[CallSite] = []
    used_strings: set[int] = set()
    body_keys: set[int] = set()
    ignored: set[int] = set()  # strings and keys in logging calls: never API data
    others: list[ast.Call] = []

    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        parts = (
            module.client_parts(node)
            or module.wrapper_parts(node)
            or module.configured_parts(node, configured)
        )
        if parts is None:
            if module.is_logging(node):
                ignored.update(id(n) for arg in node.args for n in ast.walk(arg))
                ignored.update(id(n) for k in node.keywords for n in ast.walk(k.value))
            else:
                others.append(node)
            continue
        scope = module.scope_of(node)
        strings = [s for part in parts.url for s in module.strings(part, scope)]
        if not strings:
            continue  # not a request to a URL written in this file
        body = module.trace_body(parts.body, scope, node)
        used_strings.update(id(s) for s in strings)
        body_keys.update(body.nodes)
        calls.append(
            CallSite(
                line=node.lineno,
                method=parts.method,
                paths=[str(s.value) for s in strings],
                body=body.status,
                keys=body.keys,
                maybe=body.maybe,
                why=body.why,
            )
        )

    # Other calls that pass an API path: apimend cannot tell whether they send requests.
    unknown_calls = []
    for node in others:
        args = [arg for arg in node.args if not _is_method_name(arg)][:1]
        args += [k.value for k in node.keywords if k.arg in URL_KEYWORDS]
        scope = module.scope_of(node)
        found = [s for arg in args for s in module.strings(arg, scope, calls=False)]
        paths = [
            str(s.value)
            for s in found
            if id(s) not in used_strings and _looks_like_api_path(str(s.value))
        ]
        if paths:
            unknown_calls.append(UnknownCall(node.lineno, _written(node.func), paths))

    docstrings = {
        id(node.value)
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    loose_strings, loose_keys = [], []
    for node in ast.walk(module.tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in ignored:
            continue
        if id(node) not in used_strings and id(node) not in docstrings:
            loose_strings.append((node.lineno, node.value))
        parent = module.parents.get(node)
        is_key = (isinstance(parent, ast.Dict) and any(k is node for k in parent.keys)) or (
            isinstance(parent, ast.Subscript) and parent.slice is node
        )
        if is_key and id(node) not in body_keys:
            loose_keys.append((node.lineno, node.value))
    return Analysis(calls, loose_strings, loose_keys, unknown_calls)
