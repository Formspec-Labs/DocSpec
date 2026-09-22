"""Small, eager Core oracle. No production imports, storage, hashes, or SQL.

This model is deliberately unsuitable for corpus processing. Tests compare
production results with its logical values, independently of physical layout.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any


def value_key(value: Any) -> tuple:
    """Compare JSON without Python's True == 1 or object insertion ordering."""
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("boolean", value)
    if type(value) is int and abs(value) <= 2**53 - 1:
        return ("integer", value)
    if isinstance(value, str):
        value.encode("utf-16-be")  # Surrogates are outside the admitted codec.
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(map(value_key, value)))
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return ("object", tuple((key, value_key(value[key])) for key in keys))
    raise ValueError("outside the Core JSON codec")


def pointer_tokens(pointer: str) -> tuple[str, ...]:
    """Split a JSON Pointer into tokens; anything that is not a legal pointer refuses."""

    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("invalid JSON Pointer")
    if re.search(r"~(?![01])", pointer):
        raise ValueError("invalid JSON Pointer escape")
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:])


def selected_field(value: Any, pointer: str) -> list:
    """A well-formed unresolved address is absent; invalid syntax refuses."""
    for token in pointer_tokens(pointer):
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            # Avoid parsing an arbitrarily long integer just to prove absence.
            if len(token) > len(str(len(value))) or int(token) >= len(value):
                return ["absent"]
            value = value[int(token)]
        else:
            return ["absent"]
    return ["present", deepcopy(value)]


def selected_value(value: Any, selectors: list[dict] | None = None) -> list:
    """Return ``["present", value]`` without selectors, else one labelled ``present``/``absent`` row per selector.

    Duplicate selector labels are refused.
    """

    if selectors is None:
        return ["present", deepcopy(value)]
    labels = [selector["label"] for selector in selectors]
    if len(set(labels)) != len(labels):
        raise ValueError("duplicate selector label")
    return [[selector["label"], *selected_field(value, selector["pointer"])] for selector in selectors]


@dataclass(frozen=True)
class State:
    state_id: str
    members: dict[str, str]
    occurrences: dict[str, Any]

    @classmethod
    def root(cls, state_id: str, rows: list[tuple[str, str, Any]]) -> State:
        """Build a root state; member keys must be distinct strings and occurrences distinct identities."""

        keys = [row[0] for row in rows]
        entities = [row[1] for row in rows]
        if len(set(keys)) != len(keys) or len(set(entities)) != len(entities):
            raise ValueError("roots require distinct keys and occurrences")
        if any(not isinstance(key, str) for key in keys):
            raise ValueError("member keys must be strings")
        for _, _, value in rows:
            value_key(value)
        return cls(state_id, dict(zip(keys, entities, strict=True)), deepcopy({e: v for _, e, v in rows}))

    def revise(self, state_id: str, edits: list[dict], new_values: dict[str, Any] | None = None) -> State:
        """Return the revised state, applying edits in sequence order.

        Refuses a reused state identity, a changed retained occurrence, edit
        sequences that are not distinct nonnegative integers, an unknown
        occurrence, and removing an absent member.
        """

        if state_id == self.state_id:
            raise ValueError("revision requires a new state identity")
        occurrences = deepcopy(self.occurrences)
        for entity, value in (new_values or {}).items():
            key = value_key(value)
            if entity in occurrences and value_key(occurrences[entity]) != key:
                raise ValueError("cannot change a retained occurrence")
            occurrences[entity] = deepcopy(value)
        sequences = [edit.get("sequence") for edit in edits]
        if any(type(seq) is not int or seq < 0 for seq in sequences) or len(set(sequences)) != len(sequences):
            raise ValueError("edits require distinct nonnegative sequence numbers")
        members = self.members.copy()
        for edit in sorted(edits, key=lambda edit: edit["sequence"]):
            key = edit["member_key"]
            if not isinstance(key, str):
                raise ValueError("member keys must be strings")
            if edit["op"] == "put":
                entity = edit["occurrence_id"]
                if entity not in occurrences:
                    raise ValueError("unknown occurrence")
                members[key] = entity
            elif edit["op"] == "remove":
                if key not in members:
                    raise ValueError("cannot remove an absent member")
                del members[key]
            else:
                raise ValueError("unknown membership edit")
        return State(state_id, members, occurrences)

    def selected_members(
        self, *, selectors: list[dict] | None = None, scope: list[str] | None = None,
        material_keys: bool = False, material_entities: bool = False,
        ordered_keys: list[str] | None = None,
    ) -> list:
        """ordered_keys is the independently supplied expected sort-rule result.

        Without it, a Counter of value_key rows expresses multiset equivalence;
        list order here is only deterministic fixture presentation.
        """
        keys = list(self.members) if scope is None else list(scope)
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate requested member key")
        if ordered_keys is not None:
            if len(ordered_keys) != len(keys) or set(ordered_keys) != set(keys):
                raise ValueError("sort result must contain each requested key once")
            keys = ordered_keys
        else:
            keys.sort()
        rows = []
        for key in keys:
            entity = self.members.get(key)
            row = ["absent"] if entity is None else [
                "present", selected_value(self.occurrences[entity], selectors),
            ]
            if material_keys:
                row.append(["key", key])
            if material_entities and entity is not None:
                row.append(["entity", entity])
            rows.append(row)
        return rows


def check_provenance(
    *, generations: list[dict], usages: list[dict], derivations: list[tuple[str, str]],
    activities: dict[str, tuple[int | None, int | None]] | None = None,
    qualified_derivations: list[dict] | None = None,
) -> None:
    """Check the fixture vocabulary, not arbitrary PROV documents.

    Event positions represent stated causal order within the fixture, never
    inferred wall-clock order or a membership edit order. Missing positions
    remain unknown. Imported entities need no invented generation or activity.
    """
    generated = {}
    for generation in generations:
        entity = generation["entity"]
        if entity in generated and generated[entity] != generation:
            raise ValueError("conflicting generation")
        generated[entity] = generation
    for event in [*generations, *usages]:
        activity = event["activity"]
        if activities is not None:
            if activity not in activities:
                raise ValueError("unknown activity")
            start, end = activities[activity]
            position = event.get("position")
            if start is not None and end is not None and start > end:
                raise ValueError("activity ends before it starts")
            if position is not None and (
                (start is not None and position < start) or (end is not None and position > end)
            ):
                raise ValueError("event outside activity")
    for usage in usages:
        generation = generated.get(usage["entity"])
        before = generation.get("position") if generation else None
        after = usage.get("position")
        if before is not None and after is not None and before > after:
            raise ValueError("usage precedes generation")
    generation_events = {event["event_id"]: event for event in generations if "event_id" in event}
    usage_events = {event["event_id"]: event for event in usages if "event_id" in event}
    qualified_pairs = []
    for relation in qualified_derivations or ():
        generation = generation_events.get(relation["generation_event"])
        usage = usage_events.get(relation["usage_event"])
        if generation is None or usage is None:
            raise ValueError("qualified derivation names an unknown event")
        if (generation["entity"] != relation["generated"] or usage["entity"] != relation["used"]
                or generation["activity"] != usage["activity"]):
            raise ValueError("qualified derivation events do not match")
        before, after = usage.get("position"), generation.get("position")
        if before is not None and after is not None and before > after:
            raise ValueError("derived output precedes its input usage")
        qualified_pairs.append((relation["generated"], relation["used"]))
    parents: dict[str, set[str]] = {}
    for derived, source in [*derivations, *qualified_pairs]:
        if derived == source:
            raise ValueError("generation self-dependence")
        parents.setdefault(derived, set()).add(source)
        before = generated.get(source, {}).get("position")
        after = generated.get(derived, {}).get("position")
        if before is not None and after is not None and before >= after:
            raise ValueError("derivation generation order")
    # A small explicit reachability walk stays independent of production SQL.
    for entity in parents:
        pending = list(parents[entity])
        seen = set()
        while pending:
            parent = pending.pop()
            if parent == entity:
                raise ValueError("generation cycle")
            before = generated.get(parent, {}).get("position")
            after = generated.get(entity, {}).get("position")
            if before is not None and after is not None and before >= after:
                raise ValueError("transitive derivation generation order")
            if parent not in seen:
                seen.add(parent)
                pending.extend(parents.get(parent, ()))
