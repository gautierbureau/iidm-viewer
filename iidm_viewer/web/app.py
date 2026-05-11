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
_NAD_DIST = os.path.join(_FRONTEND_DIR, "nad_component", "dist")
_SLD_DIST = os.path.join(_FRONTEND_DIR, "sld_component", "dist")

# URL prefixes under which the bundles are served. The bundles
# reference ``./assets/<name>.js`` relatively, so the static mount
# must terminate the same way Streamlit's `declare_component(path=…)`
# does.
_MAP_URL = "/_iidm/map_component"
_NAD_URL = "/_iidm/nad_component"
_SLD_URL = "/_iidm/sld_component"

app.add_static_files(_MAP_URL, _MAP_DIST)
app.add_static_files(_NAD_URL, _NAD_DIST)
app.add_static_files(_SLD_URL, _SLD_DIST)


# Component-types registry used by the Data Explorer tab. Sourced from
# ``iidm_viewer.component_registry`` (the framework-agnostic module
# both Qt and NiceGUI prototypes share). Aliased for backwards
# compatibility with earlier tests that imported ``COMPONENT_GETTERS``
# from this module.
from iidm_viewer.component_registry import (
    COMPONENT_TYPES as COMPONENT_GETTERS,
    TOPOLOGY_AFFECTING_ATTRIBUTES,
    apply_bulk_edit,
    apply_cell_edit,
    editable_attributes,
    get_dataframe,
    is_editable,
)


# ---------------------------------------------------------------------------
# Shared state — single-client prototype
# ---------------------------------------------------------------------------
_state = AppState()
_map_data_version = 0
_map_ready = False
_nad_ready = False
_sld_ready = False
# When the corresponding iframe is not yet ready, queue the latest
# render payload and dispatch as soon as the bundle posts its
# 'streamlit:componentReady'.
_pending_map: Optional[dict] = None
_pending_nad: Optional[dict] = None
_pending_sld: Optional[dict] = None

# Diagram caches — same idea as the PySide6 prototype.
_sld_cache: dict[str, tuple[str, str]] = {}
_nad_cache: dict[tuple[str, int], tuple[str, str]] = {}

# NAD depth (number of hops shown around the focus VL). Mutated by
# the depth input in the NAD tab.
_nad_depth: int = 1


# ---------------------------------------------------------------------------
# pypowsybl helpers — routed through the shared
# ``iidm_viewer.diagram_services`` so the Streamlit + Qt + NiceGUI
# front-ends share one code path.
# ---------------------------------------------------------------------------
from iidm_viewer.diagram_services import (
    extract_map_data as _extract_map_data,
    generate_nad as _generate_nad,
    generate_sld as _generate_sld,
)


def _fetch_dataframe(network: NetworkProxy, getter_name: str):
    """Worker-routed fetch by *pypowsybl method name*.

    A thin shim against the registry's :func:`get_dataframe`, which
    takes a *component label*. Kept so existing tests that probe the
    lower-level entry don't have to change.
    """
    import pandas as pd

    raw = object.__getattribute__(network, "_obj")

    def _do():
        method = getattr(raw, getter_name, None)
        if method is None:
            return pd.DataFrame()
        df = method()
        if df is not None and df.index.name:
            df = df.reset_index()
        return df if df is not None else pd.DataFrame()

    return run(_do)


# ---------------------------------------------------------------------------
# JS bridge — single page-level <script> that adapts the Streamlit iframe
# protocol to NiceGUI's emitEvent / ui.on bus.
# ---------------------------------------------------------------------------
_BRIDGE_JS = r"""
(function () {
  // Each component is identified by a short name; the iframe id is
  // derived by convention. Keeping the registry data-driven means
  // adding a 4th iframe later is one line.
  const COMPONENTS = ['map', 'nad', 'sld'];

  function iframeFor(component) {
    return document.getElementById('iidm-' + component + '-iframe');
  }

  function componentForSource(src) {
    for (const c of COMPONENTS) {
      const f = iframeFor(c);
      if (f && src === f.contentWindow) return c;
    }
    return null;
  }

  window.iidmRenderTo = function (component, args) {
    const iframe = iframeFor(component);
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


def _push_nad(vl_id: str, depth: int) -> None:
    global _pending_nad
    if not vl_id or _state.network is None:
        return
    key = (vl_id, int(depth))
    entry = _nad_cache.get(key)
    if entry is None:
        try:
            entry = _generate_nad(_state.network, vl_id, int(depth))
        except Exception as exc:
            ui.notify(f"NAD generation failed for {vl_id}: {exc}", type="negative")
            return
        _nad_cache[key] = entry
    svg, metadata = entry
    args = {"svg": svg, "metadata": metadata, "height": 700}
    if _nad_ready:
        _send_render("nad", args)
    else:
        _pending_nad = args


# ---------------------------------------------------------------------------
# Data Explorer helpers
# ---------------------------------------------------------------------------
def _dataframe_to_aggrid_options(df, editable_cols: Optional[list] = None) -> dict:
    """Build an ag-Grid options dict from a pandas DataFrame.

    * NaN → em-dash for parity with the Streamlit / Qt prototypes.
    * Per-column sort (header click) and per-column filter (column
      menu) are enabled via ``defaultColDef`` so every column gets
      them without having to enumerate.
    * The "id" column is pinned-left so it stays visible while
      scrolling wide tables (lines, generators).
    * Columns listed in ``editable_cols`` get ``editable: true`` so
      ag-Grid surfaces an inline editor — the host listens for
      ``cellValueChanged`` to commit the edit.
    """
    import math

    if df is None or df.empty:
        return {
            "columnDefs": [],
            "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }

    editable_set = set(editable_cols or [])

    def _cell(v):
        if v is None:
            return "—"
        if isinstance(v, float):
            if math.isnan(v):
                return "—"
            return format(v, ".4g")
        return v

    columns = [str(c) for c in df.columns]
    column_defs = []
    for c in columns:
        dtype = df[c].dtype
        kind = getattr(dtype, "kind", "")
        is_numeric = kind in ("i", "u", "f")
        is_bool = kind == "b"
        defn: dict = {"headerName": c, "field": c}
        if c == "id":
            defn["pinned"] = "left"
            # Surface ag-Grid Community's row-selection checkbox on the
            # id column — gives a discoverable affordance for bulk edit
            # without committing to ag-Grid Enterprise's checkbox column.
            defn["checkboxSelection"] = True
            defn["headerCheckboxSelection"] = True
        if is_numeric:
            defn["type"] = "numericColumn"
            defn["filter"] = "agNumberColumnFilter"
        elif is_bool:
            defn["filter"] = True   # default (set / text)
            defn["cellEditor"] = "agSelectCellEditor"
            defn["cellEditorParams"] = {"values": [True, False]}
        # else: defaults — sortable, text filter, resizable.
        if c in editable_set:
            defn["editable"] = True
            defn["cellStyle"] = {"backgroundColor": "#fff7e0"}
        column_defs.append(defn)

    row_data = [
        {c: _cell(row[c]) for c in columns}
        for _, row in df.iterrows()
    ]
    return {
        "columnDefs": column_defs,
        "rowData": row_data,
        "defaultColDef": _DEFAULT_COL_DEF,
        # ``multiple`` plus the ``id`` checkbox column gives ag-Grid
        # Community Ctrl/Shift multi-row picking; the bulk-edit panel
        # reads ``get_selected_rows`` to map back to element ids.
        "rowSelection": "multiple",
        "suppressRowClickSelection": False,
    }


# Apply sortable / resizable / floating-filter to every column once,
# rather than repeating it on each column def.
_DEFAULT_COL_DEF: dict = {
    "sortable": True,
    "resizable": True,
    "filter": True,
    "floatingFilter": True,
}


def _build_data_explorer():
    """Materialise the Data Explorer panel and return a refresh closure.

    The closure re-fetches the DataFrame for whatever component is
    selected in the combo and pushes it into the ag-Grid. Filter +
    sort are handled inside ag-Grid (per-column floating filters,
    default sort on header click). Edits are dispatched here via the
    ``cellValueChanged`` event.
    """
    with ui.row().classes("q-pa-sm items-center w-full"):
        ui.label("Component:")
        select = ui.select(
            options=list(COMPONENT_GETTERS),
            value="Substations",
        ).props("dense outlined").classes("w-64")
        summary = ui.label("Load a network to inspect its components.") \
            .classes("text-caption q-ml-md")

    grid = ui.aggrid({
        "columnDefs": [], "rowData": [],
        "defaultColDef": _DEFAULT_COL_DEF,
        "rowSelection": "multiple",
    }).classes("w-full").style("height: 600px")

    # --- Bulk-edit panel --------------------------------------------------
    # ag-Grid keeps the selection on the client; we resolve it on demand
    # via ``grid.get_selected_rows()`` rather than mirroring it in Python.
    with ui.row().classes("q-pa-sm items-center w-full") as bulk_row:
        bulk_label = ui.label("Apply to selection:")
        bulk_attr = ui.select(options=[], value=None) \
            .props("dense outlined").classes("w-48")
        ui.label("=")
        bulk_value = ui.input(placeholder="New value") \
            .props("dense outlined").classes("flex-grow")
        bulk_button = ui.button("Apply")
    bulk_row.set_visibility(False)

    def refresh() -> None:
        label = select.value
        if _state.network is None or not label:
            grid.options = {
                "columnDefs": [], "rowData": [],
                "defaultColDef": _DEFAULT_COL_DEF,
                "rowSelection": "multiple",
            }
            grid.update()
            summary.set_text("No network loaded.")
            bulk_row.set_visibility(False)
            return
        try:
            df = get_dataframe(_state.network, label)
        except Exception as exc:
            grid.options = {
                "columnDefs": [], "rowData": [],
                "defaultColDef": _DEFAULT_COL_DEF,
                "rowSelection": "multiple",
            }
            grid.update()
            summary.set_text(f"{label}: failed — {exc}")
            bulk_row.set_visibility(False)
            return
        cols = [c for c in editable_attributes(label) if c in df.columns]
        grid.options = _dataframe_to_aggrid_options(df, editable_cols=cols)
        grid.update()
        editable_msg = " · editable: " + ", ".join(cols) if cols else ""
        if df.empty:
            summary.set_text(f"{label}: empty (no rows in this network)")
        else:
            summary.set_text(
                f"{label}: {df.shape[0]} rows · {df.shape[1]} columns{editable_msg}"
            )
        # Refresh the bulk-edit attribute combo so it offers only the
        # editable columns for *this* component. Hide the whole panel
        # when the component isn't editable at all.
        bulk_attr.options = cols
        bulk_attr.value = cols[0] if cols else None
        bulk_attr.update()
        bulk_row.set_visibility(bool(cols))
        bulk_label.set_text("Apply to selection:")

    def on_cell_changed(e) -> None:
        """ag-Grid emits ``cellValueChanged`` with ``data, colId, oldValue, newValue``."""
        args = e.args or {}
        col_id = args.get("colId") or (args.get("column") or {}).get("colId")
        new_value = args.get("newValue")
        old_value = args.get("oldValue")
        row = args.get("data") or {}
        element_id = row.get("id")
        component = select.value
        if not element_id or not col_id or _state.network is None:
            return
        if not is_editable(component, col_id):
            return
        try:
            apply_cell_edit(_state.network, component, str(element_id), col_id, new_value)
        except Exception as exc:
            ui.notify(
                f"Edit rejected — {component}/{element_id}/{col_id}: {exc}",
                type="negative",
            )
            # Refresh to revert the failed edit (cheap; 1 worker call).
            refresh()
            return
        ui.notify(
            f"{component}/{element_id}/{col_id}: {old_value} → {new_value}",
            type="positive",
            timeout=1500,
        )
        # Topology-affecting edits invalidate the diagram caches so
        # the next time the user opens the NAD / SLD tab they see
        # the refreshed picture.
        if col_id in TOPOLOGY_AFFECTING_ATTRIBUTES:
            _nad_cache.clear()
            _sld_cache.clear()
            if _state.selected_vl:
                _push_sld(_state.selected_vl)
                _push_nad(_state.selected_vl, _nad_depth)

    async def on_bulk_apply() -> None:
        component = select.value
        attribute = bulk_attr.value
        new_value = bulk_value.value
        if _state.network is None or not component or not attribute:
            return
        selected = await grid.get_selected_rows()
        ids = [str(r["id"]) for r in (selected or []) if r.get("id") is not None]
        if not ids:
            ui.notify("Select one or more rows first.", type="warning")
            return
        try:
            prev_map = apply_bulk_edit(
                _state.network, component, ids, attribute, new_value,
            )
        except Exception as exc:
            ui.notify(
                f"Bulk edit rejected — {component}/{len(ids)} rows/{attribute}: {exc}",
                type="negative",
            )
            return
        ui.notify(
            f"{component}: {attribute} = {new_value} applied to {len(ids)} rows",
            type="positive",
            timeout=1500,
        )
        bulk_value.value = ""
        bulk_value.update()
        # Topology-affecting bulk changes flush the diagram caches so
        # a subsequent tab switch shows the updated picture.
        if attribute in TOPOLOGY_AFFECTING_ATTRIBUTES:
            _nad_cache.clear()
            _sld_cache.clear()
            if _state.selected_vl:
                _push_sld(_state.selected_vl)
                _push_nad(_state.selected_vl, _nad_depth)
        # Refresh the grid so the new (possibly coerced) values appear.
        refresh()

    bulk_button.on_click(on_bulk_apply)
    grid.on("cellValueChanged", on_cell_changed)
    select.on_value_change(lambda _e: refresh())
    return refresh


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
    global _map_ready, _nad_ready, _sld_ready, _pending_map, _pending_nad, _pending_sld
    _map_ready = False
    _nad_ready = False
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
        nad_tab = ui.tab("Network Area Diagram")
        sld_tab = ui.tab("Single Line Diagram")
        data_tab = ui.tab("Data Explorer Components")
    panels = ui.tab_panels(tabs, value=map_tab).classes("w-full")
    with panels:
        with ui.tab_panel(map_tab).classes("q-pa-none"):
            ui.html(
                f'<iframe id="iidm-map-iframe" src="{_MAP_URL}/index.html" '
                'style="width:100%;height:670px;border:none;display:block"></iframe>'
            )
        with ui.tab_panel(nad_tab).classes("q-pa-none"):
            with ui.row().classes("q-pa-sm items-center"):
                ui.label("Depth:")
                depth_input = ui.number(value=_nad_depth, min=0, max=10, step=1, format="%d") \
                    .props("dense outlined").classes("w-24")
                nad_caption = ui.label(
                    "Click any node to jump to its Single Line Diagram."
                ).classes("text-caption q-ml-md")

                def _on_depth_changed(e):
                    global _nad_depth
                    try:
                        _nad_depth = max(0, int(e.value))
                    except (TypeError, ValueError):
                        return
                    if _state.selected_vl:
                        _push_nad(_state.selected_vl, _nad_depth)

                depth_input.on("update:model-value", _on_depth_changed)
            ui.html(
                f'<iframe id="iidm-nad-iframe" src="{_NAD_URL}/index.html" '
                'style="width:100%;height:700px;border:none;display:block"></iframe>'
            )
        with ui.tab_panel(sld_tab).classes("q-pa-none"):
            ui.html(
                f'<iframe id="iidm-sld-iframe" src="{_SLD_URL}/index.html" '
                'style="width:100%;height:700px;border:none;display:block"></iframe>'
            )
        with ui.tab_panel(data_tab):
            refresh_data_grid = _build_data_explorer()

    # ------------------------------------------------------------------
    # Cross-tab navigation: substation click on map -> SLD tab on that VL.
    # ------------------------------------------------------------------
    def _on_state_network(network):
        if network is None:
            return
        _push_map()
        tabs.set_value(map_tab)
        refresh_data_grid()

    def _on_state_vl(vl_id):
        vl_lbl.set_text(f"VL: {vl_id}" if vl_id else "VL: —")
        if vl_id:
            _push_sld(vl_id)
            _push_nad(vl_id, _nad_depth)

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
        global _map_ready, _nad_ready, _sld_ready
        global _pending_map, _pending_nad, _pending_sld
        component = e.args.get("component")
        if component == "map":
            _map_ready = True
            if _pending_map is not None:
                _send_render("map", _pending_map)
                _pending_map = None
        elif component == "nad":
            _nad_ready = True
            if _pending_nad is not None:
                _send_render("nad", _pending_nad)
                _pending_nad = None
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
        elif component == "nad" and value.get("type") == "nad-vl-click":
            new_vl = value.get("vl")
            if new_vl:
                tabs.set_value(sld_tab)
                _state.set_selected_vl(new_vl)
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
        file_lbl.set_text("(pre-loaded)")
        _push_map()
        refresh_data_grid()
        if _state.selected_vl:
            vl_lbl.set_text(f"VL: {_state.selected_vl}")
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)


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
