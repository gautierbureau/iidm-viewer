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
    # ``rated_s`` carries a documented "0 = unset" sentinel in every
    # schema that exposes it (Generators, 2-Winding Transformers). The
    # form label says so explicitly; the user can leave it at the
    # default ``0.0`` to mean "no rated apparent power". pypowsybl
    # rejects ``rated_s = 0.0`` with "Invalid value 0.0 for rated_s",
    # so honor the sentinel here — drop the field before dispatch so
    # pypowsybl sees "column absent" → unset.
    fields = {
        k: v for k, v in fields.items()
        if not (k == "rated_s" and v == 0.0)
    }
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


# ---------------------------------------------------------------------------
# Containers (Substations, Voltage Levels, Busbar Sections)
# ---------------------------------------------------------------------------
TOPOLOGY_KINDS = ["NODE_BREAKER", "BUS_BREAKER"]


CREATABLE_CONTAINERS: dict[str, dict] = {
    "Substations": {
        "create_function": "create_substations",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "name", "label": "Name", "kind": "text", "required": False, "default": ""},
            {"name": "country", "label": "Country (ISO code)", "kind": "text",
             "required": False, "default": "",
             "help": "Two-letter ISO country code (e.g. FR, DE, IT). Leave blank if unknown."},
            {"name": "TSO", "label": "TSO", "kind": "text", "required": False, "default": ""},
        ],
    },
    "Voltage Levels": {
        "create_function": "create_voltage_levels",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "name", "label": "Name", "kind": "text", "required": False, "default": ""},
            {"name": "topology_kind", "label": "Topology kind", "kind": "select",
             "required": True, "default": "NODE_BREAKER", "options": TOPOLOGY_KINDS},
            {"name": "nominal_v", "label": "nominal_v (kV)", "kind": "float",
             "required": True, "default": 400.0, "min_value": 0.0},
            {"name": "low_voltage_limit", "label": "low_voltage_limit (kV, 0 = unset)",
             "kind": "float", "required": False, "default": 0.0, "min_value": 0.0},
            {"name": "high_voltage_limit", "label": "high_voltage_limit (kV, 0 = unset)",
             "kind": "float", "required": False, "default": 0.0, "min_value": 0.0},
        ],
        "validate": "_validate_voltage_level",
    },
    "Busbar Sections": {
        "create_function": "create_busbar_sections",
        "fields": [
            {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
            {"name": "name", "label": "Name", "kind": "text", "required": False, "default": ""},
            {"name": "node", "label": "Node", "kind": "int", "required": True,
             "default": 0, "min_value": 0,
             "help": "Free node index in the target voltage level. "
                     "Defaults to the next unused node."},
        ],
    },
}


def _validate_voltage_level(fields: dict) -> list[str]:
    """high_voltage_limit must be >= low_voltage_limit when both are set."""
    errors: list[str] = []
    low = fields.get("low_voltage_limit")
    high = fields.get("high_voltage_limit")
    if low and high and low > 0 and high > 0 and high < low:
        errors.append("high_voltage_limit must be >= low_voltage_limit.")
    return errors


# Register the container-specific validator alongside the injection ones.
# Streamlit relied on this side-effect (it kept its own _VALIDATORS dict
# and mutated it at module scope); keeping the registration here means
# both prototypes see the same validator without duplicating it.
_VALIDATORS["_validate_voltage_level"] = _validate_voltage_level


def validate_create_container_fields(component: str, fields: dict) -> list[str]:
    """Check required fields + component-specific rules for a container."""
    spec = CREATABLE_CONTAINERS.get(component)
    if not spec:
        return [f"{component!r} is not a creatable container"]
    errors: list[str] = []
    for f in spec["fields"]:
        if f["required"] and (
            fields.get(f["name"]) is None or fields.get(f["name"]) == ""
        ):
            errors.append(f"{f['label']} is required.")
    # Busbar Sections always need a target voltage level on top of the
    # form fields (it's the "where to attach to" context, not part of
    # the create_busbar_sections DataFrame columns).
    if component == "Busbar Sections" and not fields.get("voltage_level_id"):
        errors.append("Voltage level is required.")
    hook = spec.get("validate")
    if hook and hook in _VALIDATORS:
        errors.extend(_VALIDATORS[hook](fields))
    return errors


def list_substations_df(network: NetworkProxy):
    """Return substations as a DataFrame with ``id`` / ``display`` columns.

    Used by the Voltage Level creation form's substation picker.
    """
    subs = network.get_substations(attributes=["name"]).reset_index()
    if subs.empty:
        return pd.DataFrame(columns=["id", "display"])
    subs["display"] = subs.apply(
        lambda r: r["name"] if r["name"] else r["id"], axis=1
    )
    return subs[["id", "display"]].sort_values("display")


def next_free_node(network: NetworkProxy, voltage_level_id: str) -> int:
    """Suggest the next unused node index in a node-breaker voltage level.

    Scans busbar section ``node`` and switch ``node1`` / ``node2`` columns
    for the VL. Returns ``max(used) + 1``, or 0 when the VL has no
    elements yet.
    """
    used: set[int] = set()
    try:
        bbs = network.get_busbar_sections(all_attributes=True)
    except Exception:
        bbs = pd.DataFrame()
    if not bbs.empty and "node" in bbs.columns and "voltage_level_id" in bbs.columns:
        vl_bbs = bbs[bbs["voltage_level_id"] == voltage_level_id]
        used.update(int(n) for n in vl_bbs["node"].dropna().tolist())
    try:
        sw = network.get_switches(all_attributes=True)
    except Exception:
        sw = pd.DataFrame()
    if not sw.empty and "voltage_level_id" in sw.columns:
        vl_sw = sw[sw["voltage_level_id"] == voltage_level_id]
        for col in ("node1", "node2"):
            if col in vl_sw.columns:
                used.update(int(n) for n in vl_sw[col].dropna().tolist())
    return max(used) + 1 if used else 0


def create_container(
    network: NetworkProxy,
    component: str,
    fields: dict,
) -> None:
    """Create a substation, voltage level, or busbar section.

    Unlike injections / branches these don't use a ``_bay`` helper —
    they call the plain ``create_<type>s`` method on the raw network
    object. Drops empty strings and "unset" sentinels (zero
    voltage-limits) so pypowsybl treats them as missing rather than
    literal values.
    """
    if component not in CREATABLE_CONTAINERS:
        raise ValueError(f"{component!r} is not a creatable container")

    errors = validate_create_container_fields(component, fields)
    if errors:
        raise ValueError("; ".join(errors))

    spec = CREATABLE_CONTAINERS[component]
    clean = {k: v for k, v in fields.items() if v is not None and v != ""}
    if component == "Voltage Levels":
        for key in ("low_voltage_limit", "high_voltage_limit"):
            if clean.get(key) == 0.0:
                clean.pop(key, None)

    df = pd.DataFrame([clean]).set_index("id")
    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        getattr(raw, spec["create_function"])(df)

    run(_do_create)


# ---------------------------------------------------------------------------
# HVDC lines (between two existing converter stations)
# ---------------------------------------------------------------------------
CONVERTERS_MODES = [
    "SIDE_1_RECTIFIER_SIDE_2_INVERTER",
    "SIDE_1_INVERTER_SIDE_2_RECTIFIER",
]


# HVDC lines are created directly via ``raw.create_hvdc_lines`` — no _bay
# helper. The two endpoints are *existing* VSC / LCC converter stations.
CREATABLE_HVDC_LINES: dict = {
    "create_function": "create_hvdc_lines",
    "fields": [
        {"name": "id", "label": "ID", "kind": "text", "required": True, "default": ""},
        {"name": "name", "label": "Name", "kind": "text", "required": False, "default": ""},
        {"name": "r", "label": "r (Ω)", "kind": "float", "required": True, "default": 1.0},
        {"name": "nominal_v", "label": "nominal_v (kV)", "kind": "float",
         "required": True, "default": 400.0},
        {"name": "max_p", "label": "max_p (MW)", "kind": "float",
         "required": True, "default": 1000.0, "min_value": 0.0},
        {"name": "target_p", "label": "target_p (MW)", "kind": "float",
         "required": True, "default": 0.0},
        {"name": "converters_mode", "label": "Converters mode", "kind": "select",
         "required": True, "default": CONVERTERS_MODES[0],
         "options": CONVERTERS_MODES},
    ],
}


def list_converter_stations(network: NetworkProxy) -> list[tuple[str, str]]:
    """Return ``[(id, kind)]`` for every VSC + LCC converter station in the network.

    Used by the HVDC creation form to populate the two endpoint pickers.
    """
    stations: list[tuple[str, str]] = []
    try:
        for sid in network.get_vsc_converter_stations().index.tolist():
            stations.append((sid, "VSC"))
    except Exception:
        pass
    try:
        for sid in network.get_lcc_converter_stations().index.tolist():
            stations.append((sid, "LCC"))
    except Exception:
        pass
    return sorted(stations)


def validate_create_hvdc_line_fields(fields: dict) -> list[str]:
    """Required fields + distinct endpoints + ``|target_p| <= max_p``.

    Anything pypowsybl-specific (e.g. a station already attached to
    another HVDC line) surfaces at the create call rather than here.
    """
    errors: list[str] = []
    for f in CREATABLE_HVDC_LINES["fields"]:
        if f["required"] and (
            fields.get(f["name"]) is None or fields.get(f["name"]) == ""
        ):
            errors.append(f"{f['label']} is required.")
    if not fields.get("converter_station1_id"):
        errors.append("Converter station 1 is required.")
    if not fields.get("converter_station2_id"):
        errors.append("Converter station 2 is required.")
    if (
        fields.get("converter_station1_id")
        and fields.get("converter_station2_id")
        and fields["converter_station1_id"] == fields["converter_station2_id"]
    ):
        errors.append("The two converter stations must differ.")
    if (
        fields.get("target_p") is not None
        and fields.get("max_p") is not None
        and abs(fields["target_p"]) > fields["max_p"]
    ):
        errors.append("|target_p| must be <= max_p.")
    return errors


def create_hvdc_line(network: NetworkProxy, fields: dict) -> None:
    """Create an HVDC line between two existing converter stations.

    Validates the endpoints + electrical attributes; then dispatches
    ``raw.create_hvdc_lines`` on the worker thread. The two stations
    must already exist and must not already be wired to another HVDC
    line — pypowsybl raises if either condition fails.
    """
    errors = validate_create_hvdc_line_fields(fields)
    if errors:
        raise ValueError("; ".join(errors))

    row = {k: v for k, v in fields.items() if v is not None and v != ""}
    df = pd.DataFrame([row]).set_index("id")
    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        raw.create_hvdc_lines(df)

    run(_do_create)


# ---------------------------------------------------------------------------
# Tap changers (ratio + phase) on existing 2-winding transformers
# ---------------------------------------------------------------------------
PTC_REGULATION_MODES = ["CURRENT_LIMITER", "ACTIVE_POWER_CONTROL"]
TRANSFORMER_SIDES = ["ONE", "TWO"]


# Ratio/Phase tap changer creation spec. Unlike injections, tap changers are
# attached to an *existing* transformer — no bay allocation. They need 2
# dataframes: one for the tap changer attributes, one for the per-tap steps.
CREATABLE_TAP_CHANGERS: dict[str, dict] = {
    "Ratio": {
        "create_method": "create_ratio_tap_changers",
        "main_fields": [
            {"name": "tap", "label": "Current tap",
             "kind": "int", "required": True, "default": 0, "min_value": 0},
            {"name": "low_tap", "label": "Lowest tap number",
             "kind": "int", "required": True, "default": 0, "min_value": 0},
            {"name": "oltc", "label": "On-load tap changing (OLTC)",
             "kind": "bool", "required": True, "default": False,
             "help": "Must be true to enable voltage regulation."},
            {"name": "regulating", "label": "Regulating", "kind": "bool",
             "required": True, "default": False},
            {"name": "target_v", "label": "target_v (kV, 0 = unset)",
             "kind": "float", "required": False, "default": 0.0, "min_value": 0.0},
            {"name": "target_deadband", "label": "target_deadband (kV, 0 = unset)",
             "kind": "float", "required": False, "default": 0.0, "min_value": 0.0},
            {"name": "regulated_side", "label": "Regulated side", "kind": "select",
             "required": False, "default": "ONE", "options": TRANSFORMER_SIDES},
        ],
        "step_columns": ["r", "x", "g", "b", "rho"],
        "step_defaults": {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.0},
    },
    "Phase": {
        "create_method": "create_phase_tap_changers",
        "main_fields": [
            {"name": "tap", "label": "Current tap",
             "kind": "int", "required": True, "default": 0, "min_value": 0},
            {"name": "low_tap", "label": "Lowest tap number",
             "kind": "int", "required": True, "default": 0, "min_value": 0},
            {"name": "regulation_mode", "label": "Regulation mode",
             "kind": "select", "required": True, "default": "CURRENT_LIMITER",
             "options": PTC_REGULATION_MODES},
            {"name": "regulating", "label": "Regulating", "kind": "bool",
             "required": True, "default": False},
            {"name": "target_deadband", "label": "target_deadband (0 = unset)",
             "kind": "float", "required": False, "default": 0.0, "min_value": 0.0},
            {"name": "regulated_side", "label": "Regulated side", "kind": "select",
             "required": False, "default": "ONE", "options": TRANSFORMER_SIDES},
        ],
        "step_columns": ["r", "x", "g", "b", "rho", "alpha"],
        "step_defaults": {
            "r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.0, "alpha": 0.0,
        },
    },
}


def list_two_winding_transformers(network: NetworkProxy) -> list[str]:
    """Return 2WT ids sorted alphabetically (empty list if none)."""
    try:
        twts = network.get_2_windings_transformers(attributes=["name"])
    except Exception:
        return []
    return sorted(twts.index.tolist())


def list_transformers_without_tap_changer(
    network: NetworkProxy, kind: str
) -> list[str]:
    """2WT ids that don't already have a ``kind`` ('Ratio' or 'Phase') tap changer.

    Used by the UI to pre-filter the target picker — creating a second tap
    changer of the same kind on a transformer raises in pypowsybl.
    """
    twts = list_two_winding_transformers(network)
    if not twts:
        return []
    try:
        if kind == "Ratio":
            existing = set(network.get_ratio_tap_changers().index.tolist())
        else:
            existing = set(network.get_phase_tap_changers().index.tolist())
    except Exception:
        existing = set()
    return [tid for tid in twts if tid not in existing]


def validate_create_tap_changer_fields(
    kind: str, transformer_id: str, main_fields: dict, steps: list[dict]
) -> list[str]:
    """Validate a tap-changer creation payload.

    Checks the transformer id, main fields (required + cross-field rules)
    and that at least one step is provided and spans the current ``tap``
    position. Returns a list of human-readable errors.
    """
    spec = CREATABLE_TAP_CHANGERS.get(kind)
    if not spec:
        return [f"{kind!r} tap changer is not creatable"]
    errors: list[str] = []
    if not transformer_id:
        errors.append("Target 2-winding transformer is required.")
    for f in spec["main_fields"]:
        v = main_fields.get(f["name"])
        if f["required"] and v is None:
            errors.append(f"{f['label']} is required.")
    if not steps:
        errors.append("At least one tap step is required.")
    if steps:
        low = main_fields.get("low_tap", 0) or 0
        tap = main_fields.get("tap", 0) or 0
        if tap < low or tap >= low + len(steps):
            errors.append(
                f"Current tap {tap} must be between {low} and "
                f"{low + len(steps) - 1} (inclusive)."
            )
    if main_fields.get("regulating"):
        if kind == "Ratio":
            if not main_fields.get("oltc"):
                errors.append(
                    "OLTC must be enabled to set regulating=True on a ratio "
                    "tap changer."
                )
            if not main_fields.get("target_v") or main_fields["target_v"] <= 0:
                errors.append(
                    "target_v must be > 0 when the ratio tap changer is regulating."
                )
    return errors


def create_tap_changer(
    network: NetworkProxy,
    kind: str,
    transformer_id: str,
    main_fields: dict,
    steps: list[dict],
) -> None:
    """Create a ratio or phase tap changer on an existing 2-winding transformer.

    Runs validation on the main thread then dispatches the two dataframes
    (tap-changer attributes + per-step data) to pypowsybl via the worker.
    ``steps`` is a list of dicts matching ``CREATABLE_TAP_CHANGERS[kind]``'s
    ``step_columns``; one row is emitted per entry.
    """
    if kind not in CREATABLE_TAP_CHANGERS:
        raise ValueError(f"{kind!r} tap changer is not creatable")

    errors = validate_create_tap_changer_fields(
        kind, transformer_id, main_fields, steps,
    )
    if errors:
        raise ValueError("; ".join(errors))

    spec = CREATABLE_TAP_CHANGERS[kind]
    # Drop zero-sentinel target_v / target_deadband so pypowsybl sees them as unset.
    main_row = {
        k: v for k, v in main_fields.items()
        if v is not None and v != "" and not (
            k in ("target_v", "target_deadband") and v == 0.0
        )
    }
    main_row["id"] = transformer_id
    main_df = pd.DataFrame([main_row]).set_index("id")

    step_rows = []
    for step in steps:
        row = {"id": transformer_id}
        for col in spec["step_columns"]:
            row[col] = step.get(col, spec["step_defaults"][col])
        step_rows.append(row)
    steps_df = pd.DataFrame(step_rows).set_index("id")

    raw = object.__getattribute__(network, "_obj")
    method_name = spec["create_method"]

    def _do_create():
        getattr(raw, method_name)(main_df, steps_df)

    run(_do_create)


# ---------------------------------------------------------------------------
# Coupling devices (switches tying two busbar sections in the same VL)
# ---------------------------------------------------------------------------
def list_node_breaker_vls_with_multi_bbs(
    network: NetworkProxy,
) -> list[tuple[str, str, float]]:
    """Return ``[(vl_id, display, nominal_v)]`` for every node-breaker VL
    that carries at least two busbar sections.

    A coupling device needs two distinct busbars to tie together, so VLs
    with 0 or 1 BBS aren't candidates. Used by the UI to gate the picker.
    """
    nb_vls = list_node_breaker_voltage_levels(network)
    if nb_vls.empty:
        return []
    bbs = network.get_busbar_sections()
    if bbs.empty:
        return []
    counts = bbs.groupby("voltage_level_id").size()
    out: list[tuple[str, str, float]] = []
    for _, row in nb_vls.iterrows():
        if counts.get(row["id"], 0) >= 2:
            out.append((row["id"], row["display"], float(row["nominal_v"])))
    return out


def validate_create_coupling_device_fields(
    network: NetworkProxy, bbs1: str, bbs2: str,
) -> list[str]:
    """Sanity-check a coupling-device payload before dispatching it.

    Verifies both busbar section ids are non-empty, distinct, known to
    the network, and sit in the same voltage level. Returns a list of
    human-readable errors (empty when valid).
    """
    errors: list[str] = []
    if not bbs1 or not bbs2:
        errors.append("Both busbar sections are required.")
        return errors
    if bbs1 == bbs2:
        errors.append("The two busbar sections must differ.")
    bbs = network.get_busbar_sections()
    if bbs1 not in bbs.index or bbs2 not in bbs.index:
        errors.append("Unknown busbar section id.")
        return errors
    vl1 = bbs.loc[bbs1, "voltage_level_id"]
    vl2 = bbs.loc[bbs2, "voltage_level_id"]
    if vl1 != vl2:
        errors.append(
            "A coupling device must tie busbar sections of the same voltage "
            f"level (got {vl1!r} and {vl2!r})."
        )
    return errors


def create_coupling_device(
    network: NetworkProxy,
    bbs1: str,
    bbs2: str,
    switch_prefix: str | None = None,
) -> None:
    """Create a coupling device between two busbar sections in the same VL.

    In node-breaker topology pypowsybl inserts a closed breaker plus closed
    disconnectors on both busbar sections, and open disconnectors on any
    parallel busbar sections. In bus-breaker topology only a breaker is
    added. Routed through the worker thread like every other pypowsybl call.
    """
    errors = validate_create_coupling_device_fields(network, bbs1, bbs2)
    if errors:
        raise ValueError("; ".join(errors))

    kwargs = {
        "bus_or_busbar_section_id_1": bbs1,
        "bus_or_busbar_section_id_2": bbs2,
    }
    if switch_prefix:
        kwargs["switch_prefix_id"] = switch_prefix

    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        import pypowsybl.network as pn
        pn.create_coupling_device(raw, **kwargs)

    run(_do_create)


# ---------------------------------------------------------------------------
# Secondary voltage control (network-level extension: zones + control units)
# ---------------------------------------------------------------------------
# SVC is defined on the whole network as a list of control zones plus a list
# of control units. pypowsybl takes two DataFrames:
#
#   zones  (index: name)       — target_v (kV), bus_ids (pilot points,
#                                space-separated if several)
#   units  (index: unit_id)    — zone_name, participate (bool)
#
# ``network.create_extensions('secondaryVoltageControl', [zones, units])``
# *replaces* the whole SVC definition on write (no append). pypowsybl 1.14
# has no view adapter for reading it back via ``get_extensions`` — the data
# persists in the XIIDM export only.

def list_bus_ids(network: NetworkProxy) -> list[str]:
    """Return all bus ids in the bus view. Used to populate pilot-point pickers."""
    try:
        return sorted(network.get_buses().index.tolist())
    except Exception:
        return []


def list_unit_candidates(network: NetworkProxy) -> list[str]:
    """Candidate control units: generators, batteries, and SVCs."""
    out: list[str] = []
    for getter in (
        "get_generators", "get_batteries", "get_static_var_compensators",
    ):
        try:
            out.extend(getattr(network, getter)().index.tolist())
        except Exception:
            pass
    return sorted(out)


def validate_secondary_voltage_control(
    zones: list[dict], units: list[dict],
) -> list[str]:
    """Validate a (zones, units) payload before dispatching to the extension API.

    Branches: at least one zone; per-zone name presence + uniqueness +
    ``target_v > 0`` + at least one pilot bus; per-unit id presence +
    uniqueness + a known zone reference.
    """
    errors: list[str] = []
    if not zones:
        errors.append("At least one zone is required.")
        return errors
    names: list[str] = []
    for zi, z in enumerate(zones):
        name = (z.get("name") or "").strip()
        if not name:
            errors.append(f"Zone #{zi + 1}: name is required.")
            continue
        if name in names:
            errors.append(f"Zone name {name!r} is duplicated.")
        names.append(name)
        if z.get("target_v") in (None, ""):
            errors.append(f"Zone {name!r}: target_v is required.")
        else:
            try:
                if float(z["target_v"]) <= 0:
                    errors.append(f"Zone {name!r}: target_v must be > 0.")
            except (TypeError, ValueError):
                errors.append(f"Zone {name!r}: target_v must be numeric.")
        bus_ids = (z.get("bus_ids") or "").strip()
        if not bus_ids:
            errors.append(f"Zone {name!r}: at least one pilot bus id is required.")

    unit_ids: list[str] = []
    for ui_idx, u in enumerate(units):
        uid = (u.get("unit_id") or "").strip()
        if not uid:
            errors.append(f"Unit #{ui_idx + 1}: unit_id is required.")
            continue
        if uid in unit_ids:
            errors.append(f"Unit id {uid!r} is duplicated.")
        unit_ids.append(uid)
        zn = (u.get("zone_name") or "").strip()
        if not zn:
            errors.append(f"Unit {uid!r}: zone_name is required.")
        elif zn not in names:
            errors.append(
                f"Unit {uid!r}: zone_name {zn!r} is not one of the defined zones."
            )
    return errors


def create_secondary_voltage_control(
    network: NetworkProxy, zones: list[dict], units: list[dict],
) -> None:
    """Replace the secondaryVoltageControl extension with the given zones + units.

    ``zones`` entries: ``{name, target_v, bus_ids}`` (``bus_ids`` is a single
    bus id or a space-separated list of pilot-point bus ids).
    ``units`` entries: ``{unit_id, zone_name, participate}``.
    """
    errors = validate_secondary_voltage_control(zones, units)
    if errors:
        raise ValueError("; ".join(errors))

    zones_df = pd.DataFrame(
        {
            "target_v": [float(z["target_v"]) for z in zones],
            "bus_ids": [(z.get("bus_ids") or "").strip() for z in zones],
        },
        index=pd.Index([z["name"].strip() for z in zones], name="name"),
    )
    units_df = pd.DataFrame(
        {
            "zone_name": [u["zone_name"].strip() for u in units],
            "participate": [bool(u.get("participate", True)) for u in units],
        },
        index=pd.Index([u["unit_id"].strip() for u in units], name="unit_id"),
    )

    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        raw.create_extensions("secondaryVoltageControl", [zones_df, units_df])

    run(_do_create)


# ---------------------------------------------------------------------------
# Operational limits (CURRENT / APPARENT_POWER / ACTIVE_POWER limit groups)
# ---------------------------------------------------------------------------
OPERATIONAL_LIMIT_TYPES = ["CURRENT", "APPARENT_POWER", "ACTIVE_POWER"]
OPERATIONAL_LIMIT_SIDES = ["ONE", "TWO"]

OPERATIONAL_LIMITS_TARGETS = {
    "Lines": "get_lines",
    "2-Winding Transformers": "get_2_windings_transformers",
    "Dangling Lines": "get_dangling_lines",
}

# Permanent limit's acceptable_duration is -1; data-editor-friendly sentinel.
PERMANENT_DURATION = -1


def list_operational_limit_candidates(
    network: NetworkProxy, component: str,
) -> list[str]:
    """Return ids of elements that can carry operational limits for ``component``."""
    getter = OPERATIONAL_LIMITS_TARGETS.get(component)
    if not getter:
        return []
    try:
        df = getattr(network, getter)()
    except Exception:
        return []
    return sorted(df.index.tolist())


def validate_create_operational_limits_fields(
    element_id: str,
    side: str,
    limit_type: str,
    limits: list[dict],
) -> list[str]:
    """Validate a limit-group payload before dispatching it.

    Checks the element id, side / type enums, that ``limits`` is non-empty
    and contains exactly one permanent row (``acceptable_duration = -1``),
    and that every row carries a non-negative numeric value plus a valid
    duration (``-1`` or non-negative integer).
    """
    errors: list[str] = []
    if not element_id:
        errors.append("Target element id is required.")
    if side not in OPERATIONAL_LIMIT_SIDES:
        errors.append(f"Side must be one of {OPERATIONAL_LIMIT_SIDES}.")
    if limit_type not in OPERATIONAL_LIMIT_TYPES:
        errors.append(f"Type must be one of {OPERATIONAL_LIMIT_TYPES}.")
    if not limits:
        errors.append("At least one limit row is required.")
        return errors

    permanent = 0
    for lim in limits:
        value = lim.get("value")
        if value is None:
            errors.append("Every limit needs a value.")
            return errors
        if value < 0:
            errors.append("Limit values must be non-negative.")
            return errors
        duration = lim.get("acceptable_duration")
        if duration is None:
            errors.append(
                "Every limit needs an acceptable_duration (-1 for permanent)."
            )
            return errors
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            errors.append("acceptable_duration must be an integer.")
            return errors
        if duration == -1:
            permanent += 1
        elif duration < 0:
            errors.append("acceptable_duration must be -1 (permanent) or >= 0.")
            return errors
    if permanent != 1:
        errors.append(
            "Exactly one permanent limit (acceptable_duration = -1) is required."
        )
    return errors


def create_operational_limits(
    network: NetworkProxy,
    element_id: str,
    side: str,
    limit_type: str,
    limits: list[dict],
    group_name: str = "DEFAULT",
) -> None:
    """Create a group of operational limits on one side of an element.

    ``limits`` is a list of dicts with ``name``, ``value``, and
    ``acceptable_duration`` (use ``-1`` for the permanent limit). Exactly
    one permanent limit is allowed per (element, side, group). pypowsybl
    replaces any existing limits in the target group.
    """
    errors = validate_create_operational_limits_fields(
        element_id, side, limit_type, limits,
    )
    if errors:
        raise ValueError("; ".join(errors))

    rows = []
    for lim in limits:
        duration = int(lim["acceptable_duration"])
        name = lim.get("name") or (
            "permanent" if duration == -1 else f"TATL_{duration}"
        )
        rows.append({
            "element_id": element_id,
            "side": side,
            "name": name,
            "type": limit_type,
            "value": float(lim["value"]),
            "acceptable_duration": duration,
            "fictitious": bool(lim.get("fictitious", False)),
            "group_name": group_name or "DEFAULT",
        })

    df = pd.DataFrame(rows).set_index("element_id")
    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        raw.create_operational_limits(df)

    run(_do_create)


# ---------------------------------------------------------------------------
# Reactive limits (min/max or per-P capability curve)
# ---------------------------------------------------------------------------
REACTIVE_LIMITS_TARGETS = {
    "Generators": "get_generators",
    "Batteries": "get_batteries",
    "VSC Converter Stations": "get_vsc_converter_stations",
}

REACTIVE_LIMITS_MODES = ["minmax", "curve"]


def list_reactive_limit_candidates(
    network: NetworkProxy, component: str,
) -> list[str]:
    """Return ids of elements that can carry reactive limits for ``component``."""
    getter = REACTIVE_LIMITS_TARGETS.get(component)
    if not getter:
        return []
    try:
        df = getattr(network, getter)()
    except Exception:
        return []
    return sorted(df.index.tolist())


def validate_create_reactive_limits_fields(
    mode: str, element_id: str, payload: list[dict],
) -> list[str]:
    """Validate a reactive-limits payload before dispatching it.

    ``mode`` must be ``"minmax"`` (one row with ``min_q``/``max_q``) or
    ``"curve"`` (≥2 distinct ``p`` rows). Returns human-readable errors.
    """
    errors: list[str] = []
    if mode not in REACTIVE_LIMITS_MODES:
        errors.append(f"Unknown reactive-limits mode: {mode!r}")
        return errors
    if not element_id:
        errors.append("Target element id is required.")
    if not payload:
        errors.append("At least one row is required.")
        return errors

    if mode == "minmax":
        row = payload[0]
        if row.get("min_q") is None or row.get("max_q") is None:
            errors.append("min_q and max_q are required.")
        elif row["max_q"] < row["min_q"]:
            errors.append("max_q must be >= min_q.")
        return errors

    # curve
    for row in payload:
        for k in ("p", "min_q", "max_q"):
            if row.get(k) is None:
                errors.append(f"Curve rows need non-null {k}.")
                return errors
        if row["max_q"] < row["min_q"]:
            errors.append("max_q must be >= min_q at every active power point.")
            return errors
    if len({row["p"] for row in payload}) < 2:
        errors.append(
            "A reactive capability curve needs at least 2 distinct p points."
        )
    return errors


def create_reactive_limits(
    network: NetworkProxy, element_id: str, mode: str, payload: list[dict],
) -> None:
    """Attach reactive limits (min/max or per-P curve) to an existing element.

    Validates on the main thread; then dispatches the appropriate
    pypowsybl call on the worker. pypowsybl replaces any existing reactive
    limits on the target.
    """
    errors = validate_create_reactive_limits_fields(mode, element_id, payload)
    if errors:
        raise ValueError("; ".join(errors))

    raw = object.__getattribute__(network, "_obj")
    if mode == "minmax":
        row = payload[0]
        df = pd.DataFrame(
            [{"id": element_id, "min_q": row["min_q"], "max_q": row["max_q"]}]
        ).set_index("id")

        def _do_create():
            raw.create_minmax_reactive_limits(df)
    else:
        rows = [
            {"id": element_id, "p": row["p"],
             "min_q": row["min_q"], "max_q": row["max_q"]}
            for row in payload
        ]
        df = pd.DataFrame(rows).set_index("id")

        def _do_create():
            raw.create_curve_reactive_limits(df)

    run(_do_create)
