"""Analysis passes that derive higher-level structure from a built CallGraph.

These are render-agnostic, unit-testable computations (no browser / JS needed).
"""
from .var_flow_interproc import (
    build_interprocedural_flow,
    build_scope_groups,
    extract_base_var_name,
    extract_full_var_name,
    scope_identity,
)

__all__ = [
    "build_interprocedural_flow",
    "build_scope_groups",
    "extract_base_var_name",
    "extract_full_var_name",
    "scope_identity",
]
