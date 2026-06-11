"""Framework-agnostic pypowsybl component registry.

Source of truth — for new code — for which network components the
Data Explorer tabs surface and which attributes are editable. The
existing Streamlit modules (``network_info.COMPONENT_TYPES``,
``state.EDITABLE_COMPONENTS``) currently keep their own copies; the
PySide6 and NiceGUI prototypes import from here, and a future cleanup
pass can fold the Streamlit dicts into this module too.

No streamlit / PySide6 / NiceGUI imports — this module is safe to
pull into any front-end without dragging a UI framework along.
"""
from __future__ import annotations

from typing import Any, Optional

from iidm_viewer.powsybl_worker import NetworkProxy, run


# Component label -> pypowsybl getter method name. 1:1 with
# ``network_info.COMPONENT_TYPES``.
COMPONENT_TYPES: dict[str, str] = {
    "Substations": "get_substations",
    "Voltage Levels": "get_voltage_levels",
    "Buses": "get_buses",
    "Busbar Sections": "get_busbar_sections",
    "Generators": "get_generators",
    "Loads": "get_loads",
    "Lines": "get_lines",
    "2-Winding Transformers": "get_2_windings_transformers",
    "3-Winding Transformers": "get_3_windings_transformers",
    "Switches": "get_switches",
    "Shunt Compensators": "get_shunt_compensators",
    "Static VAR Compensators": "get_static_var_compensators",
    "HVDC Lines": "get_hvdc_lines",
    "VSC Converter Stations": "get_vsc_converter_stations",
    "LCC Converter Stations": "get_lcc_converter_stations",
    "Batteries": "get_batteries",
    "Dangling Lines": "get_dangling_lines",
    "Tie Lines": "get_tie_lines",
}


# Inverse of :data:`COMPONENT_TYPES` — pypowsybl getter method name →
# component label. Used by the Streamlit ``add_to_change_log`` path to
# derive the component label from a method name when writing into the
# shared :class:`iidm_viewer.change_log.ChangeLog`. ``Switches`` lives
# under :data:`EDITABLE_COMPONENTS` (it's editable but not surfaced as
# a Data Explorer tab) so add it explicitly to keep the switch-toggle
# path symmetric with the cell-edit path.
LABEL_FOR_METHOD: dict[str, str] = {
    method: label for label, method in COMPONENT_TYPES.items()
}
LABEL_FOR_METHOD["get_switches"] = "Switches"


# Component label -> (update method name, list of editable attribute names).
# Mirrors ``state.EDITABLE_COMPONENTS``. Voltage Levels and Substations are
# intentionally omitted: their pypowsybl update story is different
# (``update_voltage_levels`` exists but its editable surface is small and
# specialised; the Streamlit path keeps a special-case handler). The MVP
# Data Explorer edit support focuses on the common-case injection /
# branch attributes.
EDITABLE_COMPONENTS: dict[str, tuple[str, list[str]]] = {
    "Loads": ("update_loads", ["p0", "q0", "connected"]),
    "Generators": (
        "update_generators",
        ["target_p", "target_v", "target_q", "voltage_regulator_on",
         "regulated_element_id", "connected"],
    ),
    "Batteries": ("update_batteries", ["target_p", "target_q", "connected"]),
    "Switches": ("update_switches", ["open"]),
    "Shunt Compensators": (
        "update_shunt_compensators",
        ["section_count", "connected"],
    ),
    "Static VAR Compensators": (
        "update_static_var_compensators",
        ["regulation_mode", "voltage_setpoint", "reactive_power_setpoint",
         "regulated_element_id", "connected"],
    ),
    "VSC Converter Stations": (
        "update_vsc_converter_stations",
        ["target_v", "target_q", "voltage_regulator_on",
         "regulated_element_id", "connected"],
    ),
    "LCC Converter Stations": (
        "update_lcc_converter_stations",
        ["power_factor", "connected"],
    ),
    "HVDC Lines": (
        "update_hvdc_lines",
        ["active_power_setpoint", "converters_mode"],
    ),
    "Dangling Lines": ("update_dangling_lines", ["p0", "q0", "connected"]),
    "Lines": (
        "update_lines",
        ["r", "x", "g1", "b1", "g2", "b2", "connected1", "connected2"],
    ),
    "2-Winding Transformers": (
        "update_2_windings_transformers",
        ["r", "x", "g", "b", "connected1", "connected2"],
    ),
}


# Editable cells whose change can perturb the SVG geometry of NAD /
# SLD diagrams (connected/open flips a switch state, which changes
# the drawn line style). Used by prototypes to decide whether to
# invalidate their per-VL diagram caches after an edit.
TOPOLOGY_AFFECTING_ATTRIBUTES: frozenset[str] = frozenset({
    "connected", "connected1", "connected2", "open",
    "voltage_regulator_on", "section_count",
})


# ---------------------------------------------------------------------------
# Disconnect / delete registries
# ---------------------------------------------------------------------------
# Per-component "disconnect" target — the attribute(s) to flip + value(s)
# that mean "this element is no longer carrying power". Reused by
# ``apply_bulk_disconnect`` and by the disconnect buttons in both
# prototypes' bulk panels.
DISCONNECT_ATTRS: dict[str, dict[str, Any]] = {
    "Loads": {"connected": False},
    "Generators": {"connected": False},
    "Batteries": {"connected": False},
    "Shunt Compensators": {"connected": False},
    "Static VAR Compensators": {"connected": False},
    "VSC Converter Stations": {"connected": False},
    "LCC Converter Stations": {"connected": False},
    "Dangling Lines": {"connected": False},
    "Switches": {"open": True},
    "Lines": {"connected1": False, "connected2": False},
    "2-Winding Transformers": {"connected1": False, "connected2": False},
}

DISCONNECTABLE_COMPONENTS: frozenset[str] = frozenset(DISCONNECT_ATTRS)


# Injection types that ``pn.remove_feeder_bays`` knows how to deep-remove
# (the element plus its bay breaker + disconnector chain).
_FEEDER_BAY_TYPES: frozenset[str] = frozenset({
    "Loads",
    "Generators",
    "Batteries",
    "Shunt Compensators",
    "Static VAR Compensators",
})

# HVDC triples: removing any one of the three elements (line or either
# converter station) cascades to remove all three.
_HVDC_TYPES: frozenset[str] = frozenset({
    "HVDC Lines",
    "VSC Converter Stations",
    "LCC Converter Stations",
})

# Branch / link types removed via the generic ``raw.remove_elements`` API.
_SHALLOW_REMOVE_TYPES: frozenset[str] = frozenset({
    "Lines",
    "2-Winding Transformers",
    "Dangling Lines",
})

REMOVABLE_COMPONENTS: frozenset[str] = (
    _FEEDER_BAY_TYPES | _HVDC_TYPES | _SHALLOW_REMOVE_TYPES
    | {"Voltage Levels", "Substations"}
)


def is_editable(component: str, attribute: Optional[str] = None) -> bool:
    """``True`` if the component (or specific attribute) is editable."""
    entry = EDITABLE_COMPONENTS.get(component)
    if entry is None:
        return False
    if attribute is None:
        return True
    return attribute in entry[1]


def editable_attributes(component: str) -> list[str]:
    entry = EDITABLE_COMPONENTS.get(component)
    return list(entry[1]) if entry else []


def get_dataframe(
    network: NetworkProxy, component: str, *, variant_id: Optional[str] = None,
):
    """Return the pandas DataFrame for ``component``, with the equipment
    id surfaced as a regular ``id`` column.

    ``variant_id`` (kw-only): when set to a non-InitialState variant,
    the fetch happens against that variant — switch + read + restore
    atomically inside one worker round-trip. Defaults to ``None`` /
    InitialState (the today behaviour, no extra hop).

    Returns an empty DataFrame for unknown component types or absent
    extensions (rather than raising) so the UI can show a clean
    "no rows" state.
    """
    import pandas as pd

    from iidm_viewer.variants import with_variant

    getter_name = COMPONENT_TYPES.get(component)
    if getter_name is None:
        return pd.DataFrame()
    raw = object.__getattribute__(network, "_obj")

    def _do():
        with with_variant(raw, variant_id):
            method = getattr(raw, getter_name, None)
            if method is None:
                return pd.DataFrame()
            df = method()
            if df is not None and df.index.name:
                df = df.reset_index()
            return df if df is not None else pd.DataFrame()

    return run(_do)


def _coerce(raw_value: Any, dtype) -> Any:
    """Best-effort coercion of a user-typed value to ``dtype``.

    Accepts strings (from a `QLineEdit` or ag-Grid text edit) and
    already-typed values (numbers, bools from ag-Grid's typed cells).
    Booleans accept the usual truthy/falsey words and the
    domain-specific ``open``/``closed`` pair.
    """
    if raw_value is None:
        return None
    kind = getattr(dtype, "kind", None)
    if kind == "b":
        if isinstance(raw_value, bool):
            return raw_value
        s = str(raw_value).strip().lower()
        if s in ("true", "t", "1", "yes", "y", "open"):
            return True
        if s in ("false", "f", "0", "no", "n", "closed"):
            return False
        raise ValueError(f"cannot interpret {raw_value!r} as bool")
    if kind in ("i", "u"):
        if isinstance(raw_value, bool):
            return int(raw_value)
        return int(raw_value)
    if kind == "f":
        return float(raw_value)
    return str(raw_value)


def apply_bulk_edit(
    network: NetworkProxy,
    component: str,
    element_ids: list[str],
    attribute: str,
    new_value: Any,
) -> dict[str, Any]:
    """Apply the same edit to every id in ``element_ids`` in a single
    vectorised ``update_<component>`` call.

    Returns ``{element_id: previous_value}`` for any id whose previous
    value could be read — hosts can use it to populate a change log or
    revert. Coercion happens once against the column's dtype, so all
    rows receive the same typed value.

    The whole read + write pair runs on the pypowsybl worker thread,
    same constraint as :func:`apply_cell_edit`.
    """
    if component not in EDITABLE_COMPONENTS:
        raise ValueError(f"component {component!r} is not editable")
    method_name, editable_attrs = EDITABLE_COMPONENTS[component]
    if attribute not in editable_attrs:
        raise ValueError(
            f"attribute {attribute!r} is not editable for {component}"
        )
    ids = [str(eid) for eid in element_ids]
    if not ids:
        return {}
    getter_name = COMPONENT_TYPES[component]
    raw = object.__getattribute__(network, "_obj")

    def _do():
        import pandas as pd

        prev: dict[str, Any] = {}
        coerced = new_value
        getter = getattr(raw, getter_name, None)
        if getter is not None:
            df = getter()
            if df is not None and attribute in df.columns:
                coerced = _coerce(new_value, df[attribute].dtype)
                for eid in ids:
                    if eid in df.index:
                        prev[eid] = df.at[eid, attribute]

        update_method = getattr(raw, method_name)
        changes = pd.DataFrame(
            {attribute: [coerced] * len(ids)},
            index=pd.Index(ids, name="id"),
        )
        update_method(changes)
        return prev

    return run(_do)


def apply_bulk_disconnect(
    network: NetworkProxy,
    component: str,
    element_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Disconnect every id in ``element_ids`` using the right pypowsybl
    attribute(s) for the component type.

    Returns ``{attribute: {element_id: previous_value}}`` for the
    caller's change log — one inner map per attribute touched
    (Lines / 2-Winding Transformers flip two attributes; everything
    else flips one). Worker-thread bound.
    """
    if component not in DISCONNECT_ATTRS:
        raise ValueError(
            f"component {component!r} has no bulk-disconnect attribute"
        )
    if not element_ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for attribute, target in DISCONNECT_ATTRS[component].items():
        out[attribute] = apply_bulk_edit(
            network, component, element_ids, attribute, target,
        )
    return out


def _find_vl_ids_for_substations(
    network: NetworkProxy, substation_ids: list[str],
) -> list[str]:
    """Return all voltage-level ids that belong to the given substations."""
    raw = object.__getattribute__(network, "_obj")
    sub_set = set(substation_ids)

    def _gather():
        vl_df = raw.get_voltage_levels()
        if vl_df.empty or "substation_id" not in vl_df.columns:
            return []
        return vl_df[vl_df["substation_id"].isin(sub_set)].index.tolist()

    return run(_gather)


def _resolve_hvdc_removal(
    network: NetworkProxy, component: str, ids: list[str],
) -> tuple[list[str], list[str]]:
    """Expand an HVDC removal request to the full triple: stations + line.

    Returns ``(station_ids, hvdc_line_ids)``. Removing any element in
    an HVDC set cascades to all three; ``pn.remove_hvdc_lines`` handles
    the line + both stations in one call.
    """
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        hvdc_df = raw.get_hvdc_lines()
        hvdc_to_stations: dict[str, tuple[str, str]] = {}
        station_to_hvdc: dict[str, str] = {}
        for hvdc_id in hvdc_df.index:
            cs1 = hvdc_df.at[hvdc_id, "converter_station1_id"]
            cs2 = hvdc_df.at[hvdc_id, "converter_station2_id"]
            hvdc_to_stations[hvdc_id] = (cs1, cs2)
            station_to_hvdc[cs1] = hvdc_id
            station_to_hvdc[cs2] = hvdc_id
        return hvdc_to_stations, station_to_hvdc

    hvdc_to_stations, station_to_hvdc = run(_gather)

    hvdc_ids: set[str] = set()
    for eid in ids:
        if component == "HVDC Lines":
            hvdc_ids.add(eid)
        else:
            hvdc_id = station_to_hvdc.get(eid)
            if hvdc_id:
                hvdc_ids.add(hvdc_id)

    station_ids: set[str] = set()
    for hvdc_id in hvdc_ids:
        cs1, cs2 = hvdc_to_stations.get(hvdc_id, (None, None))
        if cs1:
            station_ids.add(cs1)
        if cs2:
            station_ids.add(cs2)

    return list(station_ids), list(hvdc_ids)


def remove_elements(
    network: NetworkProxy,
    component: str,
    ids: list[str],
) -> list[str]:
    """Remove ``ids`` from the network on the worker thread.

    Returns the full list of element ids that were actually removed,
    which can be larger than ``ids`` for HVDC, Voltage Level, and
    Substation cascades:

    * Feeder-bay injections → ``pn.remove_feeder_bays`` (deep removal,
      drops the bay breaker + disconnectors too).
    * HVDC triples → ``pn.remove_hvdc_lines`` (handles line + both
      converter stations as one set).
    * Voltage Levels → ``pn.remove_voltage_levels`` (cascades every
      connectable inside the VL).
    * Substations → resolve contained VLs first, then
      ``pn.remove_voltage_levels``.
    * Branches → ``raw.remove_elements`` (shallow).

    Raises ``ValueError`` for unknown component types so the UI can
    fail loud rather than silently no-op.
    """
    if component not in REMOVABLE_COMPONENTS:
        raise ValueError(f"component {component!r} is not removable")
    if not ids:
        return []
    raw = object.__getattribute__(network, "_obj")

    if component in _FEEDER_BAY_TYPES:
        def _do():
            import pypowsybl.network as pn
            pn.remove_feeder_bays(raw, ids)
        run(_do)
        return list(ids)

    if component in _HVDC_TYPES:
        station_ids, hvdc_line_ids = _resolve_hvdc_removal(network, component, ids)

        def _do():
            import pypowsybl.network as pn
            pn.remove_hvdc_lines(raw, hvdc_line_ids)
        run(_do)
        return station_ids + hvdc_line_ids

    if component == "Voltage Levels":
        def _do():
            import pypowsybl.network as pn
            pn.remove_voltage_levels(raw, ids)
        run(_do)
        return list(ids)

    if component == "Substations":
        vl_ids = _find_vl_ids_for_substations(network, ids)

        def _do():
            import pypowsybl.network as pn
            if vl_ids:
                pn.remove_voltage_levels(raw, vl_ids)
        run(_do)
        return list(ids) + vl_ids

    # _SHALLOW_REMOVE_TYPES — branches and dangling lines.
    def _do():
        raw.remove_elements(ids)

    run(_do)
    return list(ids)


def toggle_switch(
    network: NetworkProxy,
    switch_id: str,
    new_open: bool,
) -> tuple[bool, bool]:
    """Open or close a single switch; return ``(before_open, after_open)``.

    The Streamlit SLD breaker-click handler and both prototypes' SLD
    handlers share this entry point so the toggle behaviour stays
    identical: one worker hop reads the previous state, one ``Switches``
    update applies the new one, and the caller receives both values
    so it can populate its change log.
    """
    raw = object.__getattribute__(network, "_obj")

    def _do() -> tuple[bool, bool]:
        df = raw.get_switches(attributes=["open"])
        if switch_id not in df.index:
            raise KeyError(f"Switch {switch_id!r} not found in network")
        before = bool(df.at[switch_id, "open"])
        import pandas as pd
        changes = pd.DataFrame(
            {"open": [new_open]},
            index=pd.Index([switch_id], name="id"),
        )
        raw.update_switches(changes)
        return before, new_open

    return run(_do)


def apply_cell_edit(
    network: NetworkProxy,
    component: str,
    element_id: str,
    attribute: str,
    new_value: Any,
) -> Any:
    """Apply a single-cell edit. Returns the previous value.

    Runs the read + write pair on the pypowsybl worker thread so
    the GraalVM isolate's thread affinity (AGENTS.md §1) is honoured.

    Raises ``ValueError`` if the component or attribute isn't editable.
    Raises whatever pypowsybl raises on update failure (e.g. invalid
    value, validation failure).
    """
    if component not in EDITABLE_COMPONENTS:
        raise ValueError(f"component {component!r} is not editable")
    method_name, editable_attrs = EDITABLE_COMPONENTS[component]
    if attribute not in editable_attrs:
        raise ValueError(
            f"attribute {attribute!r} is not editable for {component}"
        )
    getter_name = COMPONENT_TYPES[component]
    raw = object.__getattribute__(network, "_obj")

    def _do():
        import pandas as pd

        # Read previous value so the host can revert or log.
        prev = None
        getter = getattr(raw, getter_name, None)
        if getter is not None:
            df = getter()
            if df is not None and element_id in df.index and attribute in df.columns:
                prev = df.at[element_id, attribute]
                coerced = _coerce(new_value, df[attribute].dtype)
            else:
                coerced = new_value
        else:
            coerced = new_value

        update_method = getattr(raw, method_name)
        changes = pd.DataFrame(
            {attribute: [coerced]},
            index=pd.Index([element_id], name="id"),
        )
        update_method(changes)
        return prev

    return run(_do)
