"""Renames need evidence, not coincidence (review finding P0-2).

A removed property plus a new required one is not proof of a rename. apimend only
confirms a rename when the schemas match and the names are related; anything less is a
possible rename that `fix` never applies unless the developer approves it.
"""

import json

import pytest
import yaml

from apimend.cli import main
from apimend.fix import find_renames, plan_fixes
from apimend.renames import CONFIRMED, POSSIBLE, names_related, words
from apimend.scan import REMOVED_PROPERTY, find_changes

CLIENT = (
    "import requests\n"
    "def order(pizza, value):\n"
    '    return requests.post("http://x/orders", json={"pizza": pizza, "OLD": value})\n'
)


def spec(properties, components=None):
    schema = {"type": "object", "properties": properties, "required": list(properties)}
    body = {"content": {"application/json": {"schema": schema}}}
    document = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {"/orders": {"post": {"requestBody": body, "responses": {"201": {}}}}},
    }
    if components:
        document["components"] = {"schemas": components}
    return document


def removed(old_props, new_props, components=None):
    """The REMOVED_PROPERTY change for 'the' removed property."""
    changes = find_changes(spec(old_props, components), spec(new_props, components))
    [change] = [c for c in changes if c.kind == REMOVED_PROPERTY]
    return change, changes


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
SIZES = {"type": "string", "enum": ["small", "medium", "large"]}


# --- the reported case --------------------------------------------------------


def test_unrelated_properties_are_not_a_confirmed_rename():
    change, changes = removed(
        {"pizza": STRING, "quantity": INTEGER}, {"pizza": STRING, "customer_id": STRING}
    )
    assert (change.partner, change.rename) == ("customer_id", POSSIBLE)
    assert "types differ: integer -> string" in change.rename_reasons
    assert "the names are not related" in change.rename_reasons
    assert find_renames(changes) == []


def test_unrelated_properties_are_never_changed(tmp_path):
    (tmp_path / "client.py").write_text(CLIENT.replace("OLD", "quantity"))
    _, changes = removed({"quantity": INTEGER}, {"customer_id": STRING})
    assert plan_fixes(changes, tmp_path, min_confidence="low") == []


@pytest.fixture
def project(tmp_path):
    """A project whose spec removed 'quantity' and now requires 'customer_id'."""
    (tmp_path / "v1.yaml").write_text(yaml.safe_dump(spec({"pizza": STRING, "quantity": INTEGER})))
    (tmp_path / "v2.yaml").write_text(
        yaml.safe_dump(spec({"pizza": STRING, "customer_id": STRING}))
    )
    app = tmp_path / "app"
    app.mkdir()
    (app / "client.py").write_text(CLIENT.replace("OLD", "quantity"))
    return tmp_path


def fix(project, *extra):
    p = project
    return main(["fix", str(p / "v1.yaml"), str(p / "v2.yaml"), str(p / "app"), *extra])


def test_the_reported_case_through_the_command_line(project, capsys):
    assert fix(project, "--no-mock", "--test", "true") == 1
    out = capsys.readouterr().out
    assert "Possible rename, not applied: POST /orders 'quantity' -> 'customer_id'" in out
    assert "approve it with --rename quantity=customer_id" in out
    assert "Nothing was changed" in out
    assert '"quantity": value' in (project / "app" / "client.py").read_text()


# --- the developer can approve a rename apimend cannot confirm ----------------


def test_an_approved_rename_is_applied_with_a_warning(project, capsys):
    assert fix(project, "--rename", "quantity=customer_id", "--no-mock", "--test", "true") == 0
    out = capsys.readouterr().out
    assert "Using your approved rename: 'quantity' -> 'customer_id'." in out
    assert "apimend could not confirm it: types differ: integer -> string" in out
    assert '"customer_id": value' in (project / "app" / "client.py").read_text()


def test_renames_can_be_approved_in_pyproject(project, capsys):
    (project / "pyproject.toml").write_text(
        '[tool.apimend]\nrenames = { quantity = "customer_id" }\n'
    )
    assert fix(project, "--no-mock", "--test", "true") == 0
    assert '"customer_id": value' in (project / "app" / "client.py").read_text()


def test_an_approval_that_matches_nothing_is_an_error(project, capsys):
    assert fix(project, "--rename", "quantity=customer") == 2  # typo
    assert "does not match the specs" in capsys.readouterr().err
    assert '"quantity": value' in (project / "app" / "client.py").read_text()


@pytest.mark.parametrize("text", ["quantity", "=customer_id", "quantity=", "a=b=c"])
def test_a_badly_written_approval_is_an_error(project, capsys, text):
    assert fix(project, "--rename", text) == 2
    assert "write it as OLD=NEW" in capsys.readouterr().err


def test_json_output_shows_why_a_rename_was_not_applied(project, capsys):
    assert fix(project, "--format", "json") == 1
    document = json.loads(capsys.readouterr().out)
    rename = document["changes"][0]["rename"]
    assert rename["status"] == POSSIBLE
    assert "the names are not related" in rename["unconfirmed_because"]
    assert document["renames"] == []


# --- what counts as evidence --------------------------------------------------


def test_matching_schemas_and_related_names_confirm_a_rename():
    change, changes = removed({"size": SIZES}, {"pizza_size": SIZES})
    assert (change.partner, change.rename, change.rename_reasons) == ("pizza_size", CONFIRMED, ())
    assert [(r.old, r.new) for r in find_renames(changes)] == [("size", "pizza_size")]


def test_same_type_but_unrelated_names_is_not_enough():
    change, _ = removed({"quantity": INTEGER}, {"customer_id": INTEGER})
    assert change.rename == POSSIBLE
    assert change.rename_reasons == ("the names are not related",)


def test_related_names_but_a_different_type_is_not_enough():
    change, _ = removed({"size": SIZES}, {"pizza_size": INTEGER})
    assert change.rename == POSSIBLE
    assert change.rename_reasons[0].startswith("types differ: string -> integer")


def test_different_allowed_values_are_not_enough():
    upper = {"type": "string", "enum": ["S", "M", "L"]}
    change, _ = removed({"size": SIZES}, {"pizza_size": upper})
    assert change.rename_reasons == ("allowed values differ",)


def test_different_formats_are_not_enough():
    change, _ = removed(
        {"due": {"type": "string", "format": "date"}},
        {"due_at": {"type": "string", "format": "date-time"}},
    )
    assert change.rename_reasons == ("types differ: string (date) -> string (date-time)",)


def test_sharing_only_a_generic_word_is_not_enough():
    change, _ = removed({"id": STRING}, {"customer_id": STRING})
    assert change.rename_reasons == ("the names only share the generic word 'id'",)


def test_a_spelling_change_is_confirmed():
    change, _ = removed({"customerId": STRING}, {"customer_id": STRING})
    assert change.rename == CONFIRMED


def test_schemas_are_compared_through_ref_links():
    components = {"Size": SIZES}
    ref = {"$ref": "#/components/schemas/Size"}
    change, _ = removed({"size": ref}, {"pizza_size": ref}, components)
    assert change.rename == CONFIRMED


def test_several_renames_on_one_endpoint_are_each_confirmed():
    changes = find_changes(
        spec({"size": SIZES, "crust": STRING}),
        spec({"pizza_size": SIZES, "crust_type": STRING}),
    )
    pairs = {(r.old, r.new) for r in find_renames(changes)}
    assert pairs == {("size", "pizza_size"), ("crust", "crust_type")}


def test_two_equally_good_candidates_are_not_a_rename():
    changes = find_changes(
        spec({"size": STRING}), spec({"pizza_size": STRING, "size_label": STRING})
    )
    assert find_renames(changes) == []
    assert all(c.rename == "" for c in changes)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("pizza_size", ["pizza", "size"]),
        ("pizzaSize", ["pizza", "size"]),
        ("pizza-size", ["pizza", "size"]),
        ("HTTPStatus", ["http", "status"]),
    ],
)
def test_names_are_split_into_words(name, expected):
    assert words(name) == expected


def test_names_that_share_words_without_containing_each_other_are_not_related():
    assert names_related("pizza_size", "crust_size")[0] is False
