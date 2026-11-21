"""Tests for --format json: standard output must always be one valid JSON document."""

import json
import shlex
import shutil
import socket
import sys
from pathlib import Path

import pytest

from apimend.cli import main
from apimend.output import SCHEMA_VERSION

LAB = Path(__file__).resolve().parent.parent / "lab" / "pizza-api"
PYTHON = shlex.quote(sys.executable)


@pytest.fixture
def lab(tmp_path):
    """A private copy of the Pizza API lab, so tests can change it safely."""
    return Path(shutil.copytree(LAB, tmp_path / "pizza-api"))


def run_json(capsys, *argv):
    """Run apimend with --format json; return (exit code, parsed document)."""
    code = main([*argv, "--format", "json"])
    out = capsys.readouterr().out
    document = json.loads(out)  # fails the test if anything but JSON was printed
    assert document["exit_code"] == code
    assert document["schema_version"] == SCHEMA_VERSION
    return code, document


def scan_args(lab, *extra):
    return ["scan", str(lab / "v1.yaml"), str(lab / "v2.yaml"), str(lab / "app"), *extra]


def fix_args(lab, *extra):
    return ["fix", str(lab / "v1.yaml"), str(lab / "v2.yaml"), str(lab / "app"), *extra]


# --- scan --------------------------------------------------------------------


def test_scan_json_lists_every_affected_line(lab, capsys):
    code, doc = run_json(capsys, *scan_args(lab))
    assert code == 1
    assert doc["command"] == "scan"
    assert doc["result"] == "affected"
    kinds = [(c["kind"], c["property"]) for c in doc["changes"]]
    assert kinds == [("removed-property", "size"), ("new-required-property", "pizza_size")]
    usage = doc["changes"][0]["usages"][0]
    assert usage["file"].endswith("app/pizza_client.py")
    assert usage["confidence"] == "high"
    assert '"size": size' in usage["code"]
    assert doc["summary"]["affected_lines"] == 1
    assert doc["summary"]["by_confidence"] == {"high": 1, "medium": 0, "low": 0}
    assert doc["unreadable_files"] == []


def test_scan_json_when_nothing_changed(lab, capsys):
    v1 = str(lab / "v1.yaml")
    code, doc = run_json(capsys, "scan", v1, v1, str(lab / "app"))
    assert (code, doc["result"], doc["changes"]) == (0, "no-changes", [])
    assert doc["summary"]["affected_lines"] == 0  # the same fields are always present


def test_scan_json_reports_unreadable_files(lab, capsys):
    (lab / "app" / "broken.py").write_text("def oops(:\n")
    _, doc = run_json(capsys, *scan_args(lab))
    [problem] = doc["unreadable_files"]
    assert problem["file"].endswith("broken.py")
    assert "syntax error" in problem["reason"]


def test_scan_json_shows_how_many_lines_were_hidden(tmp_path, capsys):
    (tmp_path / "client.py").write_text(
        'def order(size):\n    return send("/orders", {"size": size})\n'
    )
    args = ["scan", str(LAB / "v1.yaml"), str(LAB / "v2.yaml"), str(tmp_path)]
    _, doc = run_json(capsys, *args, "--min-confidence", "high")
    assert doc["result"] == "not-affected"
    assert doc["changes"][0]["usages"] == []
    assert doc["changes"][0]["hidden_usages"] == 1
    assert doc["summary"]["hidden_lines"] == 1


def test_errors_are_json_too(lab, capsys):
    code = main(["scan", str(lab / "v1.yaml"), str(lab / "missing.yaml"), "--format", "json"])
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert code == doc["exit_code"] == 2
    assert doc["result"] == "error"
    assert "missing.yaml" in doc["error"]
    assert "missing.yaml" in captured.err  # people still see the message


def test_text_is_still_the_default(lab, capsys):
    main(scan_args(lab))
    out = capsys.readouterr().out
    assert out.startswith("Scanning")
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


# --- fix ---------------------------------------------------------------------


def test_fix_preview_json_lists_edits_and_changes_nothing(lab, capsys):
    before = (lab / "app" / "pizza_client.py").read_text()
    code, doc = run_json(capsys, *fix_args(lab))
    assert (code, doc["result"], doc["files_changed"]) == (1, "preview", False)
    [edit] = doc["edits"]
    assert '"size": size' in edit["before"]
    assert '"pizza_size": size' in edit["after"]
    assert edit["endpoint"] == {"method": "POST", "path": "/orders"}
    assert doc["verification"] is None
    assert (lab / "app" / "pizza_client.py").read_text() == before


def test_fix_json_without_mock_keeps_test_output_out_of_stdout(lab, capsys):
    tests = f"{PYTHON} -c \"print('hello from the tests')\""
    code, doc = run_json(capsys, *fix_args(lab, "--no-mock", "--test", tests))
    assert (code, doc["result"], doc["files_changed"]) == (0, "tests-only", True)
    report = doc["verification"]
    assert report["verdict"] == "tests-only"
    assert report["mock_used"] is False
    # Nothing was recorded, so coverage is unknown rather than "untested".
    assert report["untested_affected_endpoints"] is None
    assert report["untested_changed_endpoints"] is None
    assert report["test_output_tail"] == "hello from the tests"
    assert '"pizza_size": size' in (lab / "app" / "pizza_client.py").read_text()


def test_fix_json_failed_tests_undo_the_fix(lab, capsys):
    before = (lab / "app" / "pizza_client.py").read_text()
    tests = f'{PYTHON} -c "raise SystemExit(1)"'
    code, doc = run_json(capsys, *fix_args(lab, "--no-mock", "--test", tests))
    assert (code, doc["result"], doc["files_changed"]) == (1, "failed", False)
    assert doc["verification"]["tests_passed"] is False
    assert (lab / "app" / "pizza_client.py").read_text() == before


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(shutil.which("prism") is None, reason="Prism not installed")
def test_fix_json_end_to_end_is_verified(lab, capsys, monkeypatch):
    port = free_port()
    monkeypatch.setenv("PIZZA_API_URL", f"http://127.0.0.1:{port}")
    app = lab / "app"
    tests = f"{PYTHON} -m pytest {shlex.quote(str(app))} -q -p no:cacheprovider"
    code, doc = run_json(capsys, *fix_args(lab, "--port", str(port), "--test", tests))
    assert (code, doc["result"], doc["files_changed"]) == (0, "verified", True)
    report = doc["verification"]
    assert report["requests"] and all(r["matches_spec"] for r in report["requests"])
    assert report["untested_affected_endpoints"] == []
    assert report["remaining_lines"] == 0
