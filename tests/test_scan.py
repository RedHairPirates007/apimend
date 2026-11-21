from functools import partial
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
    unreadable_files,
)

LAB = Path(__file__).resolve().parent.parent / "lab" / "pizza-api"
V1, V2, APP = LAB / "v1.yaml", LAB / "v2.yaml", LAB / "app"

# Functions the tests below use as their own request wrappers, declared the way a project
# declares them (--request-function, or request-functions in pyproject.toml).
DECLARED = ("post", "send", "_request")
find_declared = partial(find_usages, request_functions=DECLARED)


def write(folder, source, name="client.py"):
    """Write a test file. `import requests` goes at the end, so line numbers stay as written."""
    (folder / name).write_text(source + "\nimport requests\n")


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
    usages = find_declared(tmp_path, Change("POST", "/orders", REMOVED_PROPERTY, "size"))
    assert [(u.file.name, u.line) for u in usages] == [("client.py", 3)]


def test_exclude_patterns_skip_matching_files(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_client.py").write_text('post("/orders", {"size": 1})\n')
    (tmp_path / "client.py").write_text('post("/orders", {"size": 1})\n')
    change = Change("POST", "/orders", REMOVED_PROPERTY, "size")
    assert len(find_declared(tmp_path, change)) == 2
    usages = find_declared(tmp_path, change, exclude=["tests/*"])
    assert [u.file.name for u in usages] == ["client.py"]


def test_skips_files_with_syntax_errors(tmp_path):
    (tmp_path / "broken.py").write_text('post("/orders", {"size": 1}\n')
    assert find_declared(tmp_path, Change("POST", "/orders", REMOVED_PROPERTY, "size")) == []


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
    assert [u.file.name for u in find_declared(tmp_path, change)] == ["old.py"]


# --- confidence levels and the call-site model ---------------------------------

SIZE_REMOVED = Change("POST", "/orders", REMOVED_PROPERTY, "size")
SIZE_REQUIRED = Change("POST", "/orders", NEW_REQUIRED_PROPERTY, "pizza_size")


def affected(tmp_path, source, change, name="client.py"):
    """Scan one file and return {line: confidence}."""
    write(tmp_path, source, name)
    return {u.line: u.confidence for u in find_declared(tmp_path, change) if u.file.name == name}


def test_confidence_levels_are_ordered():
    assert confidence_rank(LOW) < confidence_rank(MEDIUM) < confidence_rank(HIGH)


def test_the_lab_rename_is_found_with_high_confidence():
    [usage] = find_usages(APP, SIZE_REMOVED)
    assert usage.confidence == HIGH
    assert usage.reasons == ()


# Roadmap 2.1: unrelated dictionaries must not be reported as part of the request.


@pytest.mark.parametrize(
    "unrelated",
    [
        'log({"size": 42})',  # logging
        'analytics.track("order_placed", {"size": 42})',  # analytics
        'db.insert("orders", {"size": 42})',  # database
        'cache = {"size": 42}',  # another dictionary
    ],
)
def test_unrelated_dictionaries_in_the_same_function_are_ignored(tmp_path, unrelated):
    source = (
        f'def create_order():\n    post("/orders", {{"pizza": "margherita"}})\n    {unrelated}\n'
    )
    assert affected(tmp_path, source, SIZE_REMOVED) == {}


def test_only_the_request_to_the_changed_endpoint_is_reported(tmp_path):
    source = (
        "def sync():\n"
        '    requests.post("/orders", json={"size": 1})\n'
        '    requests.post("/other", json={"size": 2})\n'
        '    requests.get("/orders", params={"size": 3})\n'
    )
    assert affected(tmp_path, source, SIZE_REMOVED) == {2: HIGH}


def test_body_assigned_to_a_variable_before_the_request(tmp_path):
    source = (
        "def create_order(size):\n"
        '    payload = {"pizza": "margherita", "size": size}\n'
        '    extra = {"size": "ignored"}\n'
        '    return requests.post("/orders", json=payload)\n'
    )
    assert affected(tmp_path, source, SIZE_REMOVED) == {2: HIGH}


def test_keys_added_to_the_body_variable_are_part_of_the_request(tmp_path):
    source = (
        "def create_order(size):\n"
        '    payload = {"pizza": "margherita"}\n'
        '    payload["size"] = size\n'
        '    return requests.post("/orders", json=payload)\n'
    )
    assert affected(tmp_path, source, SIZE_REMOVED) == {3: HIGH}


def test_body_built_by_a_function_in_the_same_file(tmp_path):
    source = (
        "def _body(size):\n"
        '    return {"pizza": "margherita", "size": size}\n'
        "def create_order(size):\n"
        '    return requests.post("/orders", json=_body(size))\n'
    )
    assert affected(tmp_path, source, SIZE_REMOVED) == {2: HIGH}


def test_a_body_apimend_cannot_trace_makes_other_dictionaries_uncertain(tmp_path):
    source = (
        "import requests\n"
        "def build(size):\n"
        '    return {"size": size}\n'
        "def send(body):\n"
        '    return requests.post("/orders", json=body)\n'
    )
    write(tmp_path, source)
    [usage] = find_declared(tmp_path, SIZE_REMOVED)
    assert (usage.line, usage.confidence) == (3, LOW)
    assert "sends data apimend cannot follow" in usage.reasons[0]


# Roadmap 2.2: a property sent somewhere else must never hide a missing one.


def test_a_property_sent_to_another_endpoint_does_not_hide_a_missing_one(tmp_path):
    source = (
        "def create_order():\n"
        '    post("/orders", {"pizza": "margherita"})\n'
        "def create_other_record():\n"
        '    post("/other", {"pizza_size": "large"})\n'
    )
    assert affected(tmp_path, source, SIZE_REQUIRED) == {2: HIGH}


def test_each_call_to_the_same_endpoint_is_checked_on_its_own(tmp_path):
    source = (
        'requests.post("/orders", json={"pizza_size": "large"})\n'
        'requests.post("/orders", json={"pizza": "margherita"})\n'
    )
    assert affected(tmp_path, source, SIZE_REQUIRED) == {2: HIGH}


@pytest.mark.parametrize(
    "elsewhere",
    ['LABELS = {"pizza_size": "Pizza size"}', 'def show(order):\n    return order["pizza_size"]'],
)
def test_the_property_elsewhere_in_the_file_does_not_hide_it(tmp_path, elsewhere):
    source = f'{elsewhere}\nrequests.post("/orders", json={{"pizza": "margherita"}})\n'
    assert list(affected(tmp_path, source, SIZE_REQUIRED).values()) == [HIGH]


def test_the_property_in_another_file_does_not_hide_it(tmp_path):
    (tmp_path / "other.py").write_text('requests.post("/other", json={"pizza_size": "L"})\n')
    source = 'requests.post("/orders", json={"pizza": "margherita"})\n'
    assert affected(tmp_path, source, SIZE_REQUIRED) == {1: HIGH}


def test_a_request_that_may_send_more_is_uncertain(tmp_path):
    source = (
        "def order(extra):\n"
        '    body = {"pizza": "margherita"}\n'
        "    body.update(extra)\n"
        '    return requests.post("/orders", json=body)\n'
    )
    write(tmp_path, source)
    [usage] = find_declared(tmp_path, SIZE_REQUIRED)
    assert (usage.line, usage.confidence) == (4, LOW)
    assert "is changed with .update()" in usage.reasons[0]


# HTTP methods, paths and things apimend cannot follow


def test_http_method_is_read_from_wrapper_calls(tmp_path):
    source = (
        "def order():\n"
        '    return _request("POST", "/orders", {"pizza": "x"})\n'
        "def orders():\n"
        '    return _request(method="GET", path="/orders")\n'
    )
    assert affected(tmp_path, source, SIZE_REQUIRED) == {2: HIGH}  # GET is not affected


def test_an_unknown_http_method_is_probable_not_proven(tmp_path):
    source = 'URL = BASE + "/orders"\ndef order(size):\n    return send(URL, {"size": size})\n'
    write(tmp_path, source)
    [usage] = find_declared(tmp_path, SIZE_REMOVED)
    assert (usage.line, usage.confidence) == (3, MEDIUM)
    assert usage.reasons == ("apimend could not tell which HTTP method this request uses",)


def test_paths_with_parameters_only_partly_match(tmp_path):
    source = (
        'import requests\ndef show(order_id):\n    return requests.get(f"/orders/{order_id}")\n'
    )
    write(tmp_path, source)
    [usage] = find_declared(tmp_path, Change("GET", "/orders/{id}", REMOVED_ENDPOINT))
    assert usage.confidence == MEDIUM
    assert usage.reasons == ("the path only partly matches /orders/{id}",)


def test_a_path_no_request_uses_is_reported_not_ignored(tmp_path):
    source = 'ORDERS = "/orders"\ndef order(client):\n    return client.post(config.ORDERS)\n'
    write(tmp_path, source)
    [usage] = find_declared(tmp_path, Change("POST", "/orders", REMOVED_ENDPOINT))
    assert (usage.line, usage.confidence) == (1, LOW)
    assert "apimend found no request that uses it" in usage.reasons[0]


def test_docstrings_that_mention_a_path_are_not_reported(tmp_path):
    source = '"""Talks to /orders."""\ndef f():\n    """Calls /orders."""\n'
    assert affected(tmp_path, source, Change("POST", "/orders", REMOVED_ENDPOINT)) == {}


MIXED_CLIENT = """\
import requests


def place(size):
    return requests.post("https://api.example.com/orders", json={"size": size})


def build(size):
    return {"size": size}


def send(body):
    return requests.post("/orders", json=body)
"""


def test_scan_can_hide_lower_confidence_lines(tmp_path, capsys):
    app = tmp_path / "app"
    app.mkdir()
    (app / "client.py").write_text(MIXED_CLIENT)
    assert main(["scan", str(V1), str(V2), str(app)]) == 1
    out = capsys.readouterr().out
    assert "[low]    " in out and "sends data apimend cannot follow" in out

    assert main(["scan", str(V1), str(V2), str(app), "--min-confidence", "high"]) == 1
    out = capsys.readouterr().out
    assert "[low]" not in out
    assert "line(s) with lower confidence were hidden by --min-confidence high." in out
    assert "1 high, 0 medium, 0 low confidence." in out


def test_scan_rejects_an_unknown_confidence_level(capsys):
    with pytest.raises(SystemExit):
        main(["scan", str(V1), str(V2), str(APP), "--min-confidence", "certain"])


CLIENT_STYLES = """\
import json
import urllib.request
import aiohttp, httpx, requests
BASE = "https://api.example.com"


def with_urllib(size):
    body = {"pizza": "m", "size": size}
    req = urllib.request.Request(BASE + "/orders", data=json.dumps(body).encode(), method="POST")
    return urllib.request.urlopen(req)


def with_httpx(client: httpx.Client, size):
    return client.post(url=f"{BASE}/orders", json={"size": size})


def with_requests(size):
    return requests.request("POST", BASE + "/orders", json={"size": size})


async def with_aiohttp(session: aiohttp.ClientSession, size):
    async with session.post(BASE + "/orders", json={"size": size}) as response:
        return await response.json()
"""


def test_common_http_clients_are_understood(tmp_path):
    assert affected(tmp_path, CLIENT_STYLES, SIZE_REMOVED) == {
        8: HIGH,  # urllib, body passed through json.dumps(...).encode()
        14: HIGH,  # httpx, URL as a keyword
        18: HIGH,  # requests.request("POST", ...)
        22: HIGH,  # aiohttp
    }
    required = affected(tmp_path, CLIENT_STYLES, SIZE_REQUIRED)
    assert required == {9: HIGH, 14: HIGH, 18: HIGH, 22: HIGH}  # urlopen(req) is not a 2nd request


# --- execution order: only data set before a request is part of it ------------


def test_a_key_added_after_the_request_is_not_part_of_it(tmp_path):
    source = (
        "def create_order(size):\n"
        '    body = {"pizza": "margherita"}\n'
        '    requests.post("/orders", json=body)\n'
        '    body["size"] = size\n'
    )
    assert affected(tmp_path, source, SIZE_REMOVED) == {}  # never sent, never changed
    assert affected(tmp_path, source, Change("POST", "/orders", NEW_REQUIRED_PROPERTY, "size")) == {
        3: HIGH
    }


def test_changes_after_the_request_do_not_make_it_uncertain(tmp_path):
    source = (
        "def create_order(extra):\n"
        '    body = {"pizza": "margherita"}\n'
        '    requests.post("/orders", json=body)\n'
        "    body.update(extra)\n"
    )
    assert affected(tmp_path, source, SIZE_REQUIRED) == {3: HIGH}  # traced, missing


def test_reassigning_the_body_after_the_request_does_not_hide_it(tmp_path):
    source = (
        "def create_order(size):\n"
        '    body = {"size": size}\n'
        '    requests.post("/orders", json=body)\n'
        '    body = {"other": 1}\n'
    )
    assert affected(tmp_path, source, SIZE_REMOVED) == {2: HIGH}


def test_a_key_added_only_in_some_cases_is_probable(tmp_path):
    source = (
        "def create_order(size, express):\n"
        '    body = {"pizza": "margherita"}\n'
        "    if express:\n"
        '        body["size"] = size\n'
        '    return requests.post("/orders", json=body)\n'
    )
    write(tmp_path, source)
    [usage] = find_declared(tmp_path, SIZE_REMOVED)
    assert (usage.line, usage.confidence) == (4, MEDIUM)
    assert usage.reasons == ("'size' is only added in some cases",)
    [usage] = find_declared(tmp_path, Change("POST", "/orders", NEW_REQUIRED_PROPERTY, "size"))
    assert (usage.line, usage.confidence) == (5, MEDIUM)
    assert usage.reasons == ("this request sends 'size' only in some cases",)


def test_a_body_assigned_only_in_some_cases_is_not_traced(tmp_path):
    source = (
        "def create_order(size, express):\n"
        "    if express:\n"
        '        body = {"size": size}\n'
        '    return requests.post("/orders", json=body)\n'
    )
    write(tmp_path, source)
    [usage] = find_declared(tmp_path, SIZE_REMOVED)
    assert usage.confidence == LOW  # reported, never changed automatically


def test_a_key_added_later_in_a_loop_reaches_the_next_request(tmp_path):
    source = (
        "def send_all(sizes):\n"
        '    body = {"pizza": "margherita"}\n'
        "    for size in sizes:\n"
        '        requests.post("/orders", json=body)\n'
        '        body["size"] = size\n'
    )
    assert affected(tmp_path, source, SIZE_REMOVED) == {5: MEDIUM}


def test_module_constants_defined_anywhere_are_followed_inside_functions(tmp_path):
    source = (
        "def create_order(size):\n"
        '    return requests.post(URL, json={"size": size})\n'
        'URL = BASE + "/orders"\n'
    )
    assert affected(tmp_path, source, SIZE_REMOVED) == {2: HIGH}


# --- files apimend cannot analyse are reported, never skipped silently ----------


def test_unreadable_files_are_listed_with_the_reason(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "broken.py").write_text("def f(:\n")
    (tmp_path / "latin.py").write_bytes(b"name = '\xe9'\n")
    found = {file.name: reason for file, reason in unreadable_files(tmp_path)}
    assert found == {"broken.py": "syntax error on line 1", "latin.py": "not UTF-8 text"}


def test_scan_warns_about_files_it_could_not_check(tmp_path, capsys):
    (tmp_path / "broken.py").write_text("def f(:\n")
    assert main(["scan", str(V1), str(V2), str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Warning: 1 file(s) could not be analysed, so they were not checked:" in out
    assert "broken.py: syntax error on line 1" in out
    assert "Your code does not appear to be affected in the files apimend could analyse." in out


def test_fix_warns_about_files_it_could_not_check(tmp_path, capsys):
    (tmp_path / "broken.py").write_text("def f(:\n")
    assert main(["fix", str(V1), str(V2), str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "broken.py: syntax error on line 1" in out
    assert "in the files apimend could analyse. Nothing to fix." in out
