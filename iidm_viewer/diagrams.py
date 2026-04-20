import html
import math

import streamlit as st
from iidm_viewer.nad_component import render_interactive_nad
from iidm_viewer.sld_component import render_interactive_sld


# Fixed palette for the bus-voltage legend rendered beneath the SLD.
# Colors are deterministic by bus order within the voltage level so
# repeat renders of the same VL produce the same legend. They do NOT
# attempt to match the bus colors inside the pypowsybl SLD SVG — a
# frontend-side legend that reads the real SVG is Option B in
# docs/future-interactive-viewer.md.
_BUS_LEGEND_PALETTE = (
    "#228b22",  # forest green
    "#4169e1",  # royal blue
    "#ff8c00",  # dark orange
    "#dc143c",  # crimson
    "#9932cc",  # dark orchid
    "#20b2aa",  # light sea green
    "#b8860b",  # dark goldenrod
    "#4b0082",  # indigo
)


def _format_float(val, fmt: str) -> str:
    if val is None:
        return "—"
    try:
        f = float(val)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(f):
        return "—"
    return format(f, fmt)


def _render_bus_legend(network, selected_vl: str) -> None:
    """Show one row per bus in `selected_vl` — colored dot, bus id, V (kV), angle (°).

    Voltages come from `network.get_buses(all_attributes=True)`; `v_mag`
    and `v_angle` are NaN until a load flow has been run, in which case
    we show em-dashes. Colors come from a fixed palette indexed by bus
    order within the VL; they do not match the SLD SVG's internal bus
    coloring — see Option B in
    docs/future-interactive-viewer.md if exact matching is ever needed.
    """
    try:
        buses = network.get_buses(all_attributes=True).reset_index()
    except Exception:
        return
    if buses.empty:
        return

    buses["voltage_level_id"] = buses["voltage_level_id"].astype(str)
    vl_buses = buses[buses["voltage_level_id"] == str(selected_vl)]
    if vl_buses.empty:
        return

    rows_html = []
    for i, (_, row) in enumerate(vl_buses.iterrows()):
        color = _BUS_LEGEND_PALETTE[i % len(_BUS_LEGEND_PALETTE)]
        bus_id = html.escape(str(row.get("id", "")))
        v_mag = _format_float(row.get("v_mag"), ".2f")
        v_angle = _format_float(row.get("v_angle"), ".2f")
        v_label = f"{v_mag} kV" if v_mag != "—" else "—"
        a_label = f"{v_angle}°" if v_angle != "—" else "—"
        rows_html.append(
            f'<tr>'
            f'<td style="padding:2px 8px;"><span style="display:inline-block;'
            f'width:12px;height:12px;border-radius:50%;background:{color};'
            f'vertical-align:middle;"></span></td>'
            f'<td style="padding:2px 8px;font-family:monospace;">{bus_id}</td>'
            f'<td style="padding:2px 8px;text-align:right;">{v_label}</td>'
            f'<td style="padding:2px 8px;text-align:right;">{a_label}</td>'
            f'</tr>'
        )

    st.markdown(
        "<table style='border-collapse:collapse;margin-top:8px;'>"
        "<thead><tr>"
        "<th></th>"
        "<th style='padding:2px 8px;text-align:left;'>Bus</th>"
        "<th style='padding:2px 8px;text-align:right;'>V</th>"
        "<th style='padding:2px 8px;text-align:right;'>Angle</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table>",
        unsafe_allow_html=True,
    )


def render_nad_tab(network, selected_vl):
    from pypowsybl.network import NadParameters
    depth = st.slider("Depth", min_value=0, max_value=10, value=1, key="nad_depth_slider")

    if not selected_vl:
        st.info("Select a voltage level in the sidebar to display the Network Area Diagram.")
        return

    with st.spinner("Generating Network Area Diagram..."):
        try:
            nad_params = NadParameters(edge_name_displayed=True, power_value_precision=1)
            nad = network.get_network_area_diagram(
                voltage_level_ids=[selected_vl],
                depth=depth,
                nad_parameters=nad_params,
            )
            svg = nad.svg
            metadata = nad.metadata
        except Exception as e:
            st.error(f"Error generating NAD: {e}")
            return

    click = render_interactive_nad(
        svg=svg,
        metadata=metadata,
        height=700,
        key=f"nad_{selected_vl}_{depth}",
    )

    if click and click.get("type") == "nad-vl-click":
        vl = click.get("vl")
        if vl and vl != st.session_state.get("selected_vl"):
            st.session_state.selected_vl = vl
            st.rerun()


def render_sld_tab(network, selected_vl):
    from pypowsybl.network import SldParameters
    if not selected_vl:
        st.info("Select a voltage level in the sidebar to display the Single Line Diagram.")
        return

    with st.spinner("Generating Single Line Diagram..."):
        try:
            sld_params = SldParameters(use_name=True, tooltip_enabled=True)
            sld = network.get_single_line_diagram(
                selected_vl,
                parameters=sld_params,
            )
            svg = sld.svg
            metadata = sld.metadata
        except Exception as e:
            st.error(f"Error generating SLD: {e}")
            return

    click = render_interactive_sld(
        svg=svg,
        metadata=metadata,
        height=700,
        key=f"sld_{selected_vl}",
    )

    _render_bus_legend(network, selected_vl)

    if click and click.get("type") == "sld-vl-click":
        vl = click.get("vl")
        if vl and vl != st.session_state.get("selected_vl"):
            st.session_state.selected_vl = vl
            st.rerun()
