"""Shared deterministic ordering for bounded operation graphs."""

from graphlib import CycleError, TopologicalSorter


def operation_order(dependencies, *, label="operation dependencies"):
    """Return a deterministic topological order, refusing unknown or cyclic dependencies."""

    unknown = set().union(*(set(parents) for parents in dependencies.values())) - dependencies.keys() if dependencies else set()
    if unknown:
        raise ValueError(f"{label} include unknown operations: {sorted(unknown)}")
    graph = TopologicalSorter(dependencies)
    try:
        graph.prepare()
    except CycleError as error:
        raise ValueError(f"{label} must form an acyclic graph") from error
    result = []
    while graph.is_active():
        ready = sorted(graph.get_ready())
        result.extend(ready)
        graph.done(*ready)
    return tuple(result)
