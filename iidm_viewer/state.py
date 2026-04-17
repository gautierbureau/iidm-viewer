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
    "Lines": (
        "update_lines",
        ["r", "x", "g1", "b1", "g2", "b2", "connected1", "connected2"],
    ),
    "2-Winding Transformers": (
        "update_2_windings_transformers",
        ["r", "x", "g", "b", "connected1", "connected2"],
    ),
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
}

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


_VALIDATORS = {
    "_validate_generator": _validate_generator,
    "_validate_minmax_p": _validate_minmax_p,
    "_validate_voltage_regulator": _validate_voltage_regulator,
    "_validate_svc": _validate_svc,
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
