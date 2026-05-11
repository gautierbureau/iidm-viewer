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


def get_dataframe(network: NetworkProxy, component: str):
    """Return the pandas DataFrame for ``component``, with the equipment
    id surfaced as a regular ``id`` column.

    Returns an empty DataFrame for unknown component types or absent
    extensions (rather than raising) so the UI can show a clean
    "no rows" state.
    """
    import pandas as pd

    getter_name = COMPONENT_TYPES.get(component)
    if getter_name is None:
        return pd.DataFrame()
    raw = object.__getattribute__(network, "_obj")

    def _do():
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
