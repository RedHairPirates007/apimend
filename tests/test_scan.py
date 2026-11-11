from pathlib import Path

import pytest

from apimend.cli import main
from apimend.scan import (
    HIGH,
    LOW,
    MEDIUM,
    NEW_REQUIRED_PROPERTY,
    REMOVED_ENDPOINT,
    REMOVED_PROPERTY,
    Change,
    SpecError,
    confidence_rank,
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
    assert "[high]   " in out
    assert "Found 1 affected line(s) in 1 file(s): 1 high, 0 medium, 0 low confidence." in out


def test_scan_command_with_identical_specs_exits_0(capsys):
    assert main(["scan", str(V1), str(V1), str(APP)]) == 0


def test_scan_command_with_missing_spec_exits_2(capsys):
    assert main(["scan", "missing.yaml", str(V2), str(APP)]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_rename_hint_only_goes_to_the_new_property():
    old = make_spec({"pizza": {}, "size": {}}, ["size"])
    new = make_spec({"pizza": {}, "pizza_size": {}}, ["pizza", "pizza_size"])
    hints = {c.prop: c.hint for c in find_changes(old, new)}
    assert "renamed to 'pizza_size'" in hints["size"]
    assert "new name" in hints["pizza_size"]
    assert hints["pizza"] == ""


def test_files_already_sending_a_new_required_property_are_not_flagged(tmp_path):
    (tmp_path / "old.py").write_text('post("/orders", {"size": 1})\n')
    (tmp_path / "new.py").write_text('post("/orders", {"pizza_size": 1})\n')
    change = Change("POST", "/orders", NEW_REQUIRED_PROPERTY, "pizza_size")
    assert [u.file.name for u in find_usages(tmp_path, change)] == ["old.py"]


# --- confidence levels -------------------------------------------------------

SIZE_REMOVED = Change("POST", "/orders", REMOVED_PROPERTY, "size")

CLIENT = """\
import requests

def place(size):
    return requests.post("https://api.example.com/orders", json={"size": size})

def list_orders():
    return requests.get("/orders", params={"size": "large"})

def describe(size):
    return "size: " + size

LABEL = "size"
"""


def confidence_by_line(usages):
    return {u.line: u.confidence for u in usages}


def test_confidence_levels_are_ordered():
    assert confidence_rank(LOW) < confidence_rank(MEDIUM) < confidence_rank(HIGH)


def test_the_lab_rename_is_found_with_high_confidence():
    [usage] = find_usages(APP, SIZE_REMOVED)
    assert usage.confidence == HIGH
    assert usage.reasons == ()


def test_confidence_reflects_how_the_property_is_used(tmp_path):
    (tmp_path / "client.py").write_text(CLIENT)
    usages = {u.line: u for u in find_usages(tmp_path, SIZE_REMOVED)}
    assert usages[4].confidence == HIGH  # dict key, same function, POST /orders
    assert usages[7].confidence == LOW  # sent with GET /orders, not POST
    assert "uses GET /orders, not POST" in usages[7].reasons[0]
    assert usages[12].confidence == LOW  # LABEL = "size": unrelated module-level text
    assert 10 not in usages  # "size: " is a different string


def test_property_built_in_another_function_is_medium(tmp_path):
    (tmp_path / "client.py").write_text(
        "import requests\n"
        "def build(size):\n"
        '    return {"size": size}\n'
        "def send(body):\n"
        '    return requests.post("/orders", json=body)\n'
    )
    [usage] = find_usages(tmp_path, SIZE_REMOVED)
    assert usage.confidence == MEDIUM
    assert usage.reasons == ("'size' is not in the same function as the call to /orders",)


def test_http_method_is_read_from_wrapper_calls(tmp_path):
    (tmp_path / "client.py").write_text(
        "def order():\n"
        '    return _request("POST", "/orders", {"pizza": "x"})\n'
        "def orders():\n"
        '    return _request(method="GET", path="/orders")\n'
    )
    change = Change("POST", "/orders", NEW_REQUIRED_PROPERTY, "pizza_size")
    assert confidence_by_line(find_usages(tmp_path, change)) == {2: HIGH, 4: LOW}


def test_unknown_http_method_does_not_lower_confidence(tmp_path):
    (tmp_path / "client.py").write_text(
        'URL = BASE + "/orders"\ndef order(size):\n    return send(URL, {"size": size})\n'
    )
    [usage] = find_usages(tmp_path, SIZE_REMOVED)
    assert usage.confidence == MEDIUM  # only "different function" counts against it
    assert len(usage.reasons) == 1


def test_paths_with_parameters_only_partly_match(tmp_path):
    (tmp_path / "client.py").write_text(
        'import requests\ndef show(order_id):\n    return requests.get(f"/orders/{order_id}")\n'
    )
    change = Change("GET", "/orders/{id}", REMOVED_ENDPOINT)
    [usage] = find_usages(tmp_path, change)
    assert usage.confidence == MEDIUM
    assert usage.reasons == ("the path only partly matches /orders/{id}",)


def test_scan_can_hide_lower_confidence_lines(tmp_path, capsys):
    app = tmp_path / "app"
    app.mkdir()
    (app / "client.py").write_text(CLIENT)
    assert main(["scan", str(V1), str(V2), str(app)]) == 1
    out = capsys.readouterr().out
    assert "[low]    " in out and "- the nearby call uses GET /orders, not POST" in out

    assert main(["scan", str(V1), str(V2), str(app), "--min-confidence", "high"]) == 1
    out = capsys.readouterr().out
    assert "[low]" not in out
    assert "line(s) with lower confidence were hidden by --min-confidence high." in out
    assert "Found 1 affected line(s) in 1 file(s): 1 high, 0 medium, 0 low confidence." in out


def test_scan_rejects_an_unknown_confidence_level(capsys):
    with pytest.raises(SystemExit):
        main(["scan", str(V1), str(V2), str(APP), "--min-confidence", "certain"])
