"""Fix code affected by breaking API changes, and keep a fix only when it is verified.

For now apimend fixes one kind of change automatically: a request property that was
renamed (for example `size` -> `pizza_size`). By default only lines found with high
confidence are changed. A fix is applied and checked by `apimend.verify`; it is undone
automatically unless the evidence supports keeping it.
"""

from __future__ import annotations

import ast
import difflib
import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from apimend.safety import check_writable, write_file
from apimend.scan import (
    HIGH,
    NEW_REQUIRED_PROPERTY,
    REMOVED_PROPERTY,
    Change,
    Usage,
    confidence_rank,
    find_usages,
)
from apimend.verify import PARTIAL, TESTS_ONLY, UNVERIFIED, VERIFIED, Endpoint, Report, verify

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
    endpoint: tuple[str, str] = ("", "")  # (method, path) of the request this edit fixes


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


def rename_usages(
    changes: Iterable[Change], root: str | Path, exclude: Iterable[str] = ()
) -> list[tuple[Rename, Usage]]:
    """Every line that uses a renamed property, paired with its rename."""
    found: list[tuple[Rename, Usage]] = []
    for rename in find_renames(changes):
        removed = Change(rename.method, rename.path, REMOVED_PROPERTY, rename.old)
        found.extend((rename, usage) for usage in find_usages(root, removed, exclude))
    return found


def plan_fixes(
    changes: Iterable[Change],
    root: str | Path,
    exclude: Iterable[str] = (),
    min_confidence: str = HIGH,
) -> list[Edit]:
    """Work out which lines to change to fix renamed request properties.

    Only lines found with at least `min_confidence` are changed.
    """
    # (file, line) -> list of (start byte, end byte, replacement text)
    replacements: dict[tuple[Path, int], list[tuple[int, int, str]]] = {}
    endpoints: dict[tuple[Path, int], tuple[str, str]] = {}  # which request each line belongs to
    by_rename: dict[Rename, dict[Path, set[int]]] = {}
    for rename, usage in rename_usages(changes, root, exclude):
        if confidence_rank(usage.confidence) < confidence_rank(min_confidence):
            continue  # not sure enough: leave the line for a person to review
        lines_by_file = by_rename.setdefault(rename, {})
        lines_by_file.setdefault(usage.file, set()).add(usage.line)
    for rename, lines_by_file in by_rename.items():
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
                    endpoints.setdefault(key, (rename.method, rename.path))

    edits = []
    for (file, line), spans in sorted(replacements.items()):
        text, _ = _split_ending(_read_lines(file)[line - 1])
        data = text.encode("utf-8")  # ast column offsets count bytes, not characters
        for start, end, replacement in sorted(spans, reverse=True):  # right to left
            data = data[:start] + replacement.encode("utf-8") + data[end:]
        edits.append(Edit(file, line, text, data.decode("utf-8"), endpoints[(file, line)]))
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


def apply_edits(edits: list[Edit], root: str | Path) -> dict[Path, bytes]:
    """Write the edits to disk and return the original file contents, for undoing.

    Every file is checked before anything is written: if any of them is a symbolic link
    or is not inside `root`, nothing is changed at all.
    """
    for file in {edit.file for edit in edits}:
        check_writable(root, file)
    new_contents = _new_contents(edits)
    originals = {file: file.read_bytes() for file in new_contents}
    try:
        for file, text in new_contents.items():
            write_file(root, file, text.encode("utf-8"))
    except BaseException:
        restore(originals, root)
        raise
    return originals


def restore(originals: dict[Path, bytes], root: str | Path) -> None:
    """Put files back exactly as they were."""
    for file, data in originals.items():
        write_file(root, file, data)


def apply_and_verify(
    edits: list[Edit],
    test_command: str,
    spec: str | Path,
    port: int,
    use_mock: bool = True,
    allow_unverified: bool = False,
    recheck: Callable[[], int] | None = None,
    affected: list[Endpoint] | None = None,
    *,
    root: str | Path,
) -> tuple[Report, bool]:
    """Apply `edits`, verify them, and undo them unless the evidence supports keeping them.

    A fix is kept when it is verified, or when it is partially verified but every
    request it changed was tested (the rest of the code still needs attention, but the
    change itself is proven). Returns the verification report and whether it was kept.
    """
    changed = sorted({edit.endpoint for edit in edits if edit.endpoint != ("", "")})
    originals = apply_edits(edits, root)
    try:
        report = verify(
            test_command,
            spec,
            port,
            use_mock=use_mock,
            recheck=recheck,
            affected=affected,
            changed=changed,
        )
    except BaseException:
        restore(originals, root)
        raise
    proven = report.verdict in (VERIFIED, TESTS_ONLY) or (
        report.verdict == PARTIAL and not report.untested_changed
    )
    keep = proven or (report.verdict in (UNVERIFIED, PARTIAL) and allow_unverified)
    if not keep:
        restore(originals, root)
    return report, keep
