"""Streamlit "Network Reduction" dialog.

The three reduction modes (voltage-range, IDs, IDs+depths), their
validators, and the worker-routed pypowsybl calls live in
:mod:`iidm_viewer.network_reduction_actions` so the PySide6 + NiceGUI
prototypes share them. This file holds only the Streamlit widgets +
the session-state housekeeping the existing app expects.
"""
from __future__ import annotations

import streamlit as st

from iidm_viewer.network_reduction_actions import (
    REDUCTION_METHODS,
    list_voltage_level_ids,
    reduce_by_ids,
    reduce_by_ids_and_depths,
    reduce_by_voltage_range,
)

from .caches import invalidate_on_network_replace
from .state import get_network


def _clear_caches():
    invalidate_on_network_replace()
    st.session_state["selected_vl"] = None
    st.session_state["vl_selector_gen"] = st.session_state.get("vl_selector_gen", 0) + 1
    for key in (
        "_export_bytes",
        "_export_fmt",
        "_export_ext",
        "_lf_report_json",
        "_vl_set_by_click",
    ):
        st.session_state.pop(key, None)
    for k in list(st.session_state.keys()):
        if (
            k.startswith("_change_log_")
            or k.startswith("_removal_log_")
            or k.startswith("_ext_change_log_")
            or k.startswith("_ext_removal_log_")
            or k.startswith("_export_cache_")
        ):
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
        options=REDUCTION_METHODS,
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
            try:
                with st.spinner("Reducing network..."):
                    reduce_by_voltage_range(
                        network, v_min, v_max,
                        with_boundary_lines=with_boundary_lines,
                    )
            except Exception as exc:
                st.error(f"Reduction failed: {exc}")
                return
            _clear_caches()
            st.rerun()

    elif method == "By Voltage Level IDs":
        st.caption("Keep only the specified voltage levels and all elements between them.")
        vl_ids = list_voltage_level_ids(network)
        selected = st.multiselect(
            "Voltage levels to keep",
            options=vl_ids,
            key="nr_ids_selected",
        )

        if st.button("Apply Reduction", type="primary", key="nr_apply_ids"):
            try:
                with st.spinner("Reducing network..."):
                    reduce_by_ids(
                        network, selected,
                        with_boundary_lines=with_boundary_lines,
                    )
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
        vl_ids = list_voltage_level_ids(network)
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
            try:
                with st.spinner("Reducing network..."):
                    reduce_by_ids_and_depths(
                        network, selected, depth,
                        with_boundary_lines=with_boundary_lines,
                    )
            except Exception as exc:
                st.error(f"Reduction failed: {exc}")
                return
            _clear_caches()
            st.rerun()
