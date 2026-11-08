from pathlib import Path

import pytest

from apimend.cli import main
from apimend.scan import (
    NEW_REQUIRED_PROPERTY,
    REMOVED_ENDPOINT,
    REMOVED_PROPERTY,
    Change,
    SpecError,
    find_changes,
    find_usages,
    load_spec,
)

LAB = Path(__file__).resolve().parent.parent / "lab" / "pizza-api"
V1, V2, APP = LAB / "v1.yaml", LAB / "v2.yaml", LAB / "app"


def make_spec(properties, required, path="/orders", method="post"):
    """Build a tiny OpenAPI spec as a dictionary, for tests."""
    schema = {"type": "object", "properties": properties, "required": required}
    body = {"content": {"application/json": {"schema": schema}}}
    return {"paths": {path: {method: {"requestBody": body}}}}


def test_finds_the_rename_in_the_pizza_api():
    changes = find_changes(load_spec(V1), load_spec(V2))
    kinds = {(c.kind, c.prop) for c in changes}
    assert kinds == {(REMOVED_PROPERTY, "size"), (NEW_REQUIRED_PROPERTY, "pizza_size")}
    assert all(c.method == "POST" and c.path == "/orders" for c in changes)
    assert all("renamed" in c.hint or "new name" in c.hint for c in changes)


def test_no_changes_when_specs_are_identical():
    assert find_changes(load_spec(V1), load_spec(V1)) == []


def test_existing_property_becoming_required_is_not_called_a_rename():
    old = make_spec({"a": {}, "b": {}}, ["a"])
    new = make_spec({"a": {}, "b": {}}, ["a", "b"])
    assert find_changes(old, new) == [Change("POST", "/orders", NEW_REQUIRED_PROPERTY, "b")]


def test_removed_endpoint_is_reported():
    old = make_spec({"a": {}}, [])
    new = {"paths": {}}
    assert find_changes(old, new) == [Change("POST", "/orders", REMOVED_ENDPOINT)]


def test_follows_ref_links_to_shared_schemas():
    def spec(props):
        ref = {"$ref": "#/components/schemas/Order"}
        body = {"content": {"application/json": {"schema": ref}}}
        return {
            "paths": {"/orders": {"post": {"requestBody": body}}},
            "components": {"schemas": {"Order": {"properties": props}}},
        }

    changes = find_changes(spec({"size": {}}), spec({}))
    assert changes == [Change("POST", "/orders", REMOVED_PROPERTY, "size")]


def test_finds_the_affected_line_in_the_customer_app():
    change = Change("POST", "/orders", REMOVED_PROPERTY, "size")
    usages = find_usages(APP, change)
    assert len(usages) == 1
    assert usages[0].file.name == "pizza_client.py"
    assert '"size": size' in usages[0].code


def test_ignores_comments_and_files_that_do_not_call_the_endpoint(tmp_path):
    (tmp_path / "unrelated.py").write_text('settings = {"size": 10}\n')
    (tmp_path / "client.py").write_text(
        '# the "size" field is sent below\n'
        "def order(size):\n"
        '    return post("/orders", {"size": size})\n'
    )
    usages = find_usages(tmp_path, Change("POST", "/orders", REMOVED_PROPERTY, "size"))
    assert [(u.file.name, u.line) for u in usages] == [("client.py", 3)]


def test_exclude_patterns_skip_matching_files(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_client.py").write_text('post("/orders", {"size": 1})\n')
    (tmp_path / "client.py").write_text('post("/orders", {"size": 1})\n')
    change = Change("POST", "/orders", REMOVED_PROPERTY, "size")
    assert len(find_usages(tmp_path, change)) == 2
    usages = find_usages(tmp_path, change, exclude=["tests/*"])
    assert [u.file.name for u in usages] == ["client.py"]


def test_skips_files_with_syntax_errors(tmp_path):
    (tmp_path / "broken.py").write_text('post("/orders", {"size": 1}\n')
    assert find_usages(tmp_path, Change("POST", "/orders", REMOVED_PROPERTY, "size")) == []


def test_load_spec_rejects_files_that_are_not_specs(tmp_path):
    bad = tmp_path / "notes.yaml"
    bad.write_text("just: some notes\n")
    with pytest.raises(SpecError):
        load_spec(bad)


def test_scan_command_reports_affected_code(capsys):
    assert main(["scan", str(V1), str(V2), str(APP)]) == 1
    out = capsys.readouterr().out
    assert "request property 'size' was removed" in out
    assert "pizza_client.py:" in out
    assert "Found 1 affected line(s) in 1 file(s)." in out


def test_scan_command_with_identical_specs_exits_0(capsys):
    assert main(["scan", str(V1), str(V1), str(APP)]) == 0


def test_scan_command_with_missing_spec_exits_2(capsys):
    assert main(["scan", "missing.yaml", str(V2), str(APP)]) == 2
    assert "cannot read" in capsys.readouterr().err
