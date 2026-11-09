"""Fix code affected by breaking API changes, and keep a fix only when it is verified.

For now apimend fixes one kind of change automatically: a request property that was
renamed (for example `size` -> `pizza_size`). A fix is applied and checked by
`apimend.verify`; it is undone automatically unless the evidence supports keeping it.
"""

from __future__ import annotations

import ast
import difflib
import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from apimend.scan import NEW_REQUIRED_PROPERTY, REMOVED_PROPERTY, Change, find_usages
from apimend.verify import TESTS_ONLY, UNVERIFIED, VERIFIED, Report, verify

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
                start, end = node.col_offset, node.end_col_offset
                if end is None or node.end_lineno != node.lineno:
                    continue  # only literals that sit on a single line
                text, _ = _split_ending(source_lines[node.lineno - 1])
                raw = text.encode("utf-8")[start:end].decode("utf-8")
                # Only touch simple literals like "size" or 'size' (no prefixes or escapes).
                if raw[:1] in QUOTES and raw == f"{raw[0]}{rename.old}{raw[0]}":
                    replacement = f"{raw[0]}{rename.new}{raw[0]}"
                    key = (file, node.lineno)
                    span = (start, end, replacement)
                    replacements.setdefault(key, []).append(span)

    edits = []
    for (file, line), spans in sorted(replacements.items()):
        text, _ = _split_ending(_read_lines(file)[line - 1])
        data = text.encode("utf-8")  # ast column offsets count bytes, not characters
        for start, end, replacement in sorted(spans, reverse=True):  # right to left
            data = data[:start] + replacement.encode("utf-8") + data[end:]
        edits.append(Edit(file, line, text, data.decode("utf-8")))
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


def apply_and_verify(
    edits: list[Edit],
    test_command: str,
    spec: str | Path,
    port: int,
    use_mock: bool = True,
    allow_unverified: bool = False,
    recheck: Callable[[], int] | None = None,
) -> tuple[Report, bool]:
    """Apply `edits`, verify them, and undo them unless the evidence supports keeping them.

    Returns the verification report and whether the fix was kept.
    """
    originals = apply_edits(edits)
    try:
        report = verify(test_command, spec, port, use_mock=use_mock, recheck=recheck)
    except BaseException:
        restore(originals)
        raise
    keep = report.verdict in (VERIFIED, TESTS_ONLY) or (
        report.verdict == UNVERIFIED and allow_unverified
    )
    if not keep:
        restore(originals)
    return report, keep
