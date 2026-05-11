"""NiceGUI page for the iidm-viewer prototype (Map + SLD tabs).

The two existing JS bundles in ``frontend/{map,sld}_component/dist``
speak Streamlit's iframe wire-protocol. We re-host them as plain
``<iframe src=…>`` in the NiceGUI page and bridge the postMessage
protocol to NiceGUI's event bus (``emitEvent`` on the JS side,
``ui.on`` on the Python side). No fork of the bundles is needed —
the same dist tree is shared with the Streamlit and PySide6 paths.

Single-client design: the prototype is intended to be launched with
``ui.run(native=True)`` (or ``--no-native`` for a browser), so a
single :class:`AppState` instance is held at module level. Multi-
client sharing is out of scope for the spike.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from nicegui import app, ui

from iidm_viewer.powsybl_worker import NetworkProxy, run
from iidm_viewer.web.state import AppState


_FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "frontend"
)
_MAP_DIST = os.path.join(_FRONTEND_DIR, "map_component", "dist")
_SLD_DIST = os.path.join(_FRONTEND_DIR, "sld_component", "dist")

# URL prefixes under which the bundles are served. The bundles
# reference ``./assets/<name>.js`` relatively, so the static mount
# must terminate the same way Streamlit's `declare_component(path=…)`
# does.
_MAP_URL = "/_iidm/map_component"
_SLD_URL = "/_iidm/sld_component"

app.add_static_files(_MAP_URL, _MAP_DIST)
app.add_static_files(_SLD_URL, _SLD_DIST)


# ---------------------------------------------------------------------------
# Shared state — single-client prototype
# ---------------------------------------------------------------------------
_state = AppState()
_map_data_version = 0
_map_ready = False
_sld_ready = False
# When the corresponding iframe is not yet ready, queue the latest
# render payload and dispatch as soon as the bundle posts its
# 'streamlit:componentReady'.
_pending_map: Optional[dict] = None
_pending_sld: Optional[dict] = None

# Per-VL SLD cache (svg, metadata). Same idea as the PySide6 prototype
# — bypasses regeneration when the user revisits a VL.
_sld_cache: dict[str, tuple[str, str]] = {}


# ---------------------------------------------------------------------------
# pypowsybl helpers (all worker-routed)
# ---------------------------------------------------------------------------
def _extract_map_data(network: NetworkProxy):
    raw = object.__getattribute__(network, "_obj")

    def _extract():
        from pypowsybl_jupyter.networkmapwidget import NetworkMapWidget

        class _Extractor(NetworkMapWidget):
            def __init__(self):  # skip widget init
                pass

            def __del__(self):
                pass

        (lmap, lpos, smap, spos, _vl_subs, _sub_vls, _subs_ids, tlmap, hlmap) = (
            _Extractor().extract_map_data(raw, display_lines=True, use_line_geodata=False)
        )
        if not spos:
            return None
        return smap, spos, lmap + tlmap + hlmap, lpos

    return run(_extract)


def _generate_sld(network: NetworkProxy, vl_id: str):
    raw = object.__getattribute__(network, "_obj")

    def _do():
        from pypowsybl.network import SldParameters
        params = SldParameters(use_name=True, tooltip_enabled=True)
        sld = raw.get_single_line_diagram(vl_id, parameters=params)
        return sld.svg, sld.metadata

    return run(_do)


# ---------------------------------------------------------------------------
# JS bridge — single page-level <script> that adapts the Streamlit iframe
# protocol to NiceGUI's emitEvent / ui.on bus.
# ---------------------------------------------------------------------------
_BRIDGE_JS = r"""
(function () {
  // Identify which iframe a message came from by comparing event.source
  // to each iframe's contentWindow.
  function componentForSource(src) {
    const m = document.getElementById('iidm-map-iframe');
    const s = document.getElementById('iidm-sld-iframe');
    if (m && src === m.contentWindow) return 'map';
    if (s && src === s.contentWindow) return 'sld';
    return null;
  }

  window.iidmRenderTo = function (component, args) {
    const id = component === 'map' ? 'iidm-map-iframe' : 'iidm-sld-iframe';
    const iframe = document.getElementById(id);
    if (!iframe || !iframe.contentWindow) return;
    iframe.contentWindow.postMessage({ type: 'streamlit:render', args: args || {} }, '*');
  };

  window.addEventListener('message', function (e) {
    const d = e.data;
    if (!d || d.isStreamlitMessage !== true) return;
    const component = componentForSource(e.source);
    if (!component) return;
    if (d.type === 'streamlit:componentReady') {
      emitEvent('iidm-component-ready', { component: component });
    } else if (d.type === 'streamlit:setComponentValue') {
      emitEvent('iidm-component-value', { component: component, value: d.value });
    }
    // streamlit:setFrameHeight is ignored — iframe height is fixed by CSS.
  });
})();
"""


# ---------------------------------------------------------------------------
# Render dispatchers
# ---------------------------------------------------------------------------
def _send_render(component: str, args: dict) -> None:
    """Post a render payload to the named iframe via the JS bridge."""
    payload = json.dumps(args)
    ui.run_javascript(f"window.iidmRenderTo({json.dumps(component)}, {payload});")


def _push_map() -> None:
    global _pending_map, _map_data_version
    if _state.network is None:
        return
    data = _extract_map_data(_state.network)
    if data is None:
        args = {
            "substations": [], "substationPositions": [],
            "lines": [], "linePositions": [],
            "version": _map_data_version + 1, "height": 670,
        }
    else:
        substations, positions, lines, line_positions = data
        args = {
            "substations": substations,
            "substationPositions": positions,
            "lines": lines,
            "linePositions": line_positions or [],
            "version": _map_data_version + 1,
            "height": 670,
        }
    _map_data_version += 1
    if _map_ready:
        _send_render("map", args)
    else:
        _pending_map = args


def _push_sld(vl_id: str) -> None:
    global _pending_sld
    if not vl_id or _state.network is None:
        return
    entry = _sld_cache.get(vl_id)
    if entry is None:
        try:
            entry = _generate_sld(_state.network, vl_id)
        except Exception as exc:
            ui.notify(f"SLD generation failed for {vl_id}: {exc}", type="negative")
            return
        _sld_cache[vl_id] = entry
    svg, metadata = entry
    args = {
        "svg": svg, "metadata": metadata,
        "height": 700, "svgType": "voltage-level",
    }
    if _sld_ready:
        _send_render("sld", args)
    else:
        _pending_sld = args


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
@ui.page("/")
def main_page() -> None:
    """Wire up the page on every client connect.

    For the desktop mode (``ui.run(native=True)``) this fires once at
    startup. Refreshing the browser also re-fires it — the shared
    state above survives, but iframe-ready flags reset because the
    page DOM is new.
    """
    global _map_ready, _sld_ready, _pending_map, _pending_sld
    _map_ready = False
    _sld_ready = False

    # Page-level bridge JS, head-injected so emitEvent is bound by the
    # time the iframes finish loading.
    ui.add_body_html(f"<script>{_BRIDGE_JS}</script>")

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    with ui.header().classes("items-center bg-grey-2 text-black q-py-sm"):
        ui.label("IIDM Viewer — NiceGUI preview").classes("text-h6 q-mr-md")
        file_lbl = ui.label("No file loaded.").classes("text-caption q-mr-md")
        vl_lbl = ui.label("VL: —").classes("text-caption q-mr-md")

        async def handle_upload(e):
            tmp_path = f"/tmp/iidm_upload_{os.getpid()}_{os.path.basename(e.name)}"
            with open(tmp_path, "wb") as fh:
                fh.write(e.content.read())
            try:
                await asyncio.to_thread(_state.load_network_from_path, tmp_path)
            except Exception as exc:
                ui.notify(f"Load failed: {exc}", type="negative")
                return
            file_lbl.set_text(os.path.basename(e.name))

        ui.upload(
            on_upload=handle_upload,
            auto_upload=True,
            label="Load network…",
        ).props("flat dense accept='.xiidm,.iidm,.xml,.zip,.mat,.uct'").classes("q-mr-md")

    with ui.tabs().classes("w-full") as tabs:
        map_tab = ui.tab("Network Map")
        sld_tab = ui.tab("Single Line Diagram")
    panels = ui.tab_panels(tabs, value=map_tab).classes("w-full")
    with panels:
        with ui.tab_panel(map_tab).classes("q-pa-none"):
            ui.html(
                f'<iframe id="iidm-map-iframe" src="{_MAP_URL}/index.html" '
                'style="width:100%;height:670px;border:none;display:block"></iframe>'
            )
        with ui.tab_panel(sld_tab).classes("q-pa-none"):
            ui.html(
                f'<iframe id="iidm-sld-iframe" src="{_SLD_URL}/index.html" '
                'style="width:100%;height:700px;border:none;display:block"></iframe>'
            )

    # ------------------------------------------------------------------
    # Cross-tab navigation: substation click on map -> SLD tab on that VL.
    # ------------------------------------------------------------------
    def _on_state_network(network):
        if network is None:
            return
        _push_map()
        tabs.set_value(map_tab)

    def _on_state_vl(vl_id):
        vl_lbl.set_text(f"VL: {vl_id}" if vl_id else "VL: —")
        if vl_id:
            _push_sld(vl_id)

    # Listeners are registered fresh on every page connect; if a
    # previous registration is still around (browser refresh), the
    # old one calls into a stale `tabs` and would noop on a closed
    # client. NiceGUI is forgiving here for a single-user prototype.
    _state.on_network_changed(_on_state_network)
    _state.on_selected_vl_changed(_on_state_vl)

    # ------------------------------------------------------------------
    # Iframe -> Python event handlers
    # ------------------------------------------------------------------
    def _on_component_ready(e):
        global _map_ready, _sld_ready, _pending_map, _pending_sld
        component = e.args.get("component")
        if component == "map":
            _map_ready = True
            if _pending_map is not None:
                _send_render("map", _pending_map)
                _pending_map = None
        elif component == "sld":
            _sld_ready = True
            if _pending_sld is not None:
                _send_render("sld", _pending_sld)
                _pending_sld = None

    def _on_component_value(e):
        component = e.args.get("component")
        value = e.args.get("value") or {}
        if component == "map" and value.get("type") == "map-substation-click":
            vl_ids = value.get("vlIds") or []
            if vl_ids:
                tabs.set_value(sld_tab)
                _state.set_selected_vl(vl_ids[0])
        elif component == "sld" and value.get("type") == "sld-vl-click":
            new_vl = value.get("vl")
            if new_vl:
                _state.set_selected_vl(new_vl)

    ui.on("iidm-component-ready", _on_component_ready)
    ui.on("iidm-component-value", _on_component_value)

    # If a network was loaded before this client connected (e.g. via
    # a CLI ``initial_file``), seed the just-built UI from current
    # state right away.
    if _state.network is not None:
        _push_map()
        if _state.selected_vl:
            vl_lbl.set_text(f"VL: {_state.selected_vl}")
            _push_sld(_state.selected_vl)


def run_app(initial_file: Optional[str] = None, native: bool = True, port: int = 8669) -> None:
    """Boot the NiceGUI server.

    ``native=True`` opens in a pywebview window — desktop-app feel.
    ``native=False`` runs as a plain localhost server you connect to
    from any browser; handy for testing without GUI libs.
    """
    if initial_file:
        # Load before the server starts so the first page paint sees
        # a populated state.
        _state.load_network_from_path(initial_file)
    ui.run(
        title="IIDM Viewer (NiceGUI)",
        native=native,
        reload=False,
        port=port,
        show=not native,
    )
