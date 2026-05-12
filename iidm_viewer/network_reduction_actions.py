"""Framework-agnostic network-reduction helpers.

pypowsybl exposes three irreversible reduction methods on a Network
object:

* ``reduce_by_voltage_range(v_min, v_max, with_boundary_lines)`` — keep
  every element whose nominal voltage falls inside the band.
* ``reduce_by_ids(ids, with_boundary_lines)`` — keep only the specified
  voltage levels and the elements between them.
* ``reduce_by_ids_and_depths(vl_depths, with_boundary_lines)`` — keep
  the specified voltage levels and their neighbours up to a given
  hop count.

This module wraps each one with a thin worker-routed call (via the
existing :class:`NetworkProxy`), a pure validator that returns a list
of human-readable error strings, and a helper that lists the available
voltage level ids. The Streamlit, PySide6 and NiceGUI dialogs share
these — only the per-host widget rendering lives elsewhere.
"""
from __future__ import annotations

from typing import Iterable

from iidm_viewer.powsybl_worker import NetworkProxy


REDUCTION_METHODS: list[str] = [
    "By Voltage Range",
    "By Voltage Level IDs",
    "By Voltage Level IDs and Depths",
]


def list_voltage_level_ids(network: NetworkProxy) -> list[str]:
    """Return every voltage level id in the network, alphabetical order.

    Used by the "By IDs" / "By IDs and Depths" mode pickers to populate
    a multi-select. Falls back to an empty list on probe failures so
    the dialog can render a friendly empty state.
    """
    try:
        vls = network.get_voltage_levels()
    except Exception:
        return []
    if vls is None or vls.empty:
        return []
    return sorted(str(v) for v in vls.index.tolist())


# ---------------------------------------------------------------------------
# Validators (pure — same shape as the rest of the project)
# ---------------------------------------------------------------------------
def validate_reduce_by_voltage_range(v_min: float, v_max: float) -> list[str]:
    errors: list[str] = []
    try:
        v_min_f = float(v_min)
        v_max_f = float(v_max)
    except (TypeError, ValueError):
        errors.append("v_min and v_max must be numeric.")
        return errors
    if v_min_f < 0 or v_max_f < 0:
        errors.append("v_min and v_max must be non-negative.")
    if v_min_f >= v_max_f:
        errors.append("Minimum voltage must be less than maximum voltage.")
    return errors


def validate_reduce_by_ids(ids: Iterable) -> list[str]:
    errors: list[str] = []
    items = [i for i in (ids or []) if i]
    if not items:
        errors.append("Select at least one voltage level.")
    return errors


def validate_reduce_by_ids_and_depths(
    ids: Iterable, depth: int,
) -> list[str]:
    errors: list[str] = []
    items = [i for i in (ids or []) if i]
    if not items:
        errors.append("Select at least one voltage level.")
    try:
        d = int(depth)
    except (TypeError, ValueError):
        errors.append("Depth must be an integer.")
        return errors
    if d < 0:
        errors.append("Depth must be non-negative.")
    return errors


# ---------------------------------------------------------------------------
# Worker-routed reductions
# ---------------------------------------------------------------------------
def reduce_by_voltage_range(
    network: NetworkProxy,
    v_min: float,
    v_max: float,
    with_boundary_lines: bool = False,
) -> None:
    """Apply ``reduce_by_voltage_range`` after validating. Mutates the
    network in place — irreversible."""
    errors = validate_reduce_by_voltage_range(v_min, v_max)
    if errors:
        raise ValueError("; ".join(errors))
    # NetworkProxy auto-routes the call through the pypowsybl worker.
    network.reduce_by_voltage_range(
        v_min=float(v_min),
        v_max=float(v_max),
        with_boundary_lines=bool(with_boundary_lines),
    )


def reduce_by_ids(
    network: NetworkProxy,
    ids: Iterable,
    with_boundary_lines: bool = False,
) -> None:
    """Apply ``reduce_by_ids`` after validating. Irreversible."""
    items = [str(i) for i in (ids or []) if i]
    errors = validate_reduce_by_ids(items)
    if errors:
        raise ValueError("; ".join(errors))
    network.reduce_by_ids(
        ids=items,
        with_boundary_lines=bool(with_boundary_lines),
    )


def reduce_by_ids_and_depths(
    network: NetworkProxy,
    ids: Iterable,
    depth: int,
    with_boundary_lines: bool = False,
) -> None:
    """Apply ``reduce_by_ids_and_depths`` after validating. ``depth`` is
    applied to every selected voltage level. Irreversible."""
    items = [str(i) for i in (ids or []) if i]
    errors = validate_reduce_by_ids_and_depths(items, depth)
    if errors:
        raise ValueError("; ".join(errors))
    vl_depths = [(vl, int(depth)) for vl in items]
    network.reduce_by_ids_and_depths(
        vl_depths=vl_depths,
        with_boundary_lines=bool(with_boundary_lines),
    )
