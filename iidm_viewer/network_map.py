"""Geographical network map tab.

Reuses ``NetworkMapWidget.extract_map_data`` from pypowsybl-jupyter to
extract substations, positions, lines, and line positions from the
pypowsybl network — the same extraction the Jupyter widget uses.

The frontend (``frontend/map_component/``) consumes them via
``render_interactive_map`` and draws the map with MapLibre + deck.gl.
"""
from __future__ import annotations

import streamlit as st

from iidm_viewer.map_component import render_interactive_map
from iidm_viewer.powsybl_worker import run


def _extract_map_data(network):
    """Extract map data by delegating to pypowsybl-jupyter's extraction.

    Runs on the pypowsybl worker thread via ``run(...)``.
    """
    raw = object.__getattribute__(network, "_obj")

    def _extract():
        from pypowsybl_jupyter.networkmapwidget import NetworkMapWidget

        # extract_map_data only uses `self` for stateless helpers,
        # so we can call it directly on a throwaway subclass instance
        # that skips the widget __init__.
        class _Extractor(NetworkMapWidget):
            def __init__(self):  # skip widget init entirely
                pass
            def __del__(self):   # suppress ipywidgets cleanup noise
                pass

        extractor = _Extractor()

        (lmap, lpos, smap, spos, _vl_subs, _sub_vls, _subs_ids, tlmap, hlmap) = (
            extractor.extract_map_data(raw, display_lines=True, use_line_geodata=False)
        )

        if not spos:
            return None

        # Include tie lines and HVDC lines, matching the pypowsybl widget.
        all_lines = lmap + tlmap + hlmap

        return smap, spos, all_lines, lpos

    return run(_extract)


_MISSING = object()  # sentinel: key absent from session state (distinct from None)


def _get_cached_map_data(network):
    """Cache extraction in session state so reruns don't reprocess."""
    cache = st.session_state.get("_map_data_cache", _MISSING)
    if cache is not _MISSING:
        return cache  # may be None when the network has no geo data
    result = _extract_map_data(network)
    st.session_state["_map_data_cache"] = result
    # Bump the version so the JS map component knows to rebuild layers.
    st.session_state["_map_data_version"] = st.session_state.get("_map_data_version", 0) + 1
    return result


def render_network_map(network, selected_vl):
    del selected_vl  # reserved for future highlight support
    data = _get_cached_map_data(network)

    if data is None:
        st.info(
            "No geographical data found in this network. "
            "The network needs a 'substationPosition' extension with "
            "latitude/longitude coordinates."
        )
        return

    substations, substation_positions, lines, line_positions = data

    if not substation_positions:
        st.info(
            "The 'substationPosition' extension is present but contained no "
            "valid coordinates."
        )
        return

    version = st.session_state.get("_map_data_version", 0)

    if st.session_state.get("_map_last_sent_version") == version:
        # Data unchanged — send empty arrays so Streamlit doesn't serialize
        # and transfer the full network payload on every navigation rerun.
        # The JS component skips layer rebuilds via the version check.
        render_interactive_map(
            substations=[],
            substation_positions=[],
            lines=[],
            line_positions=[],
            version=version,
            height=670,
            key="network_map",
        )
    else:
        st.session_state["_map_last_sent_version"] = version
        render_interactive_map(
            substations=substations,
            substation_positions=substation_positions,
            lines=lines,
            line_positions=line_positions,
            version=version,
            height=670,
            key="network_map",
        )

    line_pos_count = len(line_positions)
    caption = f"{len(substations)} substations, {len(lines)} branches"
    if line_pos_count:
        caption += f", {line_pos_count} lines with detailed geometry"
    st.caption(caption)
