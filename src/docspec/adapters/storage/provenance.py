"""Admission indexes for actual Core provenance, in the metadata transaction.

The immutable Result records retain the statements. These indexes enforce the
supported PROV event constraints and Core's acyclic generation dependencies.
Bindings and dependency declarations never manufacture edges here.
"""

from graphlib import CycleError, TopologicalSorter

from docspec.domain.core_admission import event_instant
from docspec.errors import IntegrityError


PROVENANCE_SCHEMA = (
    """CREATE TABLE provenance_events (
        event_id TEXT PRIMARY KEY, event_kind TEXT NOT NULL CHECK(event_kind IN ('generation','usage')),
        entity_id TEXT NOT NULL, execution_id TEXT NOT NULL, instant INTEGER,
        execution_kind TEXT NOT NULL DEFAULT 'execution' CHECK(execution_kind='execution'),
        FOREIGN KEY(execution_kind,execution_id) REFERENCES records(kind,record_id)
    ) WITHOUT ROWID""",
    "CREATE UNIQUE INDEX entity_generation ON provenance_events(entity_id) WHERE event_kind='generation'",
    "CREATE INDEX entity_events ON provenance_events(entity_id,event_kind)",
    """CREATE TABLE derivations (
        child TEXT NOT NULL, parent TEXT NOT NULL, PRIMARY KEY(child,parent), CHECK(child!=parent)
    ) WITHOUT ROWID""",
    "CREATE INDEX derivation_children ON derivations(parent,child)",
)


def admit_provenance(connection, results) -> None:
    """Index one unit's PROV events and derivations, refusing conflicts, cycles and out-of-order generations."""

    events, edges, qualified = {}, set(), set()
    for result in results:
        for field, kind in (("generations", "generation"), ("usages", "usage")):
            for event in result[field]:
                row = (event["event_id"], kind, event["entity_id"], result["execution_id"], event_instant(event["happened_at"]))
                if row[0] in events and events[row[0]] != row:
                    raise IntegrityError("conflicting provenance event identity")
                events[row[0]] = row
        for edge in result["derivations"]:
            edges.add((edge["generated_entity_id"], edge["used_entity_id"]))
            if edge["usage_event_id"] is not None and edge["generation_event_id"] is not None:
                qualified.add((edge["usage_event_id"], edge["generation_event_id"]))
    if not events and not edges:
        return
    generation_ids = {row[2] for row in events.values() if row[1] == "generation"}
    if len(generation_ids) != sum(row[1] == "generation" for row in events.values()):
        raise IntegrityError("conflicting generation of an entity within the publication unit")
    connection.execute("CREATE TEMP TABLE incoming_events (event_id TEXT PRIMARY KEY,event_kind TEXT,entity_id TEXT,execution_id TEXT,instant INTEGER)")
    connection.executemany("INSERT INTO incoming_events VALUES (?,?,?,?,?)", events.values())
    if connection.execute(
        "SELECT 1 FROM incoming_events i JOIN provenance_events e USING(event_id) "
        "WHERE i.event_kind!=e.event_kind OR i.entity_id!=e.entity_id OR i.execution_id!=e.execution_id OR i.instant IS NOT e.instant LIMIT 1"
    ).fetchone():
        raise IntegrityError("conflicting provenance event identity")
    if connection.execute(
        "SELECT 1 FROM incoming_events i JOIN provenance_events e USING(entity_id) "
        "WHERE i.event_kind='generation' AND e.event_kind='generation' AND i.event_id!=e.event_id LIMIT 1"
    ).fetchone():
        raise IntegrityError("conflicting generation of an existing entity")
    connection.execute(
        "INSERT INTO provenance_events(event_id,event_kind,entity_id,execution_id,instant) "
        "SELECT * FROM incoming_events WHERE true ON CONFLICT(event_id) DO NOTHING"
    )
    if connection.execute(
        "SELECT 1 FROM (SELECT DISTINCT entity_id FROM incoming_events) changed "
        "JOIN provenance_events g ON g.entity_id=changed.entity_id AND g.event_kind='generation' "
        "JOIN provenance_events u ON u.entity_id=changed.entity_id AND u.event_kind='usage' "
        "WHERE g.instant>u.instant LIMIT 1"
    ).fetchone():
        raise IntegrityError("usage precedes the entity's generation")
    if any(events[before][4] is not None and events[after][4] is not None and events[before][4] > events[after][4] for before, after in qualified):
        raise IntegrityError("derived output precedes its qualified input usage")
    connection.executemany("INSERT INTO derivations VALUES (?,?) ON CONFLICT DO NOTHING", edges)
    if not edges and not generation_ids:
        return
    connection.execute("CREATE TEMP TABLE provenance_roots (direction TEXT,entity_id TEXT,PRIMARY KEY(direction,entity_id))")
    roots = {("up", parent) for _, parent in edges} | {("down", child) for child, _ in edges}
    roots.update((direction, entity) for entity in generation_ids for direction in ("up", "down"))
    connection.executemany("INSERT INTO provenance_roots VALUES (?,?)", roots)
    connection.execute("CREATE TEMP TABLE provenance_scope (child TEXT,parent TEXT,PRIMARY KEY(child,parent))")
    connection.executemany("INSERT INTO provenance_scope VALUES (?,?)", edges)
    # Walk each relevant node once per direction, not once per proposed edge.
    # UNION terminates even for a newly introduced cycle, which graphlib rejects.
    for direction, head, tail in (("up", "child", "parent"), ("down", "parent", "child")):
        connection.execute(
            "WITH RECURSIVE scope(entity_id) AS (SELECT entity_id FROM provenance_roots WHERE direction=? "
            f"UNION SELECT d.{tail} FROM derivations d JOIN scope s ON d.{head}=s.entity_id) "
            f"INSERT INTO provenance_scope SELECT d.child,d.parent FROM derivations d JOIN scope s ON d.{head}=s.entity_id "
            "WHERE true ON CONFLICT DO NOTHING", (direction,),
        )
    parents = {}
    for child, parent in connection.execute("SELECT child,parent FROM provenance_scope"):
        parents.setdefault(child, set()).add(parent)
        parents.setdefault(parent, set())
    instants = dict(connection.execute(
        "SELECT e.entity_id,e.instant FROM provenance_events e JOIN "
        "(SELECT child AS entity_id FROM provenance_scope UNION SELECT parent FROM provenance_scope) s USING(entity_id) "
        "WHERE e.event_kind='generation' AND e.instant IS NOT NULL"
    ))
    latest = {}
    try:
        for entity in TopologicalSorter(parents).static_order():
            before = max((latest[parent] for parent in parents[entity] if latest[parent] is not None), default=None)
            own = instants.get(entity)
            if own is not None and before is not None and own <= before:
                raise IntegrityError("derived entity generation must follow its ancestors' generation")
            latest[entity] = own if own is not None else before
    except CycleError as error:
        raise IntegrityError("generation dependencies contain a cycle") from error
