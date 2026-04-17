import pandas as pd
import streamlit as st

from iidm_viewer.powsybl_worker import NetworkProxy, run


def init_state():
    defaults = {
        "network": None,
        "selected_vl": None,
        "nad_depth": 1,
        "component_type": "Voltage Levels",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def load_network(uploaded_file):
    from io import BytesIO
    if uploaded_file.name.lower().endswith(".zip"):
        buf = BytesIO(uploaded_file.getbuffer())
    else:
        import zipfile
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(uploaded_file.name, uploaded_file.getvalue())
        buf.seek(0)

    def _load():
        import pypowsybl.network as pn
        return pn.load_from_binary_buffer(buf)

    network = NetworkProxy(run(_load))
    st.session_state.network = network
    st.session_state.selected_vl = None
    st.session_state.pop("_map_data_cache", None)
    return network


def create_empty_network(network_id: str = "network"):
    """Create a blank network and install it as the session network.

    Lets users bootstrap a model from scratch without uploading anything —
    they can then build it up via the Data Explorer's "Create a new …"
    forms. Like :func:`load_network`, the resulting object is a
    :class:`NetworkProxy` so every subsequent pypowsybl call runs on the
    worker thread.
    """
    nid = (network_id or "network").strip() or "network"

    def _create():
        import pypowsybl.network as pn
        return pn.create_empty(network_id=nid)

    network = NetworkProxy(run(_create))
    st.session_state.network = network
    st.session_state.selected_vl = None
    st.session_state.pop("_map_data_cache", None)
    st.session_state.pop("_vl_lookup_cache", None)
    st.session_state.pop("_last_file", None)
    return network


def get_network():
    return st.session_state.get("network")


def run_loadflow(network):
    raw = object.__getattribute__(network, "_obj")

    # Read parameters from session state on the main thread
    from iidm_viewer.lf_parameters import get_lf_parameters
    generic, provider = get_lf_parameters()

    def _run_ac():
        import pypowsybl.loadflow as lf
        params = lf.Parameters(**generic)
        if provider:
            params.provider_parameters = {k: str(v) for k, v in provider.items()}
        return lf.run_ac(raw, parameters=params)

    results = run(_run_ac)
    # Invalidate cached lookups so tabs reload fresh data
    st.session_state.pop("_vl_lookup_cache", None)
    return results


# Component label -> (update method, [editable attributes])
EDITABLE_COMPONENTS: dict[str, tuple[str, list[str]]] = {
    "Loads": ("update_loads", ["p0", "q0", "connected"]),
    "Generators": (
        "update_generators",
        ["target_p", "target_v", "target_q", "voltage_regulator_on", "connected"],
    ),
    "Batteries": ("update_batteries", ["target_p", "target_q", "connected"]),
    "Switches": ("update_switches", ["open"]),
    "Shunt Compensators": ("update_shunt_compensators", ["section_count", "connected"]),
    "Static VAR Compensators": (
        "update_static_var_compensators",
        ["regulation_mode", "voltage_setpoint", "reactive_power_setpoint", "connected"],
    ),
    "VSC Converter Stations": (
        "update_vsc_converter_stations",
        ["target_v", "target_q", "voltage_regulator_on", "connected"],
    ),
    "LCC Converter Stations": (
        "update_lcc_converter_stations",
        ["power_factor", "connected"],
    ),
    "HVDC Lines": ("update_hvdc_lines", ["active_power_setpoint", "converters_mode"]),
    "Dangling Lines": ("update_dangling_lines", ["p0", "q0", "connected"]),
    "Lines": ("update_lines", ["connected1", "connected2"]),
    "2-Winding Transformers": ("update_2_windings_transformers", ["connected1", "connected2"]),
}


def update_components(network, component: str, changes_df):
    """Apply a DataFrame of changes via the appropriate update_ method.

    *changes_df* is indexed by element id and may contain NaN for cells
    that didn't change.  pypowsybl rejects NaN values, so we group rows
    by their non-null column set and issue one update call per group.
    """
    if changes_df.empty:
        return
    update_method_name, _ = EDITABLE_COMPONENTS[component]
    raw = object.__getattribute__(network, "_obj")

    # Group rows by which columns are non-null
    groups: dict[tuple[str, ...], list[str]] = {}
    for idx in changes_df.index:
        row = changes_df.loc[idx]
        cols = tuple(row.dropna().index.tolist())
        groups.setdefault(cols, []).append(idx)

    def _do_update():
        method = getattr(raw, update_method_name)
        for cols, ids in groups.items():
            subset = changes_df.loc[ids, list(cols)]
            method(subset)

    run(_do_update)
    st.session_state.pop("_vl_lookup_cache", None)


# Component label -> creation spec. For now only node-breaker feeder-bay
# creation is exposed; the backend handles the disconnector + breaker switches
# internally so the user only has to pick a busbar section.
ENERGY_SOURCES = ["OTHER", "HYDRO", "NUCLEAR", "WIND", "SOLAR", "THERMAL"]
FEEDER_DIRECTIONS = ["TOP", "BOTTOM"]
LOAD_TYPES = ["UNDEFINED", "AUXILIARY", "FICTITIOUS"]
SVC_REGULATION_MODES = ["VOLTAGE", "REACTIVE_POWER"]

# Each field is a dict with:
#   name:     pypowsybl attribute name on the _bay DataFrame
#   label:    human label shown in the form
#   kind:     'text' | 'float' | 'int' | 'bool' | 'select'
#   required: bool  (empty string / None is invalid if True)
#   default:  initial widget value
#   options:  for 'select', list of allowed values
#   help:     optional Streamlit help text
# Shared locator fields (bus_or_busbar_section_id, position_order, direction)
# are added by the form renderer — they're not listed per component.

_POSITION_HELP = "Order of this feeder on the busbar (ConnectablePosition extension)."

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
_SHUNT_LINEAR_FIELDS = {"g_per_section", "b_per_section", "max_section_count"}

# Shared locator fields appended to every creation form.
LOCATOR_FIELDS: list[dict] = [
    {"name": "position_order", "label": "Position order",
     "kind": "int", "required": True, "default": 10,
     "min_value": 0, "step": 10, "help": _POSITION_HELP},
    {"name": "direction", "label": "Direction", "kind": "select",
     "required": False, "default": "BOTTOM", "options": FEEDER_DIRECTIONS},
]


def _validate_minmax_p(fields: dict) -> list[str]:
    errors = []
    if fields.get("max_p") is not None and fields.get("min_p") is not None:
        if fields["max_p"] < fields["min_p"]:
            errors.append("max_p must be >= min_p.")
    return errors


def _validate_voltage_regulator(fields: dict) -> list[str]:
    """voltage_regulator_on=True requires target_v > 0."""
    errors = []
    if fields.get("voltage_regulator_on"):
        if not fields.get("target_v") or fields["target_v"] <= 0:
            errors.append("target_v must be > 0 when voltage regulator is on.")
    return errors


def _validate_generator(fields: dict) -> list[str]:
    return _validate_minmax_p(fields) + _validate_voltage_regulator(fields)


def _validate_svc(fields: dict) -> list[str]:
    errors = []
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
    errors = []
    section_count = fields.get("section_count")
    max_sc = fields.get("max_section_count")
    if section_count is not None and max_sc is not None and section_count > max_sc:
        errors.append("Initial section count must be <= max_section_count.")
    return errors


_VALIDATORS = {
    "_validate_generator": _validate_generator,
    "_validate_minmax_p": _validate_minmax_p,
    "_validate_voltage_regulator": _validate_voltage_regulator,
    "_validate_svc": _validate_svc,
    "_validate_shunt": _validate_shunt,
}


def validate_create_fields(component: str, fields: dict) -> list[str]:
    """Run registry-driven validation; returns a list of human-readable errors.

    Checks required component fields + the shared locator fields + the
    bus_or_busbar_section_id context field (filled in by the form renderer
    from the busbar picker). Runs the component's ``validate`` hook last.
    """
    spec = CREATABLE_COMPONENTS.get(component)
    if not spec:
        return [f"{component!r} is not creatable"]
    errors = []
    for f in spec["fields"] + LOCATOR_FIELDS:
        if f["required"] and (fields.get(f["name"]) is None
                               or fields.get(f["name"]) == ""):
            errors.append(f"{f['label']} is required.")
    if not fields.get("bus_or_busbar_section_id"):
        errors.append("Busbar section is required.")
    hook = spec.get("validate")
    if hook and hook in _VALIDATORS:
        errors.extend(_VALIDATORS[hook](fields))
    return errors


def list_node_breaker_voltage_levels(network):
    """Return node-breaker voltage levels as a DataFrame with id/display/substation_id/nominal_v."""
    vls = network.get_voltage_levels(all_attributes=True)
    if "topology_kind" not in vls.columns:
        return pd.DataFrame(columns=["id", "display", "substation_id", "nominal_v"])
    nb = vls[vls["topology_kind"] == "NODE_BREAKER"].reset_index()
    if nb.empty:
        return pd.DataFrame(columns=["id", "display", "substation_id", "nominal_v"])
    nb["display"] = nb.apply(lambda r: r["name"] if r["name"] else r["id"], axis=1)
    return nb[["id", "display", "substation_id", "nominal_v"]].sort_values("display")


def list_busbar_sections(network, voltage_level_id: str):
    """Return a sorted list of busbar section ids in the given voltage level."""
    bbs = network.get_busbar_sections()
    if bbs.empty:
        return []
    return sorted(bbs[bbs["voltage_level_id"] == voltage_level_id].index.tolist())


def _dispatch_bay_create(network, bay_fn_name: str, fields: dict):
    """Build a one-row DataFrame and call pypowsybl's ``<bay_fn_name>``."""
    row = {k: v for k, v in fields.items() if v is not None and v != ""}
    df = pd.DataFrame([row]).set_index("id")
    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        import pypowsybl.network as pn
        getattr(pn, bay_fn_name)(raw, df)

    run(_do_create)
    st.session_state.pop("_vl_lookup_cache", None)
    st.session_state.pop("_map_data_cache", None)


def _dispatch_shunt_bay(network, fields: dict):
    """Shunt compensator bay creation needs 3 dataframes (shunt + linear + non-linear).

    We only support the LINEAR model for now; non_linear_model_df is empty.
    Linear-model columns are peeled off the flat ``fields`` dict before
    building the two input dataframes.
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
    st.session_state.pop("_vl_lookup_cache", None)
    st.session_state.pop("_map_data_cache", None)


def create_component_bay(network, component: str, fields: dict):
    """Create a new injection on a busbar section via a clean feeder bay.

    Routes through pypowsybl's ``create_*_bay`` helper which, in node-breaker
    voltage levels, allocates nodes and inserts a closed disconnector plus a
    breaker between the busbar section and the new injection. Callers supply
    the busbar id and the injection attributes; node numbering stays internal.
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


# --- Branches (two-end connectables: lines + 2-winding transformers) ---

# Shared locator fields for each side of a branch. Rendered twice (sides 1 + 2)
# by the form renderer; keys are ``*_1`` / ``*_2``.
_BRANCH_SIDE_LOCATOR = [
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
    """Locator fields for one side of a branch, with names suffixed ``_<side>``."""
    return [
        {**f, "name": f"{f['name']}_{side}", "label": f"{f['label']} {side}"}
        for f in _BRANCH_SIDE_LOCATOR
    ]


def validate_create_branch_fields(component: str, fields: dict, network=None) -> list[str]:
    """Validate branch creation fields. Checks electrical fields + side-1/side-2
    locator fields + busbar ids; if ``same_substation`` is set and a network is
    supplied, verifies both chosen busbar sections live in the same substation.
    """
    spec = CREATABLE_BRANCHES.get(component)
    if not spec:
        return [f"{component!r} is not a creatable branch"]
    errors = []
    all_required = (
        spec["fields"]
        + branch_side_locator_fields(1)
        + branch_side_locator_fields(2)
    )
    for f in all_required:
        if f["required"] and (fields.get(f["name"]) is None
                               or fields.get(f["name"]) == ""):
            errors.append(f"{f['label']} is required.")
    for side in (1, 2):
        key = f"bus_or_busbar_section_id_{side}"
        if not fields.get(key):
            errors.append(f"Busbar section {side} is required.")
    if spec.get("same_substation") and network is not None:
        sub = _substations_of_bbs(
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


def _substations_of_bbs(network, bbs1: str, bbs2: str):
    """Return ``(sub1, sub2)`` for the two busbar sections, or None if missing."""
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


def create_branch_bay(network, component: str, fields: dict):
    """Create a new line or 2-winding transformer with feeder bays on each side.

    ``fields`` must include the electrical fields, ``bus_or_busbar_section_id_1``,
    ``bus_or_busbar_section_id_2``, and ``position_order_1``/``_2``. Routes
    through the pypowsybl worker thread exactly like :func:`create_component_bay`.
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


# --- Containers (substations, voltage levels, busbar sections) ---

TOPOLOGY_KINDS = ["NODE_BREAKER", "BUS_BREAKER"]

# Container label -> creation spec. Unlike injections these don't go through a
# ``_bay`` helper: they call the plain ``create_<type>s`` method on the network.
# The spec lists the generic fields; anything that depends on existing network
# state (substation picker, VL picker, auto-node) is handled in the form.
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
    errors = []
    low = fields.get("low_voltage_limit")
    high = fields.get("high_voltage_limit")
    if low and high and low > 0 and high > 0 and high < low:
        errors.append("high_voltage_limit must be >= low_voltage_limit.")
    return errors


_VALIDATORS["_validate_voltage_level"] = _validate_voltage_level


def validate_create_container_fields(component: str, fields: dict) -> list[str]:
    """Check required fields for a container creation spec + component-specific rules."""
    spec = CREATABLE_CONTAINERS.get(component)
    if not spec:
        return [f"{component!r} is not a creatable container"]
    errors = []
    for f in spec["fields"]:
        if f["required"] and (fields.get(f["name"]) is None
                               or fields.get(f["name"]) == ""):
            errors.append(f"{f['label']} is required.")
    if component == "Busbar Sections" and not fields.get("voltage_level_id"):
        errors.append("Voltage level is required.")
    hook = spec.get("validate")
    if hook and hook in _VALIDATORS:
        errors.extend(_VALIDATORS[hook](fields))
    return errors


def list_substations_df(network):
    """Return substations as a DataFrame with id/display, sorted by display."""
    subs = network.get_substations(attributes=["name"]).reset_index()
    if subs.empty:
        return pd.DataFrame(columns=["id", "display"])
    subs["display"] = subs.apply(
        lambda r: r["name"] if r["name"] else r["id"], axis=1
    )
    return subs[["id", "display"]].sort_values("display")


def next_free_node(network, voltage_level_id: str) -> int:
    """Suggest the next unused node index in a node-breaker voltage level.

    Scans busbar section ``node`` and switch ``node1``/``node2`` columns for
    the VL. Returns ``max(used) + 1``, or 0 when the VL has no elements yet.
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


def create_container(network, component: str, fields: dict):
    """Create a substation, voltage level, or busbar section on the network.

    Drops empty strings and "unset" sentinels (e.g. ``low_voltage_limit=0``)
    so pypowsybl treats them as missing rather than literal values.
    Dispatches through the worker thread like :func:`create_component_bay`.
    """
    if component not in CREATABLE_CONTAINERS:
        raise ValueError(f"{component!r} is not a creatable container")

    errors = validate_create_container_fields(component, fields)
    if errors:
        raise ValueError("; ".join(errors))

    spec = CREATABLE_CONTAINERS[component]
    # Drop blanks + zero-sentinels so pypowsybl treats them as unset.
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
    st.session_state.pop("_vl_lookup_cache", None)
    st.session_state.pop("_map_data_cache", None)


def get_voltage_levels_df(network):
    vls = network.get_voltage_levels(attributes=["name", "substation_id", "nominal_v"])
    vls = vls.reset_index()
    vls["display"] = vls.apply(
        lambda r: r["name"] if r["name"] else r["id"], axis=1
    )
    return vls.sort_values("display")


def filter_voltage_levels(vls_df, text):
    if not text:
        return vls_df
    mask = vls_df["display"].str.contains(text, case=False, na=False, regex=False)
    return vls_df[mask]


# --- Tap changers (ratio + phase) on existing 2-winding transformers ---

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
        "step_defaults": {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.0, "alpha": 0.0},
    },
}


def list_two_winding_transformers(network):
    """Return 2WT ids sorted alphabetically (empty list if none)."""
    try:
        twts = network.get_2_windings_transformers(attributes=["name"])
    except Exception:
        return []
    return sorted(twts.index.tolist())


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
    errors = []
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
                errors.append("OLTC must be enabled to set regulating=True on a ratio tap changer.")
            if not main_fields.get("target_v") or main_fields["target_v"] <= 0:
                errors.append("target_v must be > 0 when the ratio tap changer is regulating.")
    return errors


def create_tap_changer(
    network, kind: str, transformer_id: str, main_fields: dict, steps: list[dict]
):
    """Create a ratio or phase tap changer on an existing 2-winding transformer.

    Runs validation on the main thread then dispatches the two dataframes
    (tap-changer attributes + per-step data) to pypowsybl via the worker.
    ``steps`` is a list of dicts matching ``CREATABLE_TAP_CHANGERS[kind]``'s
    ``step_columns``; one row is emitted per entry.
    """
    if kind not in CREATABLE_TAP_CHANGERS:
        raise ValueError(f"{kind!r} tap changer is not creatable")

    errors = validate_create_tap_changer_fields(
        kind, transformer_id, main_fields, steps
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
    st.session_state.pop("_vl_lookup_cache", None)
    st.session_state.pop("_map_data_cache", None)


# --- Coupling device (switches tying two busbar sections together) ---

def create_coupling_device(
    network, bbs1: str, bbs2: str, switch_prefix: str | None = None
):
    """Create a coupling device between two busbar sections in the same VL.

    In node-breaker topology pypowsybl inserts a closed breaker plus closed
    disconnectors on both busbar sections, and open disconnectors on any
    parallel busbar sections. In bus-breaker topology only a breaker is
    added. Routed through the worker thread like every other pypowsybl call.
    """
    if not bbs1 or not bbs2:
        raise ValueError("Both busbar sections are required.")
    if bbs1 == bbs2:
        raise ValueError("The two busbar sections must differ.")

    bbs = network.get_busbar_sections()
    if bbs1 not in bbs.index or bbs2 not in bbs.index:
        raise ValueError("Unknown busbar section id.")
    vl1 = bbs.loc[bbs1, "voltage_level_id"]
    vl2 = bbs.loc[bbs2, "voltage_level_id"]
    if vl1 != vl2:
        raise ValueError(
            f"A coupling device must tie busbar sections of the same voltage level "
            f"(got {vl1!r} and {vl2!r})."
        )

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
    st.session_state.pop("_vl_lookup_cache", None)
    st.session_state.pop("_map_data_cache", None)
