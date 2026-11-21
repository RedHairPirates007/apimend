"""Decide whether a removed request property was renamed, and how sure that is.

A property that disappears while a new required one appears is not proof of a rename:
removing `quantity` and adding `customer_id` are two unrelated changes. apimend only
confirms a rename when two independent pieces of evidence agree:

- the schemas match: same type, format, allowed values, array items and object fields;
- the names are related: one contains the other's words (`size` -> `pizza_size`), or
  only the spelling changed (`customerId` -> `customer_id`). Sharing only a generic word
  such as "id" or "name" does not count.

Anything less is a *possible* rename. `fix` never applies a possible rename on its own:
the developer has to approve it, for example with `--rename quantity=customer_id`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONFIRMED = "confirmed"
POSSIBLE = "possible"

# Words too common to show two names mean the same thing.
GENERIC_WORDS = {
    "id", "ids", "uuid", "key", "name", "type", "kind", "value", "values", "data", "info",
    "code", "number", "num", "no", "count", "item", "items", "list", "field", "flag", "is",
    "has", "the", "a", "an", "of", "for", "to", "by", "at", "on", "in",
}  # fmt: skip

_MAX_DEPTH = 5  # how deep to compare nested schemas and follow $ref links


@dataclass(frozen=True)
class Evidence:
    """What supports or contradicts renaming `old` to `new`."""

    old: str
    new: str
    differences: tuple[str, ...]  # how the schemas differ; empty when they match
    names_related: bool
    names_reason: str

    @property
    def confirmed(self) -> bool:
        return not self.differences and self.names_related

    @property
    def reasons(self) -> tuple[str, ...]:
        """Why the rename is not confirmed (empty when it is)."""
        if self.confirmed:
            return ()
        return self.differences + (() if self.names_related else (self.names_reason,))


def words(name: str) -> list[str]:
    """The words in a property name: "pizzaSize", "pizza_size", "pizza-size" -> pizza, size."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def names_related(old: str, new: str) -> tuple[bool, str]:
    """Whether two property names plausibly mean the same thing, and why (not)."""
    old_words, new_words = words(old), words(new)
    if old_words == new_words:
        return True, "only the spelling of the name changed"
    shared = [w for w in old_words if w in new_words]
    meaningful = [w for w in shared if w not in GENERIC_WORDS]
    contained = set(old_words) <= set(new_words) or set(new_words) <= set(old_words)
    if meaningful and contained:
        return True, f"the names share the word '{meaningful[0]}'"
    if shared and not meaningful:
        return False, f"the names only share the generic word '{shared[0]}'"
    if meaningful:
        return False, "the names share some words, but neither contains the other"
    return False, "the names are not related"


def _resolve(node: object, spec: dict, depth: int = 0) -> dict:
    """Follow a local $ref such as #/components/schemas/Size."""
    while isinstance(node, dict) and "$ref" in node and depth <= _MAX_DEPTH:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return {}
        target: object = spec
        for part in ref[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        node, depth = target, depth + 1
    return node if isinstance(node, dict) else {}


def _shape(schema: object, spec: dict, depth: int = 0) -> dict:
    """The parts of a schema that decide which values are valid."""
    resolved = _resolve(schema, spec)
    if depth > _MAX_DEPTH:
        return {}
    shape: dict = {}
    for key in ("type", "format", "nullable"):
        if key in resolved:
            value = resolved[key]
            shape[key] = sorted(value) if isinstance(value, list) else value
    if isinstance(resolved.get("enum"), list):
        shape["enum"] = sorted(repr(v) for v in resolved["enum"])
    if "items" in resolved:
        shape["items"] = _shape(resolved["items"], spec, depth + 1)
    if isinstance(resolved.get("properties"), dict):
        shape["properties"] = {
            name: _shape(value, spec, depth + 1) for name, value in resolved["properties"].items()
        }
        shape["required"] = sorted(resolved.get("required") or [])
    return shape


def _describe_type(shape: dict) -> str:
    kind = shape.get("type", "any type")
    kind = " or ".join(kind) if isinstance(kind, list) else str(kind)
    return f"{kind} ({shape['format']})" if shape.get("format") else kind


def schema_differences(
    old_schema: object, new_schema: object, old_spec: dict, new_spec: dict
) -> tuple[str, ...]:
    """How two property schemas differ, in words. Empty when they accept the same values."""
    old, new = _shape(old_schema, old_spec), _shape(new_schema, new_spec)
    found = []
    if (old.get("type"), old.get("format")) != (new.get("type"), new.get("format")):
        found.append(f"types differ: {_describe_type(old)} -> {_describe_type(new)}")
    if old.get("enum") != new.get("enum"):
        found.append("allowed values differ")
    if old.get("nullable") != new.get("nullable"):
        found.append("only one of them allows null")
    if old.get("items") != new.get("items"):
        found.append("array items differ")
    if (old.get("properties"), old.get("required")) != (
        new.get("properties"),
        new.get("required"),
    ):
        found.append("object fields differ")
    return tuple(found)


def evidence(
    old: str, new: str, old_schema: object, new_schema: object, old_spec: dict, new_spec: dict
) -> Evidence:
    related, reason = names_related(old, new)
    differences = schema_differences(old_schema, new_schema, old_spec, new_spec)
    return Evidence(old, new, differences, related, reason)


def pair(
    removed: dict[str, object],
    added: dict[str, object],
    old_spec: dict,
    new_spec: dict,
) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """Match removed properties with new required ones on one operation.

    `removed` and `added` map property names to their schemas. Returns
    {property: (partner, CONFIRMED or POSSIBLE, reasons)} for both sides of each pair.
    A pair is confirmed when its evidence is, and neither property has another
    confirmed partner. With exactly one removed and one added property that are not
    confirmed, they are reported as a possible rename, with the reasons.
    """
    found = {
        (old, new): evidence(old, new, removed[old], added[new], old_spec, new_spec)
        for old in removed
        for new in added
    }
    confirmed = [e for e in found.values() if e.confirmed]
    result: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for e in confirmed:
        if (
            sum(c.old == e.old for c in confirmed) == 1
            and sum(c.new == e.new for c in confirmed) == 1
        ):
            result[e.old] = (e.new, CONFIRMED, ())
            result[e.new] = (e.old, CONFIRMED, ())
    if not result and len(removed) == 1 and len(added) == 1:
        [e] = found.values()
        result[e.old] = (e.new, POSSIBLE, e.reasons)
        result[e.new] = (e.old, POSSIBLE, e.reasons)
    return result
