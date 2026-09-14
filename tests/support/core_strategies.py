"""Bounded generated Core cases; production must not depend on this module."""

from hypothesis import strategies as st


scalar_strings = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=24)
json_values = st.recursive(
    st.none() | st.booleans() | st.integers(min_value=-(2**53 - 1), max_value=2**53 - 1) | scalar_strings,
    lambda child: st.lists(child, max_size=5) | st.dictionaries(scalar_strings, child, max_size=5),
    max_leaves=20,
)


@st.composite
def keyed_roots(draw):
    values = draw(st.lists(json_values, max_size=12))
    return [(f"key-{i}", f"occurrence-{i}", value) for i, value in enumerate(values)]


@st.composite
def membership_histories(draw):
    """Return valid ordered edits plus independently tracked final membership."""
    initial = {"a": "e0", "b": "e1"}
    final = initial.copy()
    edits = []
    instructions = draw(st.lists(st.tuples(st.sampled_from(("a", "b", "c")), st.booleans()), max_size=25))
    for sequence, (key, remove) in enumerate(instructions):
        if remove and key in final:
            edits.append({"sequence": sequence, "member_key": key, "op": "remove"})
            del final[key]
        else:
            entity = draw(st.sampled_from(("e0", "e1", "e2")))
            edits.append({"sequence": sequence, "member_key": key, "op": "put", "occurrence_id": entity})
            final[key] = entity
    return initial, edits, final
