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

CREATABLE_COMPONENTS: dict[str, dict] = {
    "Generators": {
        "bay_function": "create_generator_bay",
        "required": [
            "id",
            "bus_or_busbar_section_id",
            "min_p",
            "max_p",
            "target_p",
            "voltage_regulator_on",
            "position_order",
        ],
        "optional": [
            "energy_source",
            "target_q",
            "target_v",
            "rated_s",
            "direction",
        ],
    },
}


def list_node_breaker_voltage_levels(network):
    """Return node-breaker voltage levels as a DataFrame with id/display/nominal_v."""
    vls = network.get_voltage_levels(all_attributes=True)
    if "topology_kind" not in vls.columns:
        return pd.DataFrame(columns=["id", "display", "nominal_v"])
    nb = vls[vls["topology_kind"] == "NODE_BREAKER"].reset_index()
    if nb.empty:
        return pd.DataFrame(columns=["id", "display", "nominal_v"])
    nb["display"] = nb.apply(lambda r: r["name"] if r["name"] else r["id"], axis=1)
    return nb[["id", "display", "nominal_v"]].sort_values("display")


def list_busbar_sections(network, voltage_level_id: str):
    """Return a sorted list of busbar section ids in the given voltage level."""
    bbs = network.get_busbar_sections()
    if bbs.empty:
        return []
    return sorted(bbs[bbs["voltage_level_id"] == voltage_level_id].index.tolist())


def create_component_bay(network, component: str, fields: dict):
    """Create a new injection on a busbar section via a clean feeder bay.

    Routes through pypowsybl's ``create_*_bay`` helper which, in node-breaker
    voltage levels, allocates nodes and inserts a closed disconnector plus a
    breaker between the busbar section and the new injection. Callers supply
    the busbar id and the injection attributes; node numbering stays internal.
    """
    if component not in CREATABLE_COMPONENTS:
        raise ValueError(f"{component!r} is not creatable")
    spec = CREATABLE_COMPONENTS[component]
    missing = [
        f for f in spec["required"]
        if fields.get(f) is None or fields.get(f) == ""
    ]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    row = {k: v for k, v in fields.items() if v is not None and v != ""}
    df = pd.DataFrame([row]).set_index("id")
    bay_fn_name = spec["bay_function"]
    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        import pypowsybl.network as pn
        fn = getattr(pn, bay_fn_name)
        fn(raw, df)

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
