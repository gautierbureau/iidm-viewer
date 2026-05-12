"""Framework-agnostic registry + helpers for creating new components.

This module owns the schema Streamlit's data explorer renders as a
"Create a new <component>" form. The PySide6 and NiceGUI prototypes
consume the same registry and the same dispatch path; each host only
ships its own widget renderer over the shared field specs.

Field shape (subset of what Streamlit's ``_render_field`` understands):

    {
        "name": str,         # attribute name in the bay-creation DataFrame
        "label": str,        # human-readable label
        "kind": "text" | "float" | "int" | "bool" | "select",
        "required": bool,
        "default": Any,
        "options": list[str],     # required for "select"
        "min_value": Number,      # optional, "float" / "int"
        "step": int,              # optional, "int"
        "help": str,              # optional
    }

The dispatcher (:func:`create_component_bay`) translates the user's
filled-in dict into the right pypowsybl ``create_*_bay`` call,
running on the worker thread per AGENTS.md §1.

No streamlit / Qt / NiceGUI imports.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import pandas as pd

from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Option lists used by the registry
# ---------------------------------------------------------------------------
ENERGY_SOURCES = ["OTHER", "HYDRO", "NUCLEAR", "WIND", "SOLAR", "THERMAL"]
FEEDER_DIRECTIONS = ["TOP", "BOTTOM"]
LOAD_TYPES = ["UNDEFINED", "AUXILIARY", "FICTITIOUS"]
SVC_REGULATION_MODES = ["VOLTAGE", "REACTIVE_POWER"]

_POSITION_HELP = (
    "Order of this feeder on the busbar (ConnectablePosition extension)."
)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
def _validate_minmax_p(fields: dict) -> list[str]:
    errors: list[str] = []
    if fields.get("max_p") is not None and fields.get("min_p") is not None:
        if fields["max_p"] < fields["min_p"]:
            errors.append("max_p must be >= min_p.")
    return errors


def _validate_voltage_regulator(fields: dict) -> list[str]:
    """voltage_regulator_on=True requires target_v > 0."""
    errors: list[str] = []
    if fields.get("voltage_regulator_on"):
        if not fields.get("target_v") or fields["target_v"] <= 0:
            errors.append("target_v must be > 0 when voltage regulator is on.")
    return errors


def _validate_generator(fields: dict) -> list[str]:
    return _validate_minmax_p(fields) + _validate_voltage_regulator(fields)


def _validate_svc(fields: dict) -> list[str]:
    errors: list[str] = []
    if fields.get("b_max") is not None and fields.get("b_min") is not None:
        if fields["b_max"] < fields["b_min"]:
            errors.append("b_max must be >= b_min.")
    if fields.get("regulating") and fields.get("regulation_mode") == "VOLTAGE":
        if not fields.get("target_v") or fields["target_v"] <= 0:
            errors.append("target_v must be > 0 when regulating in VOLTAGE mode.")
    if fields.get("regulating") and fields.get("regulation_mode") == "REACTIVE_POWER":
        if fields.get("target_q") is None:
            errors.append("target_q is required when regulating in REACTIVE_POWER mode.")
    return errors


def _validate_shunt(fields: dict) -> list[str]:
    errors: list[str] = []
    section_count = fields.get("section_count")
    max_sc = fields.get("max_section_count")
    if section_count is not None and max_sc is not None and section_count > max_sc:
        errors.append("Initial section count must be <= max_section_count.")
    return errors


_VALIDATORS: dict[str, Callable[[dict], list[str]]] = {
    "_validate_generator": _validate_generator,
    "_validate_minmax_p": _validate_minmax_p,
    "_validate_voltage_regulator": _validate_voltage_regulator,
    "_validate_svc": _validate_svc,
    "_validate_shunt": _validate_shunt,
}


# ---------------------------------------------------------------------------
# CREATABLE_COMPONENTS registry
# ---------------------------------------------------------------------------
CREATABLE_COMPONENTS: dict[str, dict] = {
    "Generators": {
        "bay_function": "create_generator_bay",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "energy_source", "label": "Energy source", "kind": "select",
             "required": False, "default": "OTHER", "options": ENERGY_SOURCES},
            {"name": "min_p", "label": "min_p (MW)", "kind": "float", "required": True, "default": 0.0},
            {"name": "max_p", "label": "max_p (MW)", "kind": "float", "required": True, "default": 100.0},
            {"name": "target_p", "label": "target_p (MW)", "kind": "float", "required": True, "default": 0.0},
            {"name": "voltage_regulator_on", "label": "Voltage regulator on",
             "kind": "bool", "required": True, "default": False},
            {"name": "target_v", "label": "target_v (kV)", "kind": "float",
             "required": False, "default": 0.0,
             "help": "Required when voltage regulator is on."},
            {"name": "target_q", "label": "target_q (MVar)", "kind": "float",
             "required": False, "default": 0.0,
             "help": "Used when the generator does not regulate voltage."},
            {"name": "rated_s", "label": "rated_s (MVA, 0 = unset)",
             "kind": "float", "required": False, "default": 0.0,
             "min_value": 0.0},
        ],
        "validate": "_validate_generator",
    },
    "Loads": {
        "bay_function": "create_load_bay",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "type", "label": "Type", "kind": "select",
             "required": False, "default": "UNDEFINED", "options": LOAD_TYPES},
            {"name": "p0", "label": "p0 (MW)", "kind": "float", "required": True, "default": 0.0},
            {"name": "q0", "label": "q0 (MVar)", "kind": "float", "required": True, "default": 0.0},
        ],
    },
    "Batteries": {
        "bay_function": "create_battery_bay",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "min_p", "label": "min_p (MW)", "kind": "float", "required": True, "default": 0.0},
            {"name": "max_p", "label": "max_p (MW)", "kind": "float", "required": True, "default": 100.0},
            {"name": "target_p", "label": "target_p (MW)", "kind": "float", "required": True, "default": 0.0},
            {"name": "target_q", "label": "target_q (MVar)", "kind": "float", "required": True, "default": 0.0},
        ],
        "validate": "_validate_minmax_p",
    },
    "Static VAR Compensators": {
        "bay_function": "create_static_var_compensator_bay",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "b_min", "label": "b_min (S)", "kind": "float", "required": True, "default": -0.01},
            {"name": "b_max", "label": "b_max (S)", "kind": "float", "required": True, "default": 0.01},
            {"name": "regulation_mode", "label": "Regulation mode",
             "kind": "select", "required": True, "default": "VOLTAGE",
             "options": SVC_REGULATION_MODES},
            {"name": "regulating", "label": "Regulating", "kind": "bool",
             "required": True, "default": True},
            {"name": "target_v", "label": "target_v (kV)", "kind": "float",
             "required": False, "default": 0.0,
             "help": "Required when regulation mode is VOLTAGE."},
            {"name": "target_q", "label": "target_q (MVar)", "kind": "float",
             "required": False, "default": 0.0,
             "help": "Used when regulation mode is REACTIVE_POWER."},
        ],
        "validate": "_validate_svc",
    },
    "VSC Converter Stations": {
        "bay_function": "create_vsc_converter_station_bay",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "loss_factor", "label": "Loss factor (%)",
             "kind": "float", "required": True, "default": 0.0, "min_value": 0.0},
            {"name": "voltage_regulator_on", "label": "Voltage regulator on",
             "kind": "bool", "required": True, "default": False},
            {"name": "target_v", "label": "target_v (kV)", "kind": "float",
             "required": False, "default": 0.0,
             "help": "Required when voltage regulator is on."},
            {"name": "target_q", "label": "target_q (MVar)", "kind": "float",
             "required": False, "default": 0.0},
        ],
        "validate": "_validate_voltage_regulator",
    },
    "LCC Converter Stations": {
        "bay_function": "create_lcc_converter_station_bay",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "power_factor", "label": "Power factor",
             "kind": "float", "required": True, "default": 0.8},
            {"name": "loss_factor", "label": "Loss factor (%)",
             "kind": "float", "required": True, "default": 0.0, "min_value": 0.0},
        ],
    },
    "Shunt Compensators": {
        "bay_function": "create_shunt_compensator_bay",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "section_count", "label": "Initial section count",
             "kind": "int", "required": True, "default": 1, "min_value": 0},
            {"name": "max_section_count", "label": "Max section count",
             "kind": "int", "required": True, "default": 1, "min_value": 1,
             "help": "Maximum number of connectable sections (linear model)."},
            {"name": "g_per_section", "label": "g_per_section (S)",
             "kind": "float", "required": True, "default": 0.0},
            {"name": "b_per_section", "label": "b_per_section (S)",
             "kind": "float", "required": True, "default": 1e-5},
            {"name": "target_v", "label": "target_v (kV, 0 = unset)",
             "kind": "float", "required": False, "default": 0.0, "min_value": 0.0},
            {"name": "target_deadband", "label": "target_deadband (kV, 0 = unset)",
             "kind": "float", "required": False, "default": 0.0, "min_value": 0.0},
        ],
        "validate": "_validate_shunt",
    },
}

# Fields that go to the linear-model DataFrame (the rest become shunt_df).
_SHUNT_LINEAR_FIELDS: frozenset[str] = frozenset({
    "g_per_section", "b_per_section", "max_section_count",
})

# Shared locator fields appended to every creation form.
LOCATOR_FIELDS: list[dict] = [
    {"name": "position_order", "label": "Position order",
     "kind": "int", "required": True, "default": 10,
     "min_value": 0, "step": 10, "help": _POSITION_HELP},
    {"name": "direction", "label": "Direction", "kind": "select",
     "required": False, "default": "BOTTOM", "options": FEEDER_DIRECTIONS},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def coerce_field_values(
    fields_spec: list[dict],
    raw_values: dict,
) -> dict:
    """Strip text fields and cast int fields; pass float/bool/select through.

    Mirrors Streamlit's ``_coerce_field_values`` so each host's form
    renderer can feed its raw widget values straight in.
    """
    coerced: dict[str, Any] = {}
    for spec in fields_spec:
        v = raw_values.get(spec["name"])
        if spec["kind"] == "text":
            v = (v or "").strip()
        elif spec["kind"] == "int" and v is not None:
            v = int(v)
        coerced[spec["name"]] = v
    return coerced


def validate_create_fields(component: str, fields: dict) -> list[str]:
    """Run registry-driven validation; returns a list of human-readable errors.

    Checks required component fields + the shared locator fields + the
    ``bus_or_busbar_section_id`` context field (filled in by the form
    renderer from the busbar picker). Runs the component's ``validate``
    hook last.
    """
    spec = CREATABLE_COMPONENTS.get(component)
    if not spec:
        return [f"{component!r} is not creatable"]
    errors: list[str] = []
    for f in spec["fields"] + LOCATOR_FIELDS:
        if f["required"] and (
            fields.get(f["name"]) is None or fields.get(f["name"]) == ""
        ):
            errors.append(f"{f['label']} is required.")
    if not fields.get("bus_or_busbar_section_id"):
        errors.append("Busbar section is required.")
    hook = spec.get("validate")
    if hook and hook in _VALIDATORS:
        errors.extend(_VALIDATORS[hook](fields))
    return errors


# ---------------------------------------------------------------------------
# Network introspection — node-breaker VLs / busbar sections
# ---------------------------------------------------------------------------
def list_node_breaker_voltage_levels(network: NetworkProxy) -> pd.DataFrame:
    """Return node-breaker voltage levels as a DataFrame with
    ``id`` / ``display`` / ``substation_id`` / ``nominal_v`` columns.

    Bay creation only works in node-breaker topology — bus-breaker VLs
    don't have busbar sections to attach a feeder to.
    """
    vls = network.get_voltage_levels(all_attributes=True)
    if "topology_kind" not in vls.columns:
        return pd.DataFrame(columns=["id", "display", "substation_id", "nominal_v"])
    nb = vls[vls["topology_kind"] == "NODE_BREAKER"].reset_index()
    if nb.empty:
        return pd.DataFrame(columns=["id", "display", "substation_id", "nominal_v"])
    nb["display"] = nb.apply(lambda r: r["name"] if r["name"] else r["id"], axis=1)
    return nb[["id", "display", "substation_id", "nominal_v"]].sort_values("display")


def list_busbar_sections(network: NetworkProxy, voltage_level_id: str) -> list[str]:
    """Return a sorted list of busbar section ids in the given voltage level."""
    bbs = network.get_busbar_sections()
    if bbs.empty:
        return []
    return sorted(bbs[bbs["voltage_level_id"] == voltage_level_id].index.tolist())


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
def _dispatch_bay_create(
    network: NetworkProxy,
    bay_fn_name: str,
    fields: dict,
) -> None:
    """Build a one-row DataFrame and call pypowsybl's ``<bay_fn_name>``."""
    row = {k: v for k, v in fields.items() if v is not None and v != ""}
    df = pd.DataFrame([row]).set_index("id")
    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        import pypowsybl.network as pn
        getattr(pn, bay_fn_name)(raw, df)

    run(_do_create)


def _dispatch_shunt_bay(
    network: NetworkProxy,
    fields: dict,
) -> None:
    """Shunt compensator bay creation needs 3 dataframes (shunt + linear
    + non-linear). We only support the LINEAR model for now;
    non_linear_model_df stays empty.
    """
    linear_row = {k: fields[k] for k in _SHUNT_LINEAR_FIELDS if k in fields}
    linear_row["id"] = fields["id"]
    shunt_row = {
        k: v for k, v in fields.items()
        if k not in _SHUNT_LINEAR_FIELDS and v is not None and v != ""
    }
    shunt_row["model_type"] = "LINEAR"

    shunt_df = pd.DataFrame([shunt_row]).set_index("id")
    linear_df = pd.DataFrame([linear_row]).set_index("id")
    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        import pypowsybl.network as pn
        pn.create_shunt_compensator_bay(raw, shunt_df, linear_model_df=linear_df)

    run(_do_create)


def create_component_bay(
    network: NetworkProxy,
    component: str,
    fields: dict,
) -> None:
    """Create a new injection on a busbar section via a clean feeder bay.

    Routes through pypowsybl's ``create_*_bay`` helper which, in
    node-breaker voltage levels, allocates nodes and inserts a closed
    disconnector plus a breaker between the busbar section and the new
    injection. Callers supply the busbar id and the injection
    attributes; node numbering stays internal.

    Raises ``ValueError`` for an unknown component or when validation
    fails.
    """
    if component not in CREATABLE_COMPONENTS:
        raise ValueError(f"{component!r} is not creatable")

    errors = validate_create_fields(component, fields)
    if errors:
        raise ValueError("; ".join(errors))

    if component == "Shunt Compensators":
        _dispatch_shunt_bay(network, fields)
        return

    _dispatch_bay_create(
        network,
        CREATABLE_COMPONENTS[component]["bay_function"],
        fields,
    )


# ---------------------------------------------------------------------------
# Branches (Lines + 2-Winding Transformers)
# ---------------------------------------------------------------------------
# Locator fields applied to each side of a branch. Rendered twice
# (sides 1 + 2) by the form renderer; the helper below suffixes the
# field names with ``_<side>``.
_BRANCH_SIDE_LOCATOR: list[dict] = [
    {"name": "position_order", "label": "Position order",
     "kind": "int", "required": True, "default": 10,
     "min_value": 0, "step": 10, "help": _POSITION_HELP},
    {"name": "direction", "label": "Direction", "kind": "select",
     "required": False, "default": "BOTTOM", "options": FEEDER_DIRECTIONS},
]


CREATABLE_BRANCHES: dict[str, dict] = {
    "Lines": {
        "bay_function": "create_line_bays",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "r", "label": "r (Ω)", "kind": "float", "required": True, "default": 0.1},
            {"name": "x", "label": "x (Ω)", "kind": "float", "required": True, "default": 1.0},
            {"name": "g1", "label": "g1 (S)", "kind": "float", "required": True, "default": 0.0},
            {"name": "b1", "label": "b1 (S)", "kind": "float", "required": True, "default": 0.0},
            {"name": "g2", "label": "g2 (S)", "kind": "float", "required": True, "default": 0.0},
            {"name": "b2", "label": "b2 (S)", "kind": "float", "required": True, "default": 0.0},
        ],
        "same_substation": False,
    },
    "2-Winding Transformers": {
        "bay_function": "create_2_windings_transformer_bays",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "r", "label": "r (Ω)", "kind": "float", "required": True, "default": 0.5},
            {"name": "x", "label": "x (Ω)", "kind": "float", "required": True, "default": 10.0},
            {"name": "g", "label": "g (S)", "kind": "float", "required": True, "default": 0.0},
            {"name": "b", "label": "b (S)", "kind": "float", "required": True, "default": 0.0},
            {"name": "rated_u1", "label": "rated_u1 (kV)", "kind": "float",
             "required": True, "default": 400.0},
            {"name": "rated_u2", "label": "rated_u2 (kV)", "kind": "float",
             "required": True, "default": 225.0},
            {"name": "rated_s", "label": "rated_s (MVA, 0 = unset)",
             "kind": "float", "required": False, "default": 0.0, "min_value": 0.0},
        ],
        # pypowsybl only allows 2WTs between two VLs of the same substation.
        "same_substation": True,
    },
}


def branch_side_locator_fields(side: int) -> list[dict]:
    """Locator fields for one side of a branch, with names suffixed ``_<side>``.

    Used by the form renderer to build two identical locator grids
    (side 1 and side 2) and by :func:`validate_create_branch_fields`
    to assemble the required-field list.
    """
    return [
        {**f, "name": f"{f['name']}_{side}", "label": f"{f['label']} {side}"}
        for f in _BRANCH_SIDE_LOCATOR
    ]


def _substations_of_busbars(
    network: NetworkProxy, bbs1: str, bbs2: str,
) -> Optional[tuple[str, str]]:
    """Return ``(sub1, sub2)`` for the two busbar sections, or ``None``.

    Used by :func:`validate_create_branch_fields` to enforce 2-Winding
    Transformer's same-substation constraint.
    """
    if not bbs1 or not bbs2:
        return None
    bbs = network.get_busbar_sections()
    vls = network.get_voltage_levels()
    if bbs1 not in bbs.index or bbs2 not in bbs.index:
        return None
    vl1 = bbs.loc[bbs1, "voltage_level_id"]
    vl2 = bbs.loc[bbs2, "voltage_level_id"]
    if vl1 not in vls.index or vl2 not in vls.index:
        return None
    return vls.loc[vl1, "substation_id"], vls.loc[vl2, "substation_id"]


def validate_create_branch_fields(
    component: str,
    fields: dict,
    network: Optional[NetworkProxy] = None,
) -> list[str]:
    """Validate branch creation fields.

    Checks electrical fields + side-1/side-2 locator fields + busbar
    ids; if the spec has ``same_substation`` and a ``network`` is
    supplied, verifies both chosen busbar sections live in the same
    substation (the pypowsybl 2WT constraint).
    """
    spec = CREATABLE_BRANCHES.get(component)
    if not spec:
        return [f"{component!r} is not a creatable branch"]
    errors: list[str] = []
    all_required = (
        spec["fields"]
        + branch_side_locator_fields(1)
        + branch_side_locator_fields(2)
    )
    for f in all_required:
        if f["required"] and (
            fields.get(f["name"]) is None or fields.get(f["name"]) == ""
        ):
            errors.append(f"{f['label']} is required.")
    for side in (1, 2):
        key = f"bus_or_busbar_section_id_{side}"
        if not fields.get(key):
            errors.append(f"Busbar section {side} is required.")
    if spec.get("same_substation") and network is not None:
        sub = _substations_of_busbars(
            network,
            fields.get("bus_or_busbar_section_id_1"),
            fields.get("bus_or_busbar_section_id_2"),
        )
        if sub is not None and sub[0] != sub[1]:
            errors.append(
                f"{component} must connect voltage levels of the same substation "
                f"(side 1: {sub[0]!r}, side 2: {sub[1]!r})."
            )
    return errors


def create_branch_bay(
    network: NetworkProxy,
    component: str,
    fields: dict,
) -> None:
    """Create a Line or 2-Winding Transformer with feeder bays on each side.

    ``fields`` must include the electrical fields,
    ``bus_or_busbar_section_id_1`` / ``_2``, and
    ``position_order_1`` / ``_2``. Worker-thread bound.
    """
    if component not in CREATABLE_BRANCHES:
        raise ValueError(f"{component!r} is not a creatable branch")

    errors = validate_create_branch_fields(component, fields, network=network)
    if errors:
        raise ValueError("; ".join(errors))

    _dispatch_bay_create(
        network,
        CREATABLE_BRANCHES[component]["bay_function"],
        fields,
    )
