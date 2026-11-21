"""apimend must never read or change files outside the folder it was given.

Each test reproduces an attack: a link inside the project that points outside it. The
file outside must stay exactly as it was.
"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

from apimend.cli import main
from apimend.fix import Edit, apply_edits
from apimend.safety import UnsafePathError, check_writable, unsafe_reason, write_file
from apimend.scan import REMOVED_PROPERTY, Change, find_usages, unreadable_files

LAB = Path(__file__).resolve().parent.parent / "lab" / "pizza-api"
CLIENT = (
    "import requests\n"
    "def order(pizza, size):\n"
    '    return requests.post("http://x/orders", json={"pizza": pizza, "size": size})\n'
)
SIZE_REMOVED = Change("POST", "/orders", REMOVED_PROPERTY, "size", "renamed")

needs_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="creating symbolic links needs extra rights on Windows"
)


def digest(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


@pytest.fixture
def attack(tmp_path):
    """A project folder, and a file outside it that the attacker wants changed."""
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    victim = outside / "victim.py"
    victim.write_text(CLIENT)
    return project, victim


def fix_in(project: Path, *extra: str) -> int:
    v1, v2 = str(LAB / "v1.yaml"), str(LAB / "v2.yaml")
    return main(["fix", v1, v2, str(project), "--no-mock", "--test", "true", *extra])


@needs_symlinks
def test_linked_file_outside_the_project_is_never_changed(attack, capsys):
    project, victim = attack
    (project / "link.py").symlink_to(victim)
    before = digest(victim)
    fix_in(project)
    assert digest(victim) == before
    out = capsys.readouterr().out
    assert "link.py: symbolic link (not followed, for safety)" in out


@needs_symlinks
def test_linked_folder_outside_the_project_is_never_entered(attack, capsys):
    project, victim = attack
    (project / "vendor").symlink_to(victim.parent, target_is_directory=True)
    before = digest(victim)
    fix_in(project)
    assert digest(victim) == before
    assert "vendor: symbolic link to a folder" in capsys.readouterr().out


@needs_symlinks
def test_links_are_reported_not_skipped_silently(attack):
    project, victim = attack
    (project / "link.py").symlink_to(victim)
    (project / "real.py").write_text(CLIENT)
    assert [u.file.name for u in find_usages(project, SIZE_REMOVED)] == ["real.py"]
    assert [(f.name, r) for f, r in unreadable_files(project)] == [
        ("link.py", "symbolic link (not followed, for safety)")
    ]


@needs_symlinks
def test_links_inside_the_project_are_not_followed_either(tmp_path):
    (tmp_path / "real.py").write_text(CLIENT)
    (tmp_path / "alias.py").symlink_to(tmp_path / "real.py")
    assert [u.file.name for u in find_usages(tmp_path, SIZE_REMOVED)] == ["real.py"]


@needs_symlinks
def test_file_swapped_for_a_link_after_planning_is_refused(attack):
    """The checks run again right before writing: a file replaced by a link is refused."""
    project, victim = attack
    client = project / "client.py"
    client.write_text(CLIENT)
    line = CLIENT.splitlines()[2]
    edit = Edit(client, 3, line, line.replace('"size"', '"pizza_size"'), ("POST", "/orders"))
    client.unlink()
    client.symlink_to(victim)  # swapped after the fix was planned
    before = digest(victim)
    with pytest.raises(UnsafePathError, match="symbolic link"):
        apply_edits([edit], project)
    assert digest(victim) == before


@needs_symlinks
def test_nothing_is_written_if_any_file_is_unsafe(attack):
    """All files are checked first, so one unsafe file means no file changes at all."""
    project, victim = attack
    good = project / "a_good.py"
    good.write_text(CLIENT)
    bad = project / "b_bad.py"
    bad.symlink_to(victim)
    line = CLIENT.splitlines()[2]
    fixed = line.replace('"size"', '"pizza_size"')
    edits = [Edit(good, 3, line, fixed), Edit(bad, 3, line, fixed)]
    with pytest.raises(UnsafePathError):
        apply_edits(edits, project)
    assert good.read_text() == CLIENT
    assert victim.read_text() == CLIENT


@needs_symlinks
def test_write_refuses_a_link_even_without_the_earlier_check(attack, monkeypatch):
    """The write itself never follows a link (the last line of defence)."""
    project, victim = attack
    link = project / "link.py"
    link.symlink_to(victim)
    monkeypatch.setattr("apimend.safety.check_writable", lambda root, file: None)
    with pytest.raises(UnsafePathError):
        write_file(project, link, b"changed")
    assert victim.read_text() == CLIENT


def test_hard_linked_files_are_not_changed(attack):
    project, victim = attack
    try:
        os.link(victim, project / "hard.py")
    except OSError:
        pytest.skip("hard links are not supported here")
    assert unsafe_reason(project, project / "hard.py") is None  # reading is fine
    with pytest.raises(UnsafePathError, match="hard links"):
        check_writable(project, project / "hard.py")


def test_paths_outside_the_project_are_refused(attack):
    project, victim = attack
    with pytest.raises(UnsafePathError):
        check_writable(project, project / ".." / "outside" / "victim.py")
    with pytest.raises(UnsafePathError):
        check_writable(project, victim)


def test_ordinary_files_are_still_fixed(attack, capsys):
    project, _ = attack
    (project / "client.py").write_text(CLIENT)
    assert fix_in(project) == 0
    assert '"pizza_size": size' in (project / "client.py").read_text()
