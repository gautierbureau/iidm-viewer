import html
import math
import re

import streamlit as st
from iidm_viewer.nad_component import render_interactive_nad
from iidm_viewer.sld_component import render_interactive_sld


# Fallback palette used only when the exact SLD-SVG color cannot be
# resolved (e.g. the SVG carries no busbar-section tagging for a bus).
# The primary path parses the real `--sld-vl-color` values out of the
# pypowsybl-generated SVG so the legend dots match the SLD.
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

# Matches `.sld-vl120to180.sld-bus-0 {--sld-vl-color: #00AFAE}` in the SLD
# SVG's <style> CDATA block.
_SLD_COLOR_RE = re.compile(
    r"\.sld-(vl\d+to\d+)\.sld-bus-(\d+)\s*\{\s*--sld-vl-color:\s*(#[0-9A-Fa-f]{6})"
)
# Matches `<g class="sld-busbar-section sld-vl120to180 sld-bus-0" id="idB4" ...>`.
# Class-order is fixed by pypowsybl's SLG renderer: busbar-section, then
# voltage band, then bus index.
_SLD_BUSBAR_RE = re.compile(
    r'<g\s+class="sld-busbar-section\s+sld-(vl\d+to\d+)\s+sld-bus-(\d+)"\s+id="id([^"]+)"'
)
# The SLG renderer encodes non-alphanumeric characters in SVG element IDs as
# _<decimal ASCII>_  (e.g. underscore '_' → '_95_', hyphen '-' → '_45_').
_SVG_ID_ENCODE_RE = re.compile(r'_(\d+)_')


def _decode_svg_id(encoded: str) -> str:
    """Decode the SLG id encoding back to the original network element id."""
    return _SVG_ID_ENCODE_RE.sub(lambda m: chr(int(m.group(1))), encoded)


def _parse_sld_palette(svg: str) -> dict:
    """Return ``{(band, bus_index): "#RRGGBB"}`` from the SVG <style> block."""
    return {
        (m.group(1), int(m.group(2))): m.group(3)
        for m in _SLD_COLOR_RE.finditer(svg or "")
    }


def _parse_sld_busbar_indices(svg: str) -> dict:
    """Return ``{busbar_section_id: (band, bus_index)}`` from <g> elements.

    SVG element ids use the SLG encoding (``_<decimal>_``) for special chars;
    decoded back to the real network element id before returning.
    """
    return {
        _decode_svg_id(m.group(3)): (m.group(1), int(m.group(2)))
        for m in _SLD_BUSBAR_RE.finditer(svg or "")
    }


def _resolve_bus_colors(network, selected_vl: str, svg: str) -> dict:
    """Map each calculated bus id in ``selected_vl`` to its SLD color.

    Joins three pieces:
      * palette from <style>                          — ``(band, idx) -> hex``
      * busbar-section classes from <g> elements      — ``busbar_id -> (band, idx)``
      * network topology                              — ``busbar_id -> bus_id``

    Returns an empty dict on any parse/topology failure; the caller falls
    back to :data:`_BUS_LEGEND_PALETTE`.
    """
    palette = _parse_sld_palette(svg)
    busbars = _parse_sld_busbar_indices(svg)
    if not palette or not busbars:
        return {}

    # busbar-section id -> calculated bus id. Try node-breaker first
    # (get_busbar_sections lists real sections), then bus-breaker (the
    # SLG renderer injects a virtual busbar per bus-breaker bus, whose
    # id matches the bus-breaker bus id).
    # Use the network-level cache — busbar topology never changes during navigation.
    bb_to_bus: dict = {}
    bbs = _get_busbar_sections(network)
    if bbs is not None:
        try:
            vl_bbs = bbs[bbs["voltage_level_id"].astype(str) == str(selected_vl)]
            for bb_id, row in vl_bbs.iterrows():
                bus_id = row.get("bus_id")
                if bus_id:
                    bb_to_bus[str(bb_id)] = str(bus_id)
        except Exception:
            pass
    if not bb_to_bus:
        try:
            tp = network.get_bus_breaker_topology(selected_vl)
            for bb_id, row in tp.buses.iterrows():
                bus_id = row.get("bus_id")
                if bus_id:
                    bb_to_bus[str(bb_id)] = str(bus_id)
        except Exception:
            pass

    colors: dict = {}
    for bb_id, key in busbars.items():
        bus_id = bb_to_bus.get(bb_id)
        color = palette.get(key)
        if bus_id and color:
            colors.setdefault(bus_id, color)
    return colors


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


def _render_bus_legend(network, selected_vl: str, svg: str = "") -> None:
    """Show one row per bus in `selected_vl` — colored dot, bus id, V (kV), angle (°).

    Voltages come from `network.get_buses(all_attributes=True)`; `v_mag`
    and `v_angle` are NaN until a load flow has been run, in which case
    we show em-dashes. Colors are parsed from ``svg`` so they match
    pypowsybl's SLG output exactly; buses the SVG doesn't tag fall back
    to :data:`_BUS_LEGEND_PALETTE` (ordered by bus index in the VL).
    """
    buses = _get_buses_all(network)
    if buses is None or buses.empty:
        return

    buses["voltage_level_id"] = buses["voltage_level_id"].astype(str)
    vl_buses = buses[buses["voltage_level_id"] == str(selected_vl)]
    if vl_buses.empty:
        return

    bus_colors = _resolve_bus_colors(network, selected_vl, svg)

    rows_html = []
    for i, (_, row) in enumerate(vl_buses.iterrows()):
        bus_id_raw = str(row.get("id", ""))
        color = bus_colors.get(bus_id_raw) or _BUS_LEGEND_PALETTE[i % len(_BUS_LEGEND_PALETTE)]
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

    cache_key = (selected_vl, depth)
    nad_cache = st.session_state.setdefault("_nad_cache", {})
    cached = nad_cache.get(cache_key)
    if cached is not None:
        svg, metadata = cached
    else:
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
        nad_cache[cache_key] = (svg, metadata)

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
            st.session_state["_vl_set_by_click"] = True
            st.rerun()


def _net_key(network) -> int:
    """Stable id for the underlying pypowsybl Network object."""
    return id(object.__getattribute__(network, "_obj"))


def _get_substation_map(network) -> dict:
    """Return ``{vl_id: (substation_id, has_multi_vl)}`` for every VL.

    Reuses the ``get_voltage_levels_df`` result that the VL selector already
    fetches on every rerun — zero additional worker round-trips.
    """
    key = _net_key(network)
    if st.session_state.get("_sub_map_net") == key:
        return st.session_state["_sub_map_cache"]
    mapping: dict = {}
    try:
        from iidm_viewer.state import get_voltage_levels_df
        vls = get_voltage_levels_df(network)
        sub_count = vls.groupby("substation_id")["id"].count().to_dict()
        for _, row in vls.iterrows():
            vl_id = str(row["id"])
            sid_raw = row.get("substation_id")
            sid = str(sid_raw) if sid_raw else None
            mapping[vl_id] = (sid, sub_count.get(sid, 0) > 1 if sid else False)
    except Exception:
        mapping = {}
    st.session_state["_sub_map_cache"] = mapping
    st.session_state["_sub_map_net"] = key
    return mapping


def _get_busbar_sections(network):
    """Cache ``get_busbar_sections(all_attributes=True)`` per network.

    Busbar-section topology is static; caching avoids 2 round-trips on
    every SLD tab rerun.  Returns ``None`` on failure.
    """
    key = _net_key(network)
    if st.session_state.get("_bbs_net") == key:
        return st.session_state["_bbs_cache"]
    try:
        bbs = network.get_busbar_sections(all_attributes=True)
    except Exception:
        bbs = None
    st.session_state["_bbs_cache"] = bbs
    st.session_state["_bbs_net"] = key
    return bbs


def _get_buses_all(network):
    """Return ``get_buses(all_attributes=True)`` with ``reset_index()``, cached.

    Delegates to :func:`iidm_viewer.caches.get_buses_all` which owns the
    ``(net_key, lf_gen)`` invalidation. Returns ``None`` on empty result so
    call sites can keep their ``if buses is None or buses.empty`` guard.
    """
    from iidm_viewer.caches import get_buses_all
    df = get_buses_all(network)
    return df.reset_index() if not df.empty else None


def render_sld_tab(network, selected_vl):
    from pypowsybl.network import SldParameters
    if not selected_vl:
        st.info("Select a voltage level in the sidebar to display the Single Line Diagram.")
        return

    # Reset expand state when the primary VL changes.
    if st.session_state.get("_sld_last_vl") != selected_vl:
        st.session_state["_sld_last_vl"] = selected_vl
        st.session_state["sld_show_substation"] = False

    show_substation = bool(st.session_state.get("sld_show_substation", False))

    # Pure-Python lookup — the substation map is built once per network load.
    substation_id, multi_vl = _get_substation_map(network).get(selected_vl, (None, False))

    # Determine the container to render and the svgType for the viewer.
    if show_substation and substation_id:
        container_id = substation_id
        svg_type = "substation"
    else:
        container_id = selected_vl
        svg_type = "voltage-level"

    # Expand / collapse button (only shown when the substation has >1 VL).
    if substation_id and multi_vl:
        if show_substation:
            if st.button("Collapse to voltage level", key="sld_collapse_btn"):
                st.session_state["sld_show_substation"] = False
                st.rerun()
        else:
            if st.button("Expand to substation", key="sld_expand_btn"):
                st.session_state["sld_show_substation"] = True
                st.rerun()

    sld_cache = st.session_state.setdefault("_sld_cache", {})
    cached_sld = sld_cache.get(container_id)
    if cached_sld is not None:
        svg, metadata = cached_sld
    else:
        with st.spinner("Generating Single Line Diagram..."):
            try:
                sld_params = SldParameters(use_name=True, tooltip_enabled=True)
                sld = network.get_single_line_diagram(
                    container_id,
                    parameters=sld_params,
                )
                svg = sld.svg
                metadata = sld.metadata
            except Exception as e:
                st.error(f"Error generating SLD: {e}")
                return
        sld_cache[container_id] = (svg, metadata)

    click = render_interactive_sld(
        svg=svg,
        metadata=metadata,
        height=700,
        svg_type=svg_type,
        key=f"sld_{container_id}",
    )

    _render_bus_legend(network, selected_vl, svg)

    if click and click.get("type") == "sld-vl-click":
        vl = click.get("vl")
        if vl and vl != st.session_state.get("selected_vl"):
            st.session_state.selected_vl = vl
            st.session_state["_vl_set_by_click"] = True
            st.rerun()
