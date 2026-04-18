import streamlit as st

from .state import get_network


def _get_voltage_level_ids(network):
    df = network.get_voltage_levels()
    return df.index.tolist()


def _clear_caches():
    st.session_state.pop("_map_data_cache", None)
    st.session_state.pop("_vl_lookup_cache", None)
    st.session_state.pop("_export_bytes", None)
    st.session_state.pop("_export_fmt", None)
    st.session_state["selected_vl"] = None
    for k in list(st.session_state.keys()):
        if k.startswith("_change_log_") or k.startswith("_removal_log_"):
            del st.session_state[k]


@st.dialog("Network Reduction", width="large")
def show_network_reduction_dialog():
    network = get_network()
    if network is None:
        st.warning("No network loaded.")
        return

    st.error(
        "**Irreversible operation.** The network will be permanently modified. "
        "To recover the original, reload the file.",
        icon="⚠️",
    )

    method = st.radio(
        "Reduction method",
        options=[
            "By Voltage Range",
            "By Voltage Level IDs",
            "By Voltage Level IDs and Depths",
        ],
        horizontal=True,
        key="nr_method",
    )

    with_boundary_lines = st.checkbox(
        "Replace cut lines with boundary lines",
        value=False,
        help="Lines cut at the reduction boundary are replaced by boundary lines.",
        key="nr_boundary_lines",
    )

    st.divider()

    if method == "By Voltage Range":
        st.caption("Keep all elements whose nominal voltage is within the specified range (kV).")
        col1, col2 = st.columns(2)
        with col1:
            v_min = st.number_input("Minimum voltage (kV)", min_value=0.0, value=0.0, step=1.0, key="nr_v_min")
        with col2:
            v_max = st.number_input("Maximum voltage (kV)", min_value=0.0, value=9999.0, step=1.0, key="nr_v_max")

        if st.button("Apply Reduction", type="primary", key="nr_apply_range"):
            if v_min >= v_max:
                st.error("Minimum voltage must be less than maximum voltage.")
                return
            try:
                with st.spinner("Reducing network..."):
                    network.reduce_by_voltage_range(
                        v_min=v_min, v_max=v_max, with_boundary_lines=with_boundary_lines
                    )
            except Exception as exc:
                st.error(f"Reduction failed: {exc}")
                return
            _clear_caches()
            st.rerun()

    elif method == "By Voltage Level IDs":
        st.caption("Keep only the specified voltage levels and all elements between them.")
        vl_ids = _get_voltage_level_ids(network)
        selected = st.multiselect(
            "Voltage levels to keep",
            options=vl_ids,
            key="nr_ids_selected",
        )

        if st.button("Apply Reduction", type="primary", key="nr_apply_ids"):
            if not selected:
                st.error("Select at least one voltage level.")
                return
            try:
                with st.spinner("Reducing network..."):
                    network.reduce_by_ids(ids=selected, with_boundary_lines=with_boundary_lines)
            except Exception as exc:
                st.error(f"Reduction failed: {exc}")
                return
            _clear_caches()
            st.rerun()

    else:  # By Voltage Level IDs and Depths
        st.caption(
            "Keep the specified voltage levels and their neighbours up to the given depth. "
            "Each entry specifies a voltage level and how many hops away to keep."
        )
        vl_ids = _get_voltage_level_ids(network)
        selected = st.multiselect(
            "Voltage levels",
            options=vl_ids,
            key="nr_depths_vls",
        )
        depth = st.number_input(
            "Depth (applied to all selected voltage levels)",
            min_value=0,
            max_value=100,
            value=1,
            step=1,
            key="nr_depths_depth",
        )

        if st.button("Apply Reduction", type="primary", key="nr_apply_depths"):
            if not selected:
                st.error("Select at least one voltage level.")
                return
            vl_depths = [(vl, int(depth)) for vl in selected]
            try:
                with st.spinner("Reducing network..."):
                    network.reduce_by_ids_and_depths(
                        vl_depths=vl_depths, with_boundary_lines=with_boundary_lines
                    )
            except Exception as exc:
                st.error(f"Reduction failed: {exc}")
                return
            _clear_caches()
            st.rerun()
