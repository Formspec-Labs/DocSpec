"""Lazy exports for DocSpec-owned transforms.

Importing one document transform must not initialize optional tagging or
RefSpec integrations.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "build_authority_edges": "build_authority_edges",
    "build_comment_periods": "build_comment_periods",
    "build_concept_assignments": "build_concept_assignments",
    "build_concept_events": "build_concept_events",
    "build_concepts": "build_concepts",
    "build_proceedings": "build_proceedings",
    "build_regulatory_agenda": "build_regulatory_agenda",
    "build_rule_targets": "build_rule_targets",
    "build_supreme_court_opinions": "build_supreme_court_opinions",
}

__all__ = sorted(_EXPORT_MODULES)


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"docspec.transforms.{module_name}"), name)
    globals()[name] = value
    return value
