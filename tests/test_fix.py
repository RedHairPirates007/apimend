import shlex
import shutil
import socket
import sys
from pathlib import Path

import pytest

from apimend.cli import main
from apimend.fix import apply_edits, find_renames, plan_fixes, render_diff, restore
from apimend.scan import NEW_REQUIRED_PROPERTY, REMOVED_PROPERTY, Change, find_changes, load_spec

LAB = Path(__file__).resolve().parent.parent / "lab" / "pizza-api"
PYTHON = shlex.quote(sys.executable)
RENAME_CHANGES = [
    Change("POST", "/orders", REMOVED_PROPERTY, "size", "renamed"),
    Change("POST", "/orders", NEW_REQUIRED_PROPERTY, "pizza_size", "new name"),
]


@pytest.fixture
def lab(tmp_path):
    """A private copy of the Pizza API lab, so tests can change it safely."""
    return Path(shutil.copytree(LAB, tmp_path / "pizza-api"))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fix_args(lab, *extra):
    return ["fix", str(lab / "v1.yaml"), str(lab / "v2.yaml"), str(lab / "app"), *extra]


def test_find_renames_pairs_old_and_new_names():
    changes = find_changes(load_spec(LAB / "v1.yaml"), load_spec(LAB / "v2.yaml"))
    [rename] = find_renames(changes)
    assert (rename.method, rename.path, rename.old, rename.new) == (
        "POST",
        "/orders",
        "size",
        "pizza_size",
    )


def test_find_renames_skips_ambiguous_cases():
    extra = Change("POST", "/orders", NEW_REQUIRED_PROPERTY, "pizza_type", "new name")
    assert find_renames([*RENAME_CHANGES, extra]) == []


def test_plan_changes_only_the_dictionary_key(lab):
    [edit] = plan_fixes(RENAME_CHANGES, lab / "app")
    assert edit.file.name == "pizza_client.py"
    assert (
        edit.before.strip() == 'return _request("POST", "/orders", {"pizza": pizza, "size": size})'
    )
    assert edit.after.strip() == (
        'return _request("POST", "/orders", {"pizza": pizza, "pizza_size": size})'
    )


def test_keeps_quotes_line_endings_and_unicode(tmp_path):
    client = tmp_path / "client.py"
    original = "body = {'note': 'café', 'size': 1}\r\nbody['size'] = 2\r\npost('/orders', body)\r\n"
    client.write_bytes(original.encode("utf-8"))
    edits = plan_fixes(RENAME_CHANGES, tmp_path)
    originals = apply_edits(edits)
    expected = (
        "body = {'note': 'café', 'pizza_size': 1}\r\nbody['pizza_size'] = 2\r\n"
        "post('/orders', body)\r\n"
    )
    assert client.read_bytes() == expected.encode("utf-8")
    restore(originals)
    assert client.read_bytes() == original.encode("utf-8")


def test_does_not_touch_size_used_as_a_value(tmp_path):
    (tmp_path / "client.py").write_text('post("/orders", {"label": "size"})\n')
    assert plan_fixes(RENAME_CHANGES, tmp_path) == []


def test_preview_does_not_change_files(lab, capsys):
    before = (lab / "app" / "pizza_client.py").read_bytes()
    assert main(fix_args(lab)) == 1
    assert '+    return _request("POST", "/orders", {"pizza": pizza, "pizza_size": size})' in (
        capsys.readouterr().out
    )
    assert (lab / "app" / "pizza_client.py").read_bytes() == before


def test_failed_verification_undoes_the_fix(lab, capsys):
    before = (lab / "app" / "pizza_client.py").read_bytes()
    failing = f"{PYTHON} -c 'import sys; sys.exit(1)'"
    assert main(fix_args(lab, "--no-mock", "--test", failing)) == 1
    assert "NOT VERIFIED" in capsys.readouterr().out
    assert (lab / "app" / "pizza_client.py").read_bytes() == before


def test_passing_verification_keeps_the_fix(lab, capsys):
    passing = f"{PYTHON} -c 'pass'"
    assert main(fix_args(lab, "--no-mock", "--test", passing)) == 0
    assert "VERIFIED" in capsys.readouterr().out
    assert '"pizza_size": size' in (lab / "app" / "pizza_client.py").read_text()


def test_setup_problems_change_nothing(lab, capsys):
    before = (lab / "app" / "pizza_client.py").read_bytes()
    with socket.socket() as busy:  # occupy a port so the mock cannot start
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = str(busy.getsockname()[1])
        assert main(fix_args(lab, "--port", port, "--test", f"{PYTHON} -c 'pass'")) == 2
    assert "No files were changed." in capsys.readouterr().err
    assert (lab / "app" / "pizza_client.py").read_bytes() == before


@pytest.mark.skipif(shutil.which("prism") is None, reason="Prism not installed")
def test_end_to_end_fix_is_verified_against_a_mock_of_the_new_api(lab, monkeypatch):
    port = free_port()
    monkeypatch.setenv("PIZZA_API_URL", f"http://127.0.0.1:{port}")
    app = lab / "app"
    tests = f"{PYTHON} -m pytest {shlex.quote(str(app))} -q -p no:cacheprovider"
    assert main(fix_args(lab, "--port", str(port), "--test", tests)) == 0
    assert '"pizza_size": size' in (app / "pizza_client.py").read_text()
    scan = ["scan", str(lab / "v1.yaml"), str(lab / "v2.yaml"), str(app)]
    assert main(scan) == 0  # nothing left to fix


def test_preview_marks_a_missing_final_newline_like_git(tmp_path):
    (tmp_path / "client.py").write_text('post("/orders", {"size": 1})')  # no final newline
    diff = render_diff(plan_fixes(RENAME_CHANGES, tmp_path))
    assert diff.splitlines()[-4:] == [
        '-post("/orders", {"size": 1})',
        "\\ No newline at end of file",
        '+post("/orders", {"pizza_size": 1})',
        "\\ No newline at end of file",
    ]
