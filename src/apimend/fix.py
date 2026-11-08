"""Fix code affected by breaking API changes, and prove each fix with tests.

For now apimend fixes one kind of change automatically: a request property that was
renamed (for example `size` -> `pizza_size`). A fix is applied, the project's tests are
run against a mock of the new API, and the fix is undone automatically if they fail.
"""

from __future__ import annotations

import ast
import difflib
import io
import os
import shlex
import shutil
import socket
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from apimend.scan import NEW_REQUIRED_PROPERTY, REMOVED_PROPERTY, Change, find_usages

QUOTES = ("'", '"')


class FixError(Exception):
    """Raised when a fix cannot be applied or verified because of a setup problem."""


@dataclass(frozen=True)
class Rename:
    """A request property that was renamed in the new version of the API."""

    method: str
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Edit:
    """A change to one line of one file."""

    file: Path
    line: int
    before: str
    after: str


def find_renames(changes: Iterable[Change]) -> list[Rename]:
    """Pair each removed property with the new property that most likely replaced it."""
    changes = list(changes)
    renames = []
    for removed in changes:
        if removed.kind != REMOVED_PROPERTY or not removed.hint:
            continue
        candidates = [
            added.prop
            for added in changes
            if added.kind == NEW_REQUIRED_PROPERTY
            and added.hint
            and (added.method, added.path) == (removed.method, removed.path)
        ]
        if len(candidates) == 1:  # only fix when the rename is unambiguous
            renames.append(Rename(removed.method, removed.path, removed.prop, candidates[0]))
    return renames


def _read_lines(file: Path) -> list[str]:
    """A file's lines with their original line endings, numbered the way Python's AST is."""
    # read_bytes + decode keeps Windows line endings (\r\n); read_text would convert them.
    return io.StringIO(file.read_bytes().decode("utf-8"), newline="").readlines()


def _split_ending(line: str) -> tuple[str, str]:
    """Split a line into its text and its line ending."""
    text = line.rstrip("\r\n")
    return text, line[len(text) :]


def _key_literals(tree: ast.AST, name: str, lines: set[int]) -> list[ast.Constant]:
    """String literals equal to `name` used as dictionary keys or subscripts on `lines`."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [key for key in node.keys if key is not None]
        elif isinstance(node, ast.Subscript):
            keys = [node.slice]
        else:
            continue
        for key in keys:
            if (
                isinstance(key, ast.Constant)
                and key.value == name
                and key.lineno in lines
                and key.end_lineno == key.lineno
            ):
                found.append(key)
    return found


def plan_fixes(
    changes: Iterable[Change], root: str | Path, exclude: Iterable[str] = ()
) -> list[Edit]:
    """Work out which lines to change to fix renamed request properties."""
    # (file, line) -> list of (start byte, end byte, replacement text)
    replacements: dict[tuple[Path, int], list[tuple[int, int, str]]] = {}
    for rename in find_renames(changes):
        removed = Change(rename.method, rename.path, REMOVED_PROPERTY, rename.old)
        lines_by_file: dict[Path, set[int]] = {}
        for usage in find_usages(root, removed, exclude):
            lines_by_file.setdefault(usage.file, set()).add(usage.line)
        for file, lines in lines_by_file.items():
            tree = ast.parse(file.read_text(encoding="utf-8"))
            source_lines = _read_lines(file)
            for node in _key_literals(tree, rename.old, lines):
                text, _ = _split_ending(source_lines[node.lineno - 1])
                raw = text.encode("utf-8")[node.col_offset : node.end_col_offset].decode("utf-8")
                # Only touch simple literals like "size" or 'size' (no prefixes or escapes).
                if raw[:1] in QUOTES and raw == f"{raw[0]}{rename.old}{raw[0]}":
                    replacement = f"{raw[0]}{rename.new}{raw[0]}"
                    key = (file, node.lineno)
                    span = (node.col_offset, node.end_col_offset, replacement)
                    replacements.setdefault(key, []).append(span)

    edits = []
    for (file, line), spans in sorted(replacements.items()):
        text, _ = _split_ending(_read_lines(file)[line - 1])
        raw = text.encode("utf-8")
        for start, end, replacement in sorted(spans, reverse=True):  # right to left
            raw = raw[:start] + replacement.encode("utf-8") + raw[end:]
        edits.append(Edit(file, line, text, raw.decode("utf-8")))
    return edits


def _new_contents(edits: list[Edit]) -> dict[Path, str]:
    """The full new text of every file touched by `edits`."""
    contents: dict[Path, list[str]] = {}
    for edit in edits:
        lines = contents.setdefault(edit.file, _read_lines(edit.file))
        text, ending = _split_ending(lines[edit.line - 1])
        if text != edit.before:
            raise FixError(f"{edit.file}:{edit.line} changed since the fix was planned")
        lines[edit.line - 1] = edit.after + ending
    return {file: "".join(lines) for file, lines in contents.items()}


def render_diff(edits: list[Edit]) -> str:
    """A unified diff (the format `git diff` uses) of the planned edits."""
    parts = []
    for file, new_text in _new_contents(edits).items():
        old_lines = _read_lines(file)
        new_lines = io.StringIO(new_text, newline="").readlines()
        for line in difflib.unified_diff(old_lines, new_lines, f"a/{file}", f"b/{file}"):
            if not line.endswith("\n"):  # last line of a file without a final newline
                line += "\n\\ No newline at end of file\n"
            parts.append(line)
    return "".join(parts)


def apply_edits(edits: list[Edit]) -> dict[Path, bytes]:
    """Write the edits to disk and return the original file contents, for undoing."""
    new_contents = _new_contents(edits)
    originals = {file: file.read_bytes() for file in new_contents}
    try:
        for file, text in new_contents.items():
            file.write_bytes(text.encode("utf-8"))
    except BaseException:
        restore(originals)
        raise
    return originals


def restore(originals: dict[Path, bytes]) -> None:
    """Put files back exactly as they were."""
    for file, data in originals.items():
        file.write_bytes(data)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start_mock(spec: str | Path, port: int, timeout: float = 60.0) -> subprocess.Popen:
    """Start Prism as a mock of `spec` and wait until it accepts connections."""
    if shutil.which("prism") is None:
        raise FixError("Prism is not installed. Run 'apimend doctor' for help.")
    if _port_in_use(port):
        raise FixError(f"port {port} is already in use. Stop that program or use --port.")
    process = subprocess.Popen(
        ["prism", "mock", str(spec), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise FixError(f"Prism stopped unexpectedly. Is {spec} a valid spec?")
        if _port_in_use(port):
            return process
        time.sleep(0.3)
    stop_mock(process)
    raise FixError(f"Prism did not start within {timeout:.0f} seconds")


def stop_mock(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_tests(command: str, port: int, timeout: float = 600.0) -> tuple[bool, str]:
    """Run the project's test command. Returns (passed, output)."""
    env = {**os.environ, "APIMEND_MOCK_URL": f"http://127.0.0.1:{port}"}
    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FixError(f"test command not found: {exc.filename}") from exc
    except subprocess.TimeoutExpired:
        return False, f"tests did not finish within {timeout:.0f} seconds"
    return result.returncode == 0, result.stdout + result.stderr


def apply_and_verify(
    edits: list[Edit], test_command: str, spec: str | Path, port: int, use_mock: bool = True
) -> tuple[bool, str]:
    """Apply `edits`, run the tests (against a mock of `spec`), and undo on failure."""
    originals = apply_edits(edits)
    try:
        mock = start_mock(spec, port) if use_mock else None
        try:
            passed, output = run_tests(test_command, port)
        finally:
            if mock is not None:
                stop_mock(mock)
    except BaseException:
        restore(originals)
        raise
    if not passed:
        restore(originals)
    return passed, output
