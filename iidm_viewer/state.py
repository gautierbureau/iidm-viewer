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


def get_import_extensions() -> list[str]:
    """Return file extensions accepted by pypowsybl, discovered at runtime.

    Result is cached in session state so the worker is only hit once per
    browser session. ``zip`` is always included for pre-zipped archives.
    """
    if "import_extensions" not in st.session_state:
        def _get():
            import pypowsybl.network as pn
            return pn.get_import_supported_extensions()
        raw = run(_get)
        seen: set[str] = set()
        exts: list[str] = []
        for e in raw:
            e_lower = e.lower()
            if e_lower not in seen:
                seen.add(e_lower)
                exts.append(e_lower)
        if "zip" not in seen:
            exts.append("zip")
        st.session_state["import_extensions"] = exts
    return st.session_state["import_extensions"]


def get_export_formats() -> list[str]:
    """Return export format names supported by pypowsybl, cached per session."""
    if "export_formats" not in st.session_state:
        def _get():
            import pypowsybl.network as pn
            return pn.get_export_formats()
        st.session_state["export_formats"] = run(_get)
    return st.session_state["export_formats"]


def export_network(network, format_name: str) -> tuple[bytes, str]:
    """Export the network; return (bytes, file_extension).

    pypowsybl wraps some formats (e.g. XIIDM) in a ZIP archive.  Single-file
    ZIPs are unwrapped so the caller gets the real content and the correct
    extension.  Multi-file ZIPs are served as-is with extension ``zip``.
    """
    import io as _io
    import zipfile as _zf

    raw = object.__getattribute__(network, "_obj")

    def _export():
        return raw.save_to_binary_buffer(format_name).getvalue()

    data = run(_export)

    if data[:2] == b'PK':
        try:
            with _zf.ZipFile(_io.BytesIO(data)) as zf:
                names = zf.namelist()
                if len(names) == 1:
                    inner = zf.read(names[0])
                    ext = names[0].rsplit(".", 1)[-1] if "." in names[0] else format_name.lower()
                    return inner, ext
        except Exception:
            pass
        return data, "zip"

    return data, format_name.lower()


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
    st.session_state.pop("vl_selectbox", None)
    st.session_state["vl_filter_text"] = ""
    st.session_state.pop("_lf_report_json", None)
    st.session_state.pop("_map_data_cache", None)
    st.session_state.pop("_vl_lookup_cache", None)
    st.session_state.pop("_export_bytes", None)
    st.session_state.pop("_export_fmt", None)
    st.session_state.pop("_export_ext", None)
    for k in [k for k in st.session_state if k.startswith("_change_log_") or k.startswith("_removal_log_") or k.startswith("_ext_change_log_") or k.startswith("_ext_removal_log_") or k.startswith("_export_cache_")]:
        del st.session_state[k]
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
    st.session_state.pop("vl_selectbox", None)
    st.session_state["vl_filter_text"] = ""
    st.session_state.pop("_lf_report_json", None)
    st.session_state.pop("_map_data_cache", None)
    st.session_state.pop("_vl_lookup_cache", None)
    st.session_state.pop("_last_file", None)
    st.session_state.pop("_export_bytes", None)
    st.session_state.pop("_export_fmt", None)
    st.session_state.pop("_export_ext", None)
    for k in [k for k in st.session_state if k.startswith("_change_log_") or k.startswith("_removal_log_") or k.startswith("_ext_change_log_") or k.startswith("_ext_removal_log_") or k.startswith("_export_cache_")]:
        del st.session_state[k]
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
        import pypowsybl.report as r
        params = lf.Parameters(**generic)
        if provider:
            params.provider_parameters = {k: str(v) for k, v in provider.items()}
        rn = r.ReportNode(task_key="loadFlowTask", default_name="Load Flow")
        results = lf.run_ac(raw, parameters=params, report_node=rn)
        # Extract JSON string inside worker thread before the handle escapes
        return results, rn.to_json()

    results, report_json = run(_run_ac)
    st.session_state["_lf_report_json"] = report_json
    # Invalidate cached lookups so tabs reload fresh data
    st.session_state.pop("_vl_lookup_cache", None)
    return results


# Component label -> (update method, [editable attributes])
EDITABLE_COMPONENTS: dict[str, tuple[str, list[str]]] = {
    "Loads": ("update_loads", ["p0", "q0", "connected"]),
    "Generators": (
        "update_generators",
        ["target_p", "target_v", "target_q", "voltage_regulator_on", "regulated_element_id", "connected"],
    ),
    "Batteries": ("update_batteries", ["target_p", "target_q", "connected"]),
    "Switches": ("update_switches", ["open"]),
    "Shunt Compensators": ("update_shunt_compensators", ["section_count", "connected"]),
    "Static VAR Compensators": (
        "update_static_var_compensators",
        ["regulation_mode", "voltage_setpoint", "reactive_power_setpoint", "regulated_element_id", "connected"],
    ),
    "VSC Converter Stations": (
        "update_vsc_converter_stations",
        ["target_v", "target_q", "voltage_regulator_on", "regulated_element_id", "connected"],
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


# Injection types: pn.remove_feeder_bays removes the element AND its bay switches
# (breaker + disconnectors), which is the correct deep-removal for node-breaker topology.
_FEEDER_BAY_TYPES: frozenset[str] = frozenset({
    "Loads",
    "Generators",
    "Batteries",
    "Shunt Compensators",
    "Static VAR Compensators",
})

# HVDC triples: removing any one of the three elements (line or either station)
# must cascade to remove all three — the line is removed first so the stations
# are no longer attached to it, then the station bays are cleaned up.
_HVDC_TYPES: frozenset[str] = frozenset({
    "HVDC Lines",
    "VSC Converter Stations",
    "LCC Converter Stations",
})

# Branch/link types removed via the generic remove_elements API.
_SHALLOW_REMOVE_TYPES: frozenset[str] = frozenset({
    "Lines",
    "2-Winding Transformers",
    "Dangling Lines",
})

REMOVABLE_COMPONENTS: frozenset[str] = (
    _FEEDER_BAY_TYPES | _HVDC_TYPES | _SHALLOW_REMOVE_TYPES
    | {"Voltage Levels", "Substations"}
)


def _find_vl_ids_for_substations(network, substation_ids: list[str]) -> list[str]:
    """Return all voltage level ids that belong to the given substations."""
    raw = object.__getattribute__(network, "_obj")
    sub_set = set(substation_ids)

    def _gather():
        vl_df = raw.get_voltage_levels()
        if vl_df.empty or "substation_id" not in vl_df.columns:
            return []
        return vl_df[vl_df["substation_id"].isin(sub_set)].index.tolist()

    return run(_gather)


def _resolve_hvdc_removal(
    network, component: str, ids: list[str]
) -> tuple[list[str], list[str]]:
    """Expand an HVDC removal request to the full triple: stations + line.

    In IIDM an HVDC set is always exactly one line and two converter stations.
    Removing any element in the set triggers removal of all three, so this
    function returns (station_ids, hvdc_line_ids) for the caller to act on.
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


def remove_components(network, component: str, ids: list[str]) -> list[str]:
    """Remove elements from the network on the worker thread.

    Returns the complete list of element ids that were actually removed,
    which may be larger than *ids* for HVDC, Voltage Level, and Substation
    cascades.

    - Plain injections: pn.remove_feeder_bays — deep bay-switch removal.
    - HVDC triples: pn.remove_hvdc_lines — handles line and both stations.
    - Voltage Levels: pn.remove_voltage_levels — cascades all connectables.
    - Substations: resolve contained VLs, then pn.remove_voltage_levels.
    - Branches: individual shallow remove_* methods via the raw network object.
    """
    raw = object.__getattribute__(network, "_obj")

    if component in _FEEDER_BAY_TYPES:
        def _do_remove():
            import pypowsybl.network as pn
            pn.remove_feeder_bays(raw, ids)
        run(_do_remove)
        st.session_state.pop("_vl_lookup_cache", None)
        return ids

    if component in _HVDC_TYPES:
        station_ids, hvdc_line_ids = _resolve_hvdc_removal(network, component, ids)

        def _do_remove():
            import pypowsybl.network as pn
            pn.remove_hvdc_lines(raw, hvdc_line_ids)

        run(_do_remove)
        st.session_state.pop("_vl_lookup_cache", None)
        return station_ids + hvdc_line_ids

    if component == "Voltage Levels":
        def _do_remove():
            import pypowsybl.network as pn
            pn.remove_voltage_levels(raw, ids)

        run(_do_remove)
        st.session_state.pop("_vl_lookup_cache", None)
        return ids

    if component == "Substations":
        vl_ids = _find_vl_ids_for_substations(network, ids)

        def _do_remove():
            import pypowsybl.network as pn
            if vl_ids:
                pn.remove_voltage_levels(raw, vl_ids)

        run(_do_remove)
        st.session_state.pop("_vl_lookup_cache", None)
        return ids + vl_ids

    # Shallow branch removal via generic remove_elements
    def _do_remove():
        raw.remove_elements(ids)

    run(_do_remove)
    st.session_state.pop("_vl_lookup_cache", None)
    return ids


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


# Extension name -> list of columns that pypowsybl's update_extensions accepts.
#
# Extensions not listed here (substationPosition, position, slackTerminal, ...)
# have immutable columns on the Java side and can only be changed by
# remove_extensions + create_extensions. We keep those read-only for now; users
# can still remove + recreate them via the Data Explorer Components tab.
EDITABLE_EXTENSIONS: dict[str, list[str]] = {
    "activePowerControl": [
        "participate", "droop", "participation_factor",
        "min_target_p", "max_target_p",
    ],
    "busbarSectionPosition": ["busbar_index", "section_index"],
    "entsoeArea": ["code"],
    "entsoeCategory": ["code"],
    "hvdcAngleDroopActivePowerControl": ["droop", "p0", "enabled"],
    "hvdcOperatorActivePowerRange": [
        "opr_from_cs1_to_cs2", "opr_from_cs2_to_cs1",
    ],
    "standbyAutomaton": [
        "standby", "b0",
        "low_voltage_threshold", "low_voltage_setpoint",
        "high_voltage_threshold", "high_voltage_setpoint",
    ],
    "voltagePerReactivePowerControl": ["slope"],
    "voltageRegulation": [
        "voltage_regulator_on", "target_v", "regulated_element_id",
    ],
}


def remove_extension(network, extension_name: str, ids: list):
    """Remove extension rows from the network on the worker thread."""
    raw = object.__getattribute__(network, "_obj")

    def _do_remove():
        raw.remove_extensions(extension_name, ids)

    run(_do_remove)
    st.session_state.pop("_vl_lookup_cache", None)


def update_extension(network, extension_name: str, changes_df):
    """Apply a DataFrame of changes to an extension via ``update_extensions``.

    *changes_df* is indexed by the extension's native index (usually the
    element id) and may contain NaN for cells that didn't change. pypowsybl
    rejects NaN values, so we group rows by their non-null column set and
    issue one update call per group — same shape as :func:`update_components`.
    """
    if changes_df.empty:
        return
    if extension_name not in EDITABLE_EXTENSIONS:
        raise ValueError(f"Extension {extension_name!r} is not editable.")
    raw = object.__getattribute__(network, "_obj")

    groups: dict[tuple[str, ...], list] = {}
    for idx in changes_df.index:
        row = changes_df.loc[idx]
        cols = tuple(row.dropna().index.tolist())
        groups.setdefault(cols, []).append(idx)

    def _do_update():
        for cols, ids in groups.items():
            subset = changes_df.loc[ids, list(cols)]
            raw.update_extensions(extension_name, subset)

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


# --- HVDC lines (attach to two existing converter stations) ---

CONVERTERS_MODES = [
    "SIDE_1_RECTIFIER_SIDE_2_INVERTER",
    "SIDE_1_INVERTER_SIDE_2_RECTIFIER",
]

# HVDC lines are created directly via network.create_hvdc_lines — no _bay.
# The two endpoints are *existing* VSC/LCC converter stations (not busbars).
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


def list_converter_stations(network):
    """Return ``[(id, kind)]`` for every VSC and LCC converter station."""
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
    """Required fields + distinct endpoints; all other checks go to pypowsybl."""
    errors = []
    for f in CREATABLE_HVDC_LINES["fields"]:
        if f["required"] and (fields.get(f["name"]) is None
                               or fields.get(f["name"]) == ""):
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


def create_hvdc_line(network, fields: dict):
    """Create an HVDC line between two existing converter stations.

    Validates the endpoints and electrical attributes on the main thread,
    then dispatches ``network.create_hvdc_lines`` via the worker. The two
    stations must already exist and must not already be connected to an
    HVDC line.
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
    st.session_state.pop("_vl_lookup_cache", None)
    st.session_state.pop("_map_data_cache", None)


# --- Reactive limits (min/max or per-P curve) on generators / VSC / batteries ---

REACTIVE_LIMITS_TARGETS = {
    "Generators": "get_generators",
    "Batteries": "get_batteries",
    "VSC Converter Stations": "get_vsc_converter_stations",
}


def list_reactive_limit_candidates(network, component: str) -> list[str]:
    """Return ids of elements that can carry reactive limits for the given component."""
    getter = REACTIVE_LIMITS_TARGETS.get(component)
    if not getter:
        return []
    try:
        df = getattr(network, getter)()
    except Exception:
        return []
    return sorted(df.index.tolist())


def create_reactive_limits(
    network, element_id: str, mode: str, payload: list[dict]
):
    """Attach reactive limits (min/max or per-P curve) to an existing element.

    ``mode`` is ``"minmax"`` (one row with ``min_q``/``max_q``) or
    ``"curve"`` (>=2 rows of ``p``/``min_q``/``max_q``). pypowsybl replaces
    any existing reactive limits on the target. Routed via the worker.
    """
    if mode not in ("minmax", "curve"):
        raise ValueError(f"Unknown reactive-limits mode: {mode!r}")
    if not element_id:
        raise ValueError("Target element id is required.")
    if not payload:
        raise ValueError("At least one row is required.")

    if mode == "minmax":
        row = payload[0]
        if row.get("min_q") is None or row.get("max_q") is None:
            raise ValueError("min_q and max_q are required.")
        if row["max_q"] < row["min_q"]:
            raise ValueError("max_q must be >= min_q.")
        df = pd.DataFrame(
            [{"id": element_id, "min_q": row["min_q"], "max_q": row["max_q"]}]
        ).set_index("id")

        raw = object.__getattribute__(network, "_obj")

        def _do_create():
            raw.create_minmax_reactive_limits(df)
    else:
        rows = []
        for row in payload:
            for k in ("p", "min_q", "max_q"):
                if row.get(k) is None:
                    raise ValueError(f"Curve rows need non-null {k}.")
            if row["max_q"] < row["min_q"]:
                raise ValueError("max_q must be >= min_q at every active power point.")
            rows.append({
                "id": element_id, "p": row["p"],
                "min_q": row["min_q"], "max_q": row["max_q"],
            })
        if len({r["p"] for r in rows}) < 2:
            raise ValueError("A reactive capability curve needs at least 2 distinct p points.")
        df = pd.DataFrame(rows).set_index("id")

        raw = object.__getattribute__(network, "_obj")

        def _do_create():
            raw.create_curve_reactive_limits(df)

    run(_do_create)
    st.session_state.pop("_vl_lookup_cache", None)


# --- Operational limits (CURRENT / APPARENT_POWER / ACTIVE_POWER) ---

OPERATIONAL_LIMIT_TYPES = ["CURRENT", "APPARENT_POWER", "ACTIVE_POWER"]
OPERATIONAL_LIMIT_SIDES = ["ONE", "TWO"]

# Component label → getter method, to enumerate target elements.
OPERATIONAL_LIMITS_TARGETS = {
    "Lines": "get_lines",
    "2-Winding Transformers": "get_2_windings_transformers",
    "Dangling Lines": "get_dangling_lines",
}

# Permanent limit's acceptable_duration is -1; data-editor-friendly sentinel.
PERMANENT_DURATION = -1


def list_operational_limit_candidates(network, component: str) -> list[str]:
    getter = OPERATIONAL_LIMITS_TARGETS.get(component)
    if not getter:
        return []
    try:
        df = getattr(network, getter)()
    except Exception:
        return []
    return sorted(df.index.tolist())


def create_operational_limits(
    network,
    element_id: str,
    side: str,
    limit_type: str,
    limits: list[dict],
    group_name: str = "DEFAULT",
):
    """Create a group of operational limits on one side of an element.

    ``limits`` is a list of dicts with ``name``, ``value``, and
    ``acceptable_duration`` (use ``-1`` for the permanent limit). Exactly
    one permanent limit is allowed per (element, side, group). pypowsybl
    replaces any existing limits in the target group.
    """
    if element_id is None or element_id == "":
        raise ValueError("Target element id is required.")
    if side not in OPERATIONAL_LIMIT_SIDES:
        raise ValueError(f"Side must be one of {OPERATIONAL_LIMIT_SIDES}.")
    if limit_type not in OPERATIONAL_LIMIT_TYPES:
        raise ValueError(f"Type must be one of {OPERATIONAL_LIMIT_TYPES}.")
    if not limits:
        raise ValueError("At least one limit row is required.")

    rows = []
    permanent = 0
    for lim in limits:
        if lim.get("value") is None:
            raise ValueError("Every limit needs a value.")
        if lim["value"] < 0:
            raise ValueError("Limit values must be non-negative.")
        duration = lim.get("acceptable_duration")
        if duration is None:
            raise ValueError("Every limit needs an acceptable_duration (-1 for permanent).")
        duration = int(duration)
        if duration == -1:
            permanent += 1
        elif duration < 0:
            raise ValueError("acceptable_duration must be -1 (permanent) or >= 0.")
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
            "group_name": group_name,
        })
    if permanent != 1:
        raise ValueError(
            "Exactly one permanent limit (acceptable_duration = -1) is required."
        )

    # pypowsybl's create_operational_limits expects element_id as the df index.
    df = pd.DataFrame(rows).set_index("element_id")
    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        raw.create_operational_limits(df)

    run(_do_create)
    st.session_state.pop("_vl_lookup_cache", None)


# --- Extensions (first-phase: attach extension rows to existing elements) ---
#
# pypowsybl exposes 27 network extensions. This registry wires up an initial
# set that pairs directly with equipment the app can already create, so users
# can fill out geographical position / controls / HVDC droop / ENTSO-E tags
# without hand-editing XML. Read-only browsing of every extension already
# lives in `extensions_explorer.py`; this module adds write support.
#
# Each schema entry contains:
#   label        — human-readable name shown in the form
#   detail       — help caption
#   index        — the pypowsybl index column for create_extensions (usually
#                  "id", but slackTerminal is indexed by "voltage_level_id")
#   targets      — dict of component label -> getter method name, used to
#                  populate the target dropdown from existing elements
#   fields       — list of {name, kind, required, default, help, options,
#                  optional_fill}. ``kind`` drives the input widget and type
#                  coercion; ``optional_fill`` marks columns that can be
#                  dropped from the dataframe when blank.

_EXT_KIND_COERCIONS = {
    "float": float,
    "int": int,
    "bool": bool,
    "str": str,
}


CREATABLE_EXTENSIONS: dict[str, dict] = {
    "substationPosition": {
        "label": "Substation position (lat/long)",
        "detail": "Geographical coordinates used by the map tab.",
        "index": "id",
        "targets": {"Substations": "get_substations"},
        "fields": [
            {"name": "latitude", "kind": "float", "required": True, "default": 0.0,
             "help": "Decimal degrees."},
            {"name": "longitude", "kind": "float", "required": True, "default": 0.0,
             "help": "Decimal degrees."},
        ],
    },
    "entsoeArea": {
        "label": "ENTSO-E area code",
        "detail": "Two-letter ENTSO-E country / area code (e.g. FR, DE).",
        "index": "id",
        "targets": {"Substations": "get_substations"},
        "fields": [
            {"name": "code", "kind": "str", "required": True, "default": "",
             "help": "ENTSO-E area code."},
        ],
    },
    "busbarSectionPosition": {
        "label": "Busbar section position (row / section)",
        "detail": "Drives the SLD layout: which busbar row and section the BBS sits on.",
        "index": "id",
        "targets": {"Busbar Sections": "get_busbar_sections"},
        "fields": [
            {"name": "busbar_index", "kind": "int", "required": True, "default": 1,
             "help": "1-based row index."},
            {"name": "section_index", "kind": "int", "required": True, "default": 1,
             "help": "1-based section index along the row."},
        ],
    },
    "position": {
        "label": "Connectable position (order / feeder name)",
        "detail": "Feeder order / direction used by the SLD; injections only in this phase.",
        "index": "id",
        "targets": {
            "Generators": "get_generators",
            "Loads": "get_loads",
            "Batteries": "get_batteries",
            "Shunt Compensators": "get_shunt_compensators",
            "Static VAR Compensators": "get_static_var_compensators",
            "LCC Converter Stations": "get_lcc_converter_stations",
            "VSC Converter Stations": "get_vsc_converter_stations",
        },
        "fields": [
            {"name": "order", "kind": "int", "required": True, "default": 10,
             "help": "Feeder order on the busbar."},
            {"name": "feeder_name", "kind": "str", "required": False, "default": "",
             "optional_fill": True, "help": "Feeder label (defaults to element id)."},
            {"name": "direction", "kind": "choice", "required": True, "default": "TOP",
             "options": ["TOP", "BOTTOM", "UNDEFINED"],
             "help": "Feeder direction on the SLD."},
            {"name": "side", "kind": "str", "required": False, "default": "",
             "optional_fill": True,
             "help": "Leave blank for injections. For branches use ONE or TWO."},
        ],
    },
    "slackTerminal": {
        "label": "Slack terminal",
        "detail": "Overrides the slack bus chosen by the load flow.",
        "index": "voltage_level_id",
        "targets": {"Voltage Levels": "get_voltage_levels"},
        "fields": [
            {"name": "bus_id", "kind": "str", "required": False, "default": "",
             "optional_fill": True,
             "help": "Bus view bus id. Fill either bus_id OR element_id, not both."},
            {"name": "element_id", "kind": "str", "required": False, "default": "",
             "optional_fill": True,
             "help": "Connectable id (e.g. a generator). Fill either this OR bus_id."},
        ],
    },
    "activePowerControl": {
        "label": "Active power control (participation / droop)",
        "detail": "Controls how the element participates in load-flow balancing.",
        "index": "id",
        "targets": {
            "Generators": "get_generators",
            "Batteries": "get_batteries",
        },
        "fields": [
            {"name": "participate", "kind": "bool", "required": True, "default": True,
             "help": "Whether the unit participates in balancing."},
            {"name": "droop", "kind": "float", "required": True, "default": 4.0,
             "help": "Droop coefficient (% or MW/Hz depending on provider)."},
            {"name": "participation_factor", "kind": "float", "required": False,
             "default": 1.0, "optional_fill": True,
             "help": "Proportional participation weight."},
            {"name": "min_target_p", "kind": "float", "required": False,
             "default": None, "optional_fill": True,
             "help": "Lower bound applied during balancing."},
            {"name": "max_target_p", "kind": "float", "required": False,
             "default": None, "optional_fill": True,
             "help": "Upper bound applied during balancing."},
        ],
    },
    "voltageRegulation": {
        "label": "Voltage regulation (battery)",
        "detail": "Enables voltage regulation on a battery (regulated element can be any bus).",
        "index": "id",
        "targets": {"Batteries": "get_batteries"},
        "fields": [
            {"name": "voltage_regulator_on", "kind": "bool", "required": True,
             "default": True, "help": "Whether regulation is active."},
            {"name": "target_v", "kind": "float", "required": True, "default": 0.0,
             "help": "Target voltage (kV)."},
            {"name": "regulated_element_id", "kind": "str", "required": False,
             "default": "", "optional_fill": True,
             "help": "Regulated terminal element (defaults to self)."},
        ],
    },
    "voltagePerReactivePowerControl": {
        "label": "V/Q slope (SVC)",
        "detail": "Slope (kV/MVar) applied by the SVC voltage regulator.",
        "index": "id",
        "targets": {"Static VAR Compensators": "get_static_var_compensators"},
        "fields": [
            {"name": "slope", "kind": "float", "required": True, "default": 0.01,
             "help": "Slope in kV/MVar."},
        ],
    },
    "standbyAutomaton": {
        "label": "Standby automaton (SVC)",
        "detail": "Standby mode and low/high voltage thresholds + setpoints.",
        "index": "id",
        "targets": {"Static VAR Compensators": "get_static_var_compensators"},
        "fields": [
            {"name": "standby", "kind": "bool", "required": True, "default": False,
             "help": "Whether the SVC is in standby."},
            {"name": "b0", "kind": "float", "required": True, "default": 0.0,
             "help": "Susceptance in standby mode (S)."},
            {"name": "low_voltage_threshold", "kind": "float", "required": True,
             "default": 0.0, "help": "Low voltage threshold (kV)."},
            {"name": "low_voltage_setpoint", "kind": "float", "required": True,
             "default": 0.0, "help": "Low voltage setpoint (kV)."},
            {"name": "high_voltage_threshold", "kind": "float", "required": True,
             "default": 0.0, "help": "High voltage threshold (kV)."},
            {"name": "high_voltage_setpoint", "kind": "float", "required": True,
             "default": 0.0, "help": "High voltage setpoint (kV)."},
        ],
    },
    "hvdcAngleDroopActivePowerControl": {
        "label": "HVDC AC emulation (angle droop)",
        "detail": "Active power set from an offset plus droop * (theta1 - theta2).",
        "index": "id",
        "targets": {"HVDC Lines": "get_hvdc_lines"},
        "fields": [
            {"name": "droop", "kind": "float", "required": True, "default": 0.0,
             "help": "Droop (MW/deg)."},
            {"name": "p0", "kind": "float", "required": True, "default": 0.0,
             "help": "Base active power offset (MW)."},
            {"name": "enabled", "kind": "bool", "required": True, "default": True,
             "help": "Whether AC emulation is active."},
        ],
    },
    "hvdcOperatorActivePowerRange": {
        "label": "HVDC operator active power range",
        "detail": "Operator-imposed power-flow bounds in each direction.",
        "index": "id",
        "targets": {"HVDC Lines": "get_hvdc_lines"},
        "fields": [
            {"name": "opr_from_cs1_to_cs2", "kind": "float", "required": True,
             "default": 0.0, "help": "Max flow from side 1 to side 2 (MW)."},
            {"name": "opr_from_cs2_to_cs1", "kind": "float", "required": True,
             "default": 0.0, "help": "Max flow from side 2 to side 1 (MW)."},
        ],
    },
    "entsoeCategory": {
        "label": "ENTSO-E category code (generator)",
        "detail": "Integer ENTSO-E category code.",
        "index": "id",
        "targets": {"Generators": "get_generators"},
        "fields": [
            {"name": "code", "kind": "int", "required": True, "default": 0,
             "help": "ENTSO-E category code."},
        ],
    },
}


def list_extensions_for_component(component: str) -> list[str]:
    """Return extension names whose ``targets`` mapping includes this component."""
    return [
        name for name, schema in CREATABLE_EXTENSIONS.items()
        if component in schema["targets"]
    ]


def list_extension_candidates(network, extension_name: str, component: str) -> list[str]:
    """Return ids of existing elements that can carry the selected extension."""
    schema = CREATABLE_EXTENSIONS.get(extension_name)
    if not schema:
        return []
    getter = schema["targets"].get(component)
    if not getter:
        return []
    try:
        df = getattr(network, getter)()
    except Exception:
        return []
    return sorted(df.index.tolist())


def validate_create_extension_fields(extension_name: str, fields: dict) -> list[str]:
    schema = CREATABLE_EXTENSIONS.get(extension_name)
    if not schema:
        return [f"Unknown extension: {extension_name!r}"]
    errors: list[str] = []
    for fdef in schema["fields"]:
        name = fdef["name"]
        if fdef.get("required") and fields.get(name) in (None, ""):
            errors.append(f"{name} is required.")
    if extension_name == "slackTerminal":
        bus = fields.get("bus_id") or ""
        elem = fields.get("element_id") or ""
        if bool(bus) == bool(elem):
            errors.append(
                "Exactly one of bus_id or element_id must be filled."
            )
    if extension_name == "busbarSectionPosition":
        for k in ("busbar_index", "section_index"):
            v = fields.get(k)
            if v is not None and int(v) < 0:
                errors.append(f"{k} must be >= 0.")
    if extension_name == "activePowerControl":
        mn = fields.get("min_target_p")
        mx = fields.get("max_target_p")
        if mn is not None and mx is not None and mx < mn:
            errors.append("max_target_p must be >= min_target_p.")
    return errors


def create_extension(
    network, extension_name: str, target_id: str, fields: dict
):
    """Attach a single extension row to an existing element.

    Validates against the registry, coerces types, drops optional columns when
    left blank, and routes the pypowsybl ``create_extensions`` call through
    the worker thread.
    """
    schema = CREATABLE_EXTENSIONS.get(extension_name)
    if not schema:
        raise ValueError(f"Unknown extension: {extension_name!r}")
    if not target_id:
        raise ValueError("Target id is required.")
    errors = validate_create_extension_fields(extension_name, fields)
    if errors:
        raise ValueError("; ".join(errors))

    row: dict[str, object] = {}
    for fdef in schema["fields"]:
        name = fdef["name"]
        raw_val = fields.get(name)
        if raw_val in (None, "") and fdef.get("optional_fill"):
            continue
        coercer = _EXT_KIND_COERCIONS.get(fdef["kind"])
        if fdef["kind"] == "choice":
            row[name] = str(raw_val)
        elif coercer is bool:
            row[name] = bool(raw_val)
        elif coercer is not None and raw_val not in (None, ""):
            row[name] = coercer(raw_val)
        else:
            row[name] = raw_val

    index_col = schema["index"]
    df = pd.DataFrame({k: [v] for k, v in row.items()},
                      index=pd.Index([target_id], name=index_col))

    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        raw.create_extensions(extension_name, df)

    run(_do_create)
    st.session_state.pop("_vl_lookup_cache", None)


# --- Secondary voltage control (network-level, two dataframes) ---
#
# Unlike the per-element extensions above, `secondaryVoltageControl` is
# defined on the whole network as a list of control zones plus a list of
# control units. pypowsybl takes two DataFrames:
#
#   zones  (index: name)       — target_v (kV), bus_ids (pilot points,
#                                space-separated if several)
#   units  (index: unit_id)    — zone_name, participate (bool)
#
# `network.create_extensions('secondaryVoltageControl', [zones, units])`
# *replaces* the whole SVC definition on write (no append). pypowsybl 1.14
# has no view adapter for reading it back via `get_extensions` — the data
# persists in the XIIDM export only.


def list_bus_ids(network) -> list[str]:
    """Return all bus ids in the bus view. Used to populate pilot-point pickers."""
    try:
        return sorted(network.get_buses().index.tolist())
    except Exception:
        return []


def list_unit_candidates(network) -> list[str]:
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
    zones: list[dict], units: list[dict]
) -> list[str]:
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
        elif float(z["target_v"]) <= 0:
            errors.append(f"Zone {name!r}: target_v must be > 0.")
        bus_ids = (z.get("bus_ids") or "").strip()
        if not bus_ids:
            errors.append(f"Zone {name!r}: at least one pilot bus id is required.")

    unit_ids: list[str] = []
    for ui, u in enumerate(units):
        uid = (u.get("unit_id") or "").strip()
        if not uid:
            errors.append(f"Unit #{ui + 1}: unit_id is required.")
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
    network, zones: list[dict], units: list[dict]
):
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
    st.session_state.pop("_vl_lookup_cache", None)


# --- Security Analysis ---

def build_n1_contingencies(
    network,
    element_type: str,
    nominal_v_set: set | None = None,
) -> list[dict]:
    """Build N-1 contingency definitions for every element of *element_type*.

    If *nominal_v_set* is provided, only include elements where at least one
    terminal voltage level has a nominal_v in the set.  Both the element table
    and the VL table are fetched in a single worker call.

    Returns list of {"id": "N1_<element_id>", "element_id": element_id}.
    """
    if element_type == "Lines":
        getter = "get_lines"
    elif element_type == "2-Winding Transformers":
        getter = "get_2_windings_transformers"
    else:
        return []

    vl_cols = ["voltage_level1_id", "voltage_level2_id"]
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        elem_df = getattr(raw, getter)(attributes=vl_cols)
        vl_df = raw.get_voltage_levels(attributes=["nominal_v"]) if nominal_v_set else None
        return elem_df, vl_df

    elem_df, vl_df = run(_gather)

    if elem_df.empty:
        return []

    if nominal_v_set and vl_df is not None and not vl_df.empty:
        def _matches(row):
            for col in vl_cols:
                vl_id = row.get(col)
                if vl_id and vl_id in vl_df.index:
                    if vl_df.at[vl_id, "nominal_v"] in nominal_v_set:
                        return True
            return False
        elem_df = elem_df[elem_df.apply(_matches, axis=1)]

    return [
        {"id": f"N1_{eid}", "element_id": eid, "element_ids": [eid]}
        for eid in elem_df.index
    ]


def build_n2_contingencies(
    network,
    element_type: str,
    nominal_v_set: set | None = None,
) -> list[dict]:
    """Build N-2 contingency definitions for every unique pair of elements
    of *element_type* whose terminals touch one of *nominal_v_set*.

    Pairs are unordered (A, B) with A < B by element id. Returns a list of
    ``{"id": "N2_<a>_<b>", "element_ids": [a, b]}``.
    """
    from itertools import combinations

    n1 = build_n1_contingencies(network, element_type, nominal_v_set)
    ids = sorted(c["element_id"] for c in n1)
    return [
        {"id": f"N2_{a}_{b}", "element_ids": [a, b]}
        for a, b in combinations(ids, 2)
    ]


def _apply_action(analysis, action: dict) -> None:
    """Dispatch a single action dict to the right pypowsybl ``add_*_action`` call.

    Supported action types (extend here to add more):

    - ``SWITCH``: ``switch_id``, ``open``
    - ``TERMINALS_CONNECTION``: ``element_id``, ``opening``, optional ``side``
    - ``GENERATOR_ACTIVE_POWER``: ``generator_id``, ``is_relative``, ``active_power``
    - ``LOAD_ACTIVE_POWER``: ``load_id``, ``is_relative``, ``active_power``
    - ``PHASE_TAP_CHANGER_POSITION``: ``transformer_id``, ``is_relative``, ``tap_position``,
      optional ``side``
    - ``RATIO_TAP_CHANGER_POSITION``: ``transformer_id``, ``is_relative``, ``tap_position``,
      optional ``side``
    - ``SHUNT_COMPENSATOR_POSITION``: ``shunt_id``, ``section``
    """
    from pypowsybl._pypowsybl import Side

    action_id = action["action_id"]
    atype = action["type"]
    side = Side.__members__.get(action.get("side", "NONE"), Side.NONE)

    if atype == "SWITCH":
        analysis.add_switch_action(
            action_id, action["switch_id"], bool(action["open"])
        )
    elif atype == "TERMINALS_CONNECTION":
        analysis.add_terminals_connection_action(
            action_id,
            action["element_id"],
            side=side,
            opening=bool(action.get("opening", True)),
        )
    elif atype == "GENERATOR_ACTIVE_POWER":
        analysis.add_generator_active_power_action(
            action_id,
            action["generator_id"],
            bool(action["is_relative"]),
            float(action["active_power"]),
        )
    elif atype == "LOAD_ACTIVE_POWER":
        analysis.add_load_active_power_action(
            action_id,
            action["load_id"],
            bool(action["is_relative"]),
            float(action["active_power"]),
        )
    elif atype == "PHASE_TAP_CHANGER_POSITION":
        analysis.add_phase_tap_changer_position_action(
            action_id,
            action["transformer_id"],
            bool(action["is_relative"]),
            int(action["tap_position"]),
            side=side,
        )
    elif atype == "RATIO_TAP_CHANGER_POSITION":
        analysis.add_ratio_tap_changer_position_action(
            action_id,
            action["transformer_id"],
            bool(action["is_relative"]),
            int(action["tap_position"]),
            side=side,
        )
    elif atype == "SHUNT_COMPENSATOR_POSITION":
        analysis.add_shunt_compensator_position_action(
            action_id,
            action["shunt_id"],
            int(action["section"]),
        )
    else:
        raise ValueError(f"Unsupported action type: {atype!r}")


def run_security_analysis(
    network,
    contingencies: list[dict],
    monitored_elements: list[dict] | None = None,
    limit_reductions: list[dict] | None = None,
    actions: list[dict] | None = None,
    operator_strategies: list[dict] | None = None,
) -> dict:
    """Run AC security analysis on the worker thread.

    *contingencies* is a list of {"id": str, "element_id": str} dicts produced
    by :func:`build_n1_contingencies` (or any compatible builder).

    *monitored_elements* is an optional list of dicts, one per
    ``add_monitored_elements`` call:
        {
            "contingency_context_type": "ALL" | "NONE" | "SPECIFIC",
            "contingency_ids": list[str] | None,
            "branch_ids": list[str] | None,
            "voltage_level_ids": list[str] | None,
            "three_windings_transformer_ids": list[str] | None,
        }

    *limit_reductions* is an optional list of dicts, one per reduction entry:
        {
            "limit_type": "CURRENT",
            "permanent": bool,
            "temporary": bool,
            "value": float,
            "contingency_context": "ALL" | "NONE" | "SPECIFIC" (optional),
            "min_temporary_duration": int (optional),
            "max_temporary_duration": int (optional),
            "country": str (optional),
            "min_voltage": float (optional),
            "max_voltage": float (optional),
        }

    *actions* is an optional list of remedial-action dicts dispatched by
    :func:`_apply_action` — see that function for the per-type shape.

    *operator_strategies* is an optional list of dicts, one per
    ``add_operator_strategy`` call:
        {
            "operator_strategy_id": str,
            "contingency_id": str,
            "action_ids": list[str],
            "condition_type": "TRUE_CONDITION" | "ANY_VIOLATION_CONDITION"
                              | "ALL_VIOLATION_CONDITION"
                              | "AT_LEAST_ONE_VIOLATION_CONDITION",
        }

    Returns a serialized dict safe for ``st.session_state``:
        {
            "pre_status":    str,
            "pre_violations": DataFrame,
            "pre_branch_results": DataFrame,
            "pre_bus_results": DataFrame,
            "pre_3wt_results": DataFrame,
            "post": {contingency_id: {
                "status": str,
                "limit_violations": DataFrame,
                "branch_results": DataFrame,
                "bus_results": DataFrame,
                "three_windings_transformer_results": DataFrame,
            }},
            "operator_strategies": {strategy_id: {
                "status": str,
                "limit_violations": DataFrame,
                "branch_results": DataFrame,
                "bus_results": DataFrame,
                "three_windings_transformer_results": DataFrame,
                "contingency_id": str,
                "action_ids": list[str],
            }},
            "contingencies": list[dict],
        }
    """
    from iidm_viewer.lf_parameters import get_lf_parameters

    raw = object.__getattribute__(network, "_obj")
    generic, provider = get_lf_parameters()
    monitored_elements = monitored_elements or []
    limit_reductions = limit_reductions or []
    actions = actions or []
    operator_strategies = operator_strategies or []

    def _run_sa():
        import pypowsybl.security as sa
        import pypowsybl.loadflow as lf
        from pypowsybl.flowdecomposition import ContingencyContextType
        from pypowsybl._pypowsybl import ConditionType, ViolationType

        analysis = sa.create_analysis()
        for c in contingencies:
            eids = list(c.get("element_ids") or ([c["element_id"]] if "element_id" in c else []))
            if len(eids) == 1:
                analysis.add_single_element_contingency(eids[0], c["id"])
            elif len(eids) > 1:
                analysis.add_multiple_elements_contingency(eids, c["id"])

        for me in monitored_elements:
            ctx_name = me.get("contingency_context_type", "ALL")
            ctx = ContingencyContextType.__members__.get(ctx_name, ContingencyContextType.ALL)
            analysis.add_monitored_elements(
                contingency_context_type=ctx,
                contingency_ids=me.get("contingency_ids") or None,
                branch_ids=me.get("branch_ids") or None,
                voltage_level_ids=me.get("voltage_level_ids") or None,
                three_windings_transformer_ids=me.get("three_windings_transformer_ids") or None,
            )

        if limit_reductions:
            # ``limit_type`` is the index column in pypowsybl's metadata; the
            # C bridge rejects it as a regular column.
            lr_df = pd.DataFrame(limit_reductions).set_index("limit_type")
            analysis.add_limit_reductions(lr_df)

        for action in actions:
            _apply_action(analysis, action)

        for strat in operator_strategies:
            cond_name = strat.get("condition_type", "TRUE_CONDITION")
            cond = ConditionType.__members__.get(cond_name, ConditionType.TRUE_CONDITION)
            vtype_names = strat.get("violation_types") or []
            vtypes = [
                ViolationType.__members__[n]
                for n in vtype_names
                if n in ViolationType.__members__
            ] or None
            vsubjects = list(strat.get("violation_subject_ids") or []) or None
            analysis.add_operator_strategy(
                strat["operator_strategy_id"],
                strat["contingency_id"],
                list(strat["action_ids"]),
                condition_type=cond,
                violation_subject_ids=vsubjects,
                violation_types=vtypes,
            )

        lf_params = lf.Parameters(**generic)
        if provider:
            lf_params.provider_parameters = {k: str(v) for k, v in provider.items()}
        params = sa.Parameters(load_flow_parameters=lf_params)

        result = analysis.run_ac(raw, parameters=params)

        # Serialize all results before they leave the worker thread
        pre_result = result.pre_contingency_result
        pre_viol = pd.DataFrame(pre_result.limit_violations)

        def _select(
            df: pd.DataFrame,
            contingency_id: str | None,
            strategy_id: str = "",
        ) -> pd.DataFrame:
            """Slice a multi-indexed result DF by (contingency_id, operator_strategy_id).

            Index levels are ``(contingency_id, operator_strategy_id, element_id)``.
            ``""`` means "no contingency" for level 0 and "no strategy" for level 1.
            Returns an empty DataFrame if *df* is empty or the keys are absent.
            """
            if df is None or df.empty:
                return pd.DataFrame()
            try:
                if isinstance(df.index, pd.MultiIndex):
                    cid_key = "" if contingency_id is None else contingency_id
                    lvl0 = df.index.get_level_values(0)
                    mask = lvl0 == cid_key
                    if df.index.nlevels >= 3:
                        lvl1 = df.index.get_level_values(1)
                        mask = mask & (lvl1 == strategy_id)
                        return df[mask].reset_index(level=[0, 1], drop=True)
                    return df[mask].reset_index(level=0, drop=True)
                return df.copy()
            except Exception:
                return pd.DataFrame()

        branch_all = pd.DataFrame(result.branch_results) if result.branch_results is not None else pd.DataFrame()
        bus_all = pd.DataFrame(result.bus_results) if result.bus_results is not None else pd.DataFrame()
        t3w_all = (
            pd.DataFrame(result.three_windings_transformer_results)
            if result.three_windings_transformer_results is not None
            else pd.DataFrame()
        )

        post: dict = {}
        for cid, cr in result.post_contingency_results.items():
            post[cid] = {
                "status": cr.status.name,
                "limit_violations": pd.DataFrame(cr.limit_violations),
                "branch_results": _select(branch_all, cid),
                "bus_results": _select(bus_all, cid),
                "three_windings_transformer_results": _select(t3w_all, cid),
            }

        os_results: dict = {}
        for sid, osr in result.operator_strategy_results.items():
            strat = next(
                (s for s in operator_strategies if s["operator_strategy_id"] == sid),
                None,
            )
            cid = strat["contingency_id"] if strat else None
            os_results[sid] = {
                "status": osr.status.name,
                "limit_violations": pd.DataFrame(osr.limit_violations),
                "branch_results": _select(branch_all, cid, sid),
                "bus_results": _select(bus_all, cid, sid),
                "three_windings_transformer_results": _select(t3w_all, cid, sid),
                "contingency_id": cid,
                "action_ids": list(strat["action_ids"]) if strat else [],
            }

        return {
            "pre_status": pre_result.status.name,
            "pre_violations": pre_viol,
            "pre_branch_results": _select(branch_all, None),
            "pre_bus_results": _select(bus_all, None),
            "pre_3wt_results": _select(t3w_all, None),
            "post": post,
            "operator_strategies": os_results,
            "contingencies": contingencies,
        }

    return run(_run_sa)


# --- Short Circuit Analysis ---

def build_bus_faults(
    network,
    nominal_v_set: set | None = None,
    fault_type: str = "THREE_PHASE",
) -> list[dict]:
    """Build a bus-fault definition for every bus, optionally filtered by nominal voltage.

    Both the bus table and the VL table are fetched in a single worker call.

    Returns list of {"id": "SC_<bus_id>", "element_id": bus_id, "fault_type": fault_type}.
    """
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        buses = raw.get_buses(attributes=["voltage_level_id"])
        vl_df = raw.get_voltage_levels(attributes=["nominal_v"]) if nominal_v_set else None
        return buses, vl_df

    buses, vl_df = run(_gather)

    if buses.empty:
        return []

    if nominal_v_set and vl_df is not None and not vl_df.empty:
        def _matches(row):
            vl_id = row.get("voltage_level_id")
            if vl_id and vl_id in vl_df.index:
                return vl_df.at[vl_id, "nominal_v"] in nominal_v_set
            return False
        buses = buses[buses.apply(_matches, axis=1)]

    return [
        {"id": f"SC_{bid}", "element_id": bid, "fault_type": fault_type}
        for bid in buses.index
    ]


def run_short_circuit_analysis(network, faults: list[dict], sc_params: dict | None = None) -> dict:
    """Run short circuit analysis on the worker thread.

    *faults* is a list of {"id": str, "element_id": bus_id, "fault_type": str} dicts
    produced by :func:`build_bus_faults` (or any compatible builder).

    *sc_params* is a plain dict of scalar options read from the main thread:
        {
            "study_type": "SUB_TRANSIENT" | "TRANSIENT",
            "with_feeder_result": bool,
            "with_limit_violations": bool,
            "min_voltage_drop_proportional_threshold": float,
        }

    Returns a serialized dict safe for ``st.session_state``:
        {
            "fault_results": {fault_id: {
                "status": str,
                "short_circuit_power_mva": float | None,
                "current_kA": float | None,
                "feeder_results": DataFrame,
                "limit_violations": DataFrame,
            }},
            "faults": list[dict],
        }
    """
    raw = object.__getattribute__(network, "_obj")
    sc_params = sc_params or {}

    def _run_sc():
        import pypowsybl.shortcircuit as sc

        analysis = sc.create_analysis()
        for f in faults:
            analysis.set_bus_fault(f["id"], f["element_id"], 0.0, 0.0)

        params = sc.Parameters(
            study_type=sc.ShortCircuitStudyType[sc_params.get("study_type", "SUB_TRANSIENT")],
            with_feeder_result=sc_params.get("with_feeder_result", True),
            with_limit_violations=sc_params.get("with_limit_violations", True),
            min_voltage_drop_proportional_threshold=float(
                sc_params.get("min_voltage_drop_proportional_threshold", 0.0)
            ),
        )

        result = analysis.run(raw, parameters=params)

        # Serialize all results before they leave the worker thread
        fr_df = result.fault_results          # DataFrame indexed by fault_id
        feeder_df_all = result.feeder_results  # flat DataFrame, may be multi-indexed
        viol_df_all = result.limit_violations  # flat DataFrame, may be multi-indexed

        def _filter_by_fault(df: pd.DataFrame, fid: str) -> pd.DataFrame:
            if df.empty:
                return pd.DataFrame()
            try:
                if isinstance(df.index, pd.MultiIndex):
                    lvl_vals = df.index.get_level_values(0)
                    return df[lvl_vals == fid].reset_index(drop=True)
                return df[df.index == fid].reset_index(drop=True)
            except Exception:
                return pd.DataFrame()

        fault_results: dict = {}
        for f in faults:
            fid = f["id"]
            if fid in fr_df.index:
                row = fr_df.loc[fid]
                status_val = row.get("status", "UNKNOWN")
                status_str = status_val.name if hasattr(status_val, "name") else str(status_val)
                pwr_raw = row.get("short_circuit_power", None)
                pwr = float(pwr_raw) if pwr_raw is not None and pd.notna(pwr_raw) else None
                cur_raw = row.get("current", None)
                cur_a = float(cur_raw) if cur_raw is not None and pd.notna(cur_raw) else None
                cur_ka = cur_a / 1000.0 if cur_a is not None else None
            else:
                status_str = "UNKNOWN"
                pwr = None
                cur_ka = None

            fault_results[fid] = {
                "status": status_str,
                "short_circuit_power_mva": pwr,
                "current_kA": cur_ka,
                "feeder_results": _filter_by_fault(feeder_df_all, fid),
                "limit_violations": _filter_by_fault(viol_df_all, fid),
            }

        return {
            "fault_results": fault_results,
            "faults": faults,
        }

    return run(_run_sc)
