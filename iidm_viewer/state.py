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
