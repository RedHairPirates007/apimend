import shutil
from importlib.metadata import version
from pathlib import Path

import pytest

from apimend import __version__
from apimend.cli import check_tools, main

LAB = Path(__file__).resolve().parent.parent / "lab" / "pizza-api"


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_check_tools_reports_every_tool():
    result = check_tools()
    assert set(result) == {"oasdiff", "prism"}
    assert all(isinstance(v, bool) for v in result.values())


def test_doctor_runs(capsys):
    code = main(["doctor"])
    assert code in (0, 1)
    assert "apimend" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("oasdiff") is None, reason="oasdiff not installed")
def test_diff_detects_breaking_change_in_pizza_api(capsys):
    code = main(["diff", str(LAB / "v1.yaml"), str(LAB / "v2.yaml")])
    assert code == 1
    assert "BREAKING" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("oasdiff") is None, reason="oasdiff not installed")
def test_diff_same_spec_has_no_breaking_changes(capsys):
    code = main(["diff", str(LAB / "v1.yaml"), str(LAB / "v1.yaml")])
    assert code == 0


def test_doctor_reports_missing_tools(monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert main(["doctor"]) == 1
    assert "[missing]" in capsys.readouterr().out


def test_diff_without_oasdiff_explains_and_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert main(["diff", "old.yaml", "new.yaml"]) == 2
    assert "not installed" in capsys.readouterr().err


def test_installed_package_version_matches_the_code():
    assert version("apimend") == __version__
