"""NAD and SLD tab rendering."""
import math
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from iidm_viewer.diagrams import (
    _BUS_LEGEND_PALETTE,
    _format_float,
    _parse_sld_busbar_indices,
    _parse_sld_palette,
    _resolve_bus_colors,
)
from iidm_viewer.state import load_network


def test_format_float_handles_none_nan_and_values():
    assert _format_float(1.2345, ".2f") == "1.23"
    assert _format_float(None, ".2f") == "—"
    assert _format_float(float("nan"), ".2f") == "—"
    assert _format_float("not a number", ".2f") == "—"
    # Int input still formats cleanly.
    assert _format_float(42, ".1f") == "42.0"


def test_bus_legend_palette_is_deterministic_and_nonempty():
    # Palette must have at least one color — `i % len(palette)` would
    # raise ZeroDivisionError otherwise, breaking the SLD tab.
    assert len(_BUS_LEGEND_PALETTE) > 0
    # Hex-color sanity so a future palette swap can't ship a typo.
    for c in _BUS_LEGEND_PALETTE:
        assert c.startswith("#") and len(c) == 7
        int(c[1:], 16)  # parses as hex


_SLD_SVG_STUB = """
<svg>
<style><![CDATA[
.sld-vl120to180.sld-bus-0 {--sld-vl-color: #00AFAE}
.sld-vl120to180.sld-bus-1 {--sld-vl-color: #000D58}
.sld-vl300to500.sld-bus-0 {--sld-vl-color: #FF0000}
]]></style>
<g class="sld-busbar-section sld-vl120to180 sld-bus-0" id="idB4" transform="x">
</g>
<g class="sld-busbar-section sld-vl120to180 sld-bus-1" id="idB4b" transform="x">
</g>
</svg>
"""


def test_parse_sld_palette_extracts_band_index_colors():
    palette = _parse_sld_palette(_SLD_SVG_STUB)
    assert palette[("vl120to180", 0)] == "#00AFAE"
    assert palette[("vl120to180", 1)] == "#000D58"
    assert palette[("vl300to500", 0)] == "#FF0000"


def test_parse_sld_busbar_indices_matches_busbar_group_classes():
    indices = _parse_sld_busbar_indices(_SLD_SVG_STUB)
    # Pypowsybl strips the leading "id" prefix when emitting equipmentId in
    # the metadata, so our parser must match what's after `id="id…"`.
    assert indices == {"B4": ("vl120to180", 0), "B4b": ("vl120to180", 1)}


def test_parse_sld_palette_handles_missing_style_block():
    assert _parse_sld_palette("") == {}
    assert _parse_sld_palette("<svg></svg>") == {}


def test_resolve_bus_colors_empty_svg_returns_empty():
    # Covers the NAD-only / no-SLD-rendered case where the legend renders
    # before a real SVG is available.
    class _Stub:
        def get_busbar_sections(self, **_):
            raise AssertionError("should not be called when SVG is empty")

    assert _resolve_bus_colors(_Stub(), "VL4", "") == {}


def _prepare(xiidm_upload, selected_vl=None):
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = load_network(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    if selected_vl is not None:
        at.session_state["selected_vl"] = selected_vl
    at.run(timeout=30)
    return at


def test_nad_tab_info_when_no_vl_selected(xiidm_upload):
    """Empty VL filter -> vl_selector returns None -> NAD tab shows its info."""
    at = _prepare(xiidm_upload)
    at.session_state["active_tab_sync"] = 2  # Network Area Diagram
    at.text_input(key="vl_filter_text_0").set_value("ZZZZZZ").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any("Network Area Diagram" in i for i in infos)


def test_sld_tab_info_when_no_vl_selected(xiidm_upload):
    at = _prepare(xiidm_upload)
    at.session_state["active_tab_sync"] = 3  # Single Line Diagram
    at.text_input(key="vl_filter_text_0").set_value("ZZZZZZ").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any("Single Line Diagram" in i for i in infos)


def test_nad_and_sld_render_without_exception_for_valid_vl(xiidm_upload):
    at = _prepare(xiidm_upload, selected_vl="VL1")
    assert not at.exception


# ---------------------------------------------------------------------------
# sld-breaker-click handler
# ---------------------------------------------------------------------------

def test_breaker_click_writes_to_change_log(node_breaker_network):
    """Simulating a sld-breaker-click updates the switch and logs the change."""
    sw = node_breaker_network.get_switches(all_attributes=True)
    sw_id = sw.index[0]
    original_open = bool(sw.loc[sw_id, "open"])

    st.session_state.pop("_change_log_get_switches", None)
    st.session_state.pop("_last_breaker_click_ts", None)

    fake_click = {"type": "sld-breaker-click", "breakerId": sw_id,
                  "open": not original_open, "ts": 12345}

    with patch("iidm_viewer.diagrams.render_interactive_sld", return_value=fake_click), \
         patch("iidm_viewer.diagrams._render_bus_legend"):
        from iidm_viewer.diagrams import render_sld_tab
        try:
            render_sld_tab(node_breaker_network, "S1VL1")
        except Exception:
            pass  # st.rerun() raises in test context

    log = st.session_state.get("_change_log_get_switches", [])
    assert len(log) == 1
    assert log[0]["element_id"] == sw_id
    assert log[0]["property"] == "open"
    assert log[0]["before"] == original_open
    assert log[0]["after"] == (not original_open)


def test_breaker_click_deduplication_skips_same_ts(node_breaker_network):
    """Second render with same ts must not re-toggle the switch."""
    sw = node_breaker_network.get_switches(all_attributes=True)
    sw_id = sw.index[0]
    original_open = bool(sw.loc[sw_id, "open"])

    st.session_state.pop("_change_log_get_switches", None)
    # Pre-set the ts so the handler thinks this event was already processed
    st.session_state["_last_breaker_click_ts"] = 99999

    fake_click = {"type": "sld-breaker-click", "breakerId": sw_id,
                  "open": not original_open, "ts": 99999}

    with patch("iidm_viewer.diagrams.render_interactive_sld", return_value=fake_click), \
         patch("iidm_viewer.diagrams._render_bus_legend"):
        from iidm_viewer.diagrams import render_sld_tab
        try:
            render_sld_tab(node_breaker_network, "S1VL1")
        except Exception:
            pass

    # Log must be empty — duplicate ts was skipped
    assert st.session_state.get("_change_log_get_switches", []) == []
    # Switch state must be unchanged
    sw2 = node_breaker_network.get_switches(all_attributes=True)
    assert bool(sw2.loc[sw_id, "open"]) == original_open
