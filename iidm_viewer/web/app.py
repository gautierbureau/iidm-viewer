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
    DISCONNECTABLE_COMPONENTS,
    DISCONNECT_ATTRS,
    REMOVABLE_COMPONENTS,
    TOPOLOGY_AFFECTING_ATTRIBUTES,
    apply_bulk_disconnect,
    apply_bulk_edit,
    apply_cell_edit,
    editable_attributes,
    get_dataframe,
    is_editable,
    remove_elements,
)
from iidm_viewer.component_creation import (
    CREATABLE_COMPONENTS,
    LOCATOR_FIELDS,
    coerce_field_values,
    create_component_bay,
    list_busbar_sections,
    list_node_breaker_voltage_levels,
)
from iidm_viewer.data_view import (
    FILTERS,
    VL_FILTERABLE,
    dataframe_to_csv,
    filter_by_voltage_level,
    get_enriched_dataframe,
    reorder_columns,
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


def _push_map_flyto(substation_id: str, zoom: float = 11) -> None:
    """Tell the map iframe to fly to ``substation_id`` (if known)."""
    import time
    global _pending_map
    args = {
        "version": _map_data_version,
        "height": 670,
        "flyTo": {
            "substationId": substation_id,
            "zoom": zoom,
            "ts": int(time.monotonic() * 1000),
        },
    }
    if _map_ready:
        _send_render("map", args)
    else:
        _pending_map = dict(_pending_map or {}, **args)


def _handle_sld_breaker_click(value: dict) -> None:
    """Mirror Streamlit's ``sld-breaker-click`` handler: decode the
    SVG id back to the pypowsybl switch id, toggle through the shared
    ``toggle_switch`` (one worker hop for read + write), record in the
    change log, and flush the diagram caches so the new state shows.
    """
    from iidm_viewer.component_registry import toggle_switch
    from iidm_viewer.navigation import decode_svg_id

    if _state.network is None:
        return
    encoded = str(value.get("breakerId", ""))
    if not encoded:
        return
    switch_id = decode_svg_id(encoded)
    new_open = bool(value.get("open", False))
    try:
        before, after = toggle_switch(_state.network, switch_id, new_open)
    except Exception as exc:
        ui.notify(f"Switch toggle failed: {exc}", type="negative")
        return
    _state.change_log.record("Switches", switch_id, "open", before, after)
    _nad_cache.clear()
    _sld_cache.clear()
    if _state.selected_vl:
        _push_sld(_state.selected_vl)
        _push_nad(_state.selected_vl, _nad_depth)


def _handle_sld_feeder_click(value: dict, tabs, map_tab) -> None:
    """Resolve the clicked feeder's "other side" substation and fly
    the Map tab to it. Falls back to a status notification when the
    substation can't be resolved (no geo data, unknown equipment, …).
    """
    from iidm_viewer.navigation import resolve_feeder_substation

    if _state.network is None:
        return
    equipment_id = value.get("equipmentId")
    equipment_type = value.get("equipmentType")
    current_vl = _state.selected_vl
    if not equipment_id or not current_vl:
        return
    substation_id = resolve_feeder_substation(
        _state.network, str(current_vl), str(equipment_id), equipment_type,
    )
    if not substation_id:
        ui.notify(
            f"No substation known for {equipment_type or 'feeder'} "
            f"{equipment_id}",
            type="warning",
        )
        return
    tabs.set_value(map_tab)
    _push_map_flyto(substation_id)
    ui.notify(
        f"Map: focused substation {substation_id}",
        type="positive",
        timeout=1200,
    )


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
def _dataframe_to_aggrid_options(
    df,
    editable_cols: Optional[list] = None,
    *,
    filterable_cols: Optional[list] = None,
) -> dict:
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
    # When ``filterable_cols`` is provided, only those columns get an
    # ag-Grid per-column filter — matches the Streamlit FILTERS whitelist.
    # ``None`` means "filter every column" (the default Q'nice behaviour).
    filterable_set = set(filterable_cols) if filterable_cols is not None else None

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
        # Whitelist filtering — narrow the per-column filter affordance
        # to FILTERS[component] when a filterable_set is supplied.
        if filterable_set is not None and c not in filterable_set and c != "id":
            defn["filter"] = False
            defn["floatingFilter"] = False
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


def _build_create_panel_widgets(state: dict, refresh_after_create) -> None:
    """Materialise the "Create a new component" expansion.

    Stores widget references in ``state`` for :func:`_refresh_create_panel`
    to read and rebuild from. ``refresh_after_create`` is invoked after
    a successful create so the data grid picks up the new row.
    """
    expansion = ui.expansion("Create a new component", icon="add").classes("w-full")
    expansion.visible = False
    state["expansion"] = expansion
    with expansion:
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Voltage level:")
            vl_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
            ui.label("Busbar section:")
            bbs_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-48")
        # Container for the dynamic per-component field widgets.
        fields_container = ui.row().classes("items-start w-full q-pa-sm flex-wrap")
        with ui.row().classes("items-center w-full q-pa-sm"):
            create_btn = ui.button("Create", icon="add_circle")
            status_label = ui.label("").classes("text-caption q-ml-md")

    state["vl_select"] = vl_select
    state["bbs_select"] = bbs_select
    state["fields_container"] = fields_container
    state["status_label"] = status_label
    state["create_btn"] = create_btn
    state["field_widgets"] = {}

    def _on_vl_change(_e=None) -> None:
        vl_id = vl_select.value
        if not vl_id or _state.network is None:
            bbs_select.options = []
            bbs_select.value = None
            bbs_select.update()
            return
        try:
            ids = list_busbar_sections(_state.network, str(vl_id))
        except Exception:
            ids = []
        bbs_select.options = ids
        bbs_select.value = ids[0] if ids else None
        bbs_select.update()

    vl_select.on_value_change(_on_vl_change)

    def _on_create_click() -> None:
        component = state.get("current_component")
        if not component or component not in CREATABLE_COMPONENTS or _state.network is None:
            return
        bbs_id = bbs_select.value
        if not bbs_id:
            status_label.set_text("Pick a busbar section first.")
            return
        spec = CREATABLE_COMPONENTS[component]
        all_fields = list(spec["fields"]) + list(LOCATOR_FIELDS)
        raw = {f["name"]: _read_create_widget(state, f) for f in all_fields}
        values = coerce_field_values(all_fields, raw)
        values["bus_or_busbar_section_id"] = str(bbs_id)
        try:
            create_component_bay(_state.network, component, values)
        except Exception as exc:
            status_label.set_text(f"Create failed — {exc}")
            ui.notify(f"Create failed: {exc}", type="negative")
            return
        created_id = str(values.get("id") or "")
        status_label.set_text(f"Created {component.rstrip('s')} {created_id!r}.")
        ui.notify(f"Created {component.rstrip('s')} {created_id!r}",
                  type="positive", timeout=1500)
        # Topology changed — flush diagram caches and refresh data grid.
        _nad_cache.clear()
        _sld_cache.clear()
        if _state.selected_vl:
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)
        refresh_after_create()

    create_btn.on_click(_on_create_click)


def _read_create_widget(state: dict, field: dict):
    w = state["field_widgets"].get(field["name"])
    if w is None:
        return None
    # NiceGUI's ui.* widgets all carry a ``value`` attribute.
    return getattr(w, "value", None)


def _refresh_create_panel(state: dict, component: str) -> None:
    """Repopulate the create panel for ``component``.

    Hides the whole expansion when the component isn't creatable or
    the network has no node-breaker voltage levels.
    """
    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if component not in CREATABLE_COMPONENTS or _state.network is None:
        expansion.visible = False
        return

    # Populate VL dropdown.
    try:
        vls = list_node_breaker_voltage_levels(_state.network)
    except Exception:
        vls = None
    vl_options = (
        {str(row["id"]): str(row["display"]) for _, row in vls.iterrows()}
        if vls is not None and not vls.empty else {}
    )
    state["vl_select"].options = vl_options
    state["vl_select"].value = next(iter(vl_options), None)
    state["vl_select"].update()
    if not vl_options:
        # No node-breaker VLs -> creation impossible.
        expansion.visible = False
        ui.notify("No node-breaker voltage levels — creation needs busbar sections.",
                  type="info", timeout=2000)
        return

    # Trigger the VL-change handler to populate busbar sections.
    # ui.select's on_value_change fires for programmatic changes too;
    # but to be safe re-call the populate manually.
    try:
        ids = list_busbar_sections(_state.network, str(state["vl_select"].value))
    except Exception:
        ids = []
    state["bbs_select"].options = ids
    state["bbs_select"].value = ids[0] if ids else None
    state["bbs_select"].update()

    # Rebuild the field widgets.
    container = state["fields_container"]
    container.clear()
    state["field_widgets"] = {}
    spec = CREATABLE_COMPONENTS[component]
    all_fields = list(spec["fields"]) + list(LOCATOR_FIELDS)
    with container:
        for f in all_fields:
            label = f["label"] + (" *" if f.get("required") else "")
            help_text = f.get("help") or ""
            with ui.column().classes("q-mr-md q-mb-md"):
                ui.label(label).classes("text-caption")
                kind = f["kind"]
                if kind == "text":
                    w = ui.input(value=str(f.get("default") or "")) \
                        .props("dense outlined")
                elif kind == "float":
                    w = ui.number(
                        value=float(f.get("default", 0.0)),
                        min=f.get("min_value"),
                        format="%.6f",
                    ).props("dense outlined")
                elif kind == "int":
                    w = ui.number(
                        value=int(f.get("default", 0)),
                        min=f.get("min_value"),
                        step=int(f.get("step", 1)),
                        format="%d",
                    ).props("dense outlined")
                elif kind == "bool":
                    w = ui.switch(value=bool(f.get("default", False)))
                elif kind == "select":
                    options = list(f.get("options", []))
                    w = ui.select(
                        options=options,
                        value=f.get("default") if f.get("default") in options else (options[0] if options else None),
                    ).props("dense outlined").classes("w-40")
                else:
                    continue
                if help_text:
                    w.tooltip(help_text)
                state["field_widgets"][f["name"]] = w

    state["status_label"].set_text("")
    expansion.text = f"Create a new {component.lower().rstrip('s')}"
    expansion.visible = True


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
        vl_filter = ui.checkbox("Filter by VL").classes("q-ml-md")
        vl_filter.visible = False
        ui.space()
        download_btn = ui.button("Download CSV").props("flat")
        summary = ui.label("Load a network to inspect its components.") \
            .classes("text-caption q-ml-md w-full")

    # Create-new-component expansion lives above the data grid.
    # The schema comes from CREATABLE_COMPONENTS; rebuilt on every
    # component change. Hidden when the active component isn't
    # creatable or the network has no node-breaker VLs.
    create_state = {
        "container": None,
        "vl_select": None,
        "bbs_select": None,
        "field_widgets": {},
        "status_label": None,
        "expansion": None,
    }
    _build_create_panel_widgets(create_state, refresh_after_create=lambda: refresh())

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
        # "Apply & Run LF" mirrors Streamlit's twin-button layout.
        bulk_button_lf = ui.button("Apply && Run LF").props("flat")
        # Disconnect + Delete sit alongside Apply. Disconnect flips
        # ``connected*`` / ``open`` and goes through the change log;
        # Delete is destructive and bypasses the log.
        disconnect_button = ui.button("Disconnect").props("flat")
        delete_button = ui.button("Delete").props("flat color=negative")
    bulk_row.set_visibility(False)

    # Holds the most-recently-rendered DataFrame so the CSV button
    # exports the exact view the user is looking at.
    current_df = {"df": None}

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
            vl_filter.visible = False
            current_df["df"] = None
            return
        try:
            df_full = get_enriched_dataframe(_state.network, label)
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
        df = reorder_columns(df_full, label)
        original_rows = df.shape[0]

        # Filter-by-selected-VL: only meaningful when applicable.
        vl_applicable = (
            label in VL_FILTERABLE
            and _state.selected_vl is not None
        )
        vl_filter.visible = vl_applicable
        if vl_applicable:
            vl_filter.text = f"Filter by VL: {_state.selected_vl}"
            if vl_filter.value:
                df = filter_by_voltage_level(df, _state.selected_vl)
        elif vl_filter.value:
            # Switched to a non-VL-filterable component; auto-uncheck.
            vl_filter.value = False

        cols = [c for c in editable_attributes(label) if c in df.columns]
        # ``FILTERS`` whitelist narrows ag-Grid's per-column filter
        # affordance to the same set Streamlit's expander offers.
        filterable_cols = [c for c in FILTERS.get(label, []) if c in df.columns]
        grid.options = _dataframe_to_aggrid_options(
            df, editable_cols=cols, filterable_cols=filterable_cols,
        )
        grid.update()
        current_df["df"] = df
        editable_msg = " · editable: " + ", ".join(cols) if cols else ""
        if df.empty and original_rows == 0:
            summary.set_text(f"{label}: empty (no rows in this network)")
        elif df.shape[0] < original_rows:
            summary.set_text(
                f"{label}: {df.shape[0]} / {original_rows} rows · "
                f"{df.shape[1]} columns{editable_msg}"
            )
        else:
            summary.set_text(
                f"{label}: {df.shape[0]} rows · {df.shape[1]} columns{editable_msg}"
            )
        # Refresh the bulk-edit attribute combo so it offers only the
        # editable columns for *this* component. The whole panel is
        # visible whenever the component supports any bulk action
        # (edit, disconnect, or delete).
        bulk_attr.options = cols
        bulk_attr.value = cols[0] if cols else None
        bulk_attr.update()
        # Refresh the create panel for the new component.
        _refresh_create_panel(create_state, label)
        is_disconnectable = label in DISCONNECTABLE_COMPONENTS
        is_removable = label in REMOVABLE_COMPONENTS
        bulk_row.set_visibility(bool(cols) or is_disconnectable or is_removable)
        # When the component isn't editable, hide the edit-only inputs
        # so the row reads cleanly as just "Disconnect" / "Delete".
        bulk_label.set_visibility(bool(cols))
        bulk_attr.set_visibility(bool(cols))
        bulk_value.set_visibility(bool(cols))
        bulk_button.set_visibility(bool(cols))
        disconnect_button.set_visibility(is_disconnectable)
        delete_button.set_visibility(is_removable)
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
            prev = apply_cell_edit(
                _state.network, component, str(element_id), col_id, new_value,
            )
        except Exception as exc:
            ui.notify(
                f"Edit rejected — {component}/{element_id}/{col_id}: {exc}",
                type="negative",
            )
            # Refresh to revert the failed edit (cheap; 1 worker call).
            refresh()
            return
        _state.change_log.record(component, str(element_id), col_id, prev, new_value)
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

    async def on_bulk_apply(run_lf_after: bool = False) -> None:
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
        _state.change_log.record_bulk(component, attribute, prev_map, new_value)
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
        # "Apply & Run LF" path: kick off the load flow after the edit.
        # The state listener handles cache flush + diagram refresh.
        if run_lf_after:
            try:
                result = await asyncio.to_thread(_state.run_loadflow)
            except Exception as exc:
                ui.notify(f"Load flow failed: {exc}", type="negative")
                return
            if result and result.converged:
                ui.notify(f"AC load flow: {result.status}", type="positive")
            else:
                ui.notify(
                    f"AC load flow: {result.status if result else 'UNKNOWN'}",
                    type="warning",
                )

    async def on_bulk_disconnect() -> None:
        component = select.value
        if _state.network is None or not component:
            return
        if component not in DISCONNECTABLE_COMPONENTS:
            return
        selected = await grid.get_selected_rows()
        ids = [str(r["id"]) for r in (selected or []) if r.get("id") is not None]
        if not ids:
            ui.notify("Select one or more rows first.", type="warning")
            return
        try:
            per_attr_prev_map = apply_bulk_disconnect(_state.network, component, ids)
        except Exception as exc:
            ui.notify(
                f"Disconnect rejected — {component}/{len(ids)} rows: {exc}",
                type="negative",
            )
            return
        for attribute, prev_map in per_attr_prev_map.items():
            _state.change_log.record_bulk(
                component, attribute, prev_map,
                DISCONNECT_ATTRS[component][attribute],
            )
        ui.notify(
            f"{component}: disconnected {len(ids)} row(s)",
            type="positive",
            timeout=1500,
        )
        # Disconnect always touches a topology-affecting attribute, so
        # flush the diagram caches unconditionally.
        _nad_cache.clear()
        _sld_cache.clear()
        if _state.selected_vl:
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)
        refresh()

    async def on_bulk_delete() -> None:
        component = select.value
        if _state.network is None or not component:
            return
        if component not in REMOVABLE_COMPONENTS:
            return
        selected = await grid.get_selected_rows()
        ids = [str(r["id"]) for r in (selected or []) if r.get("id") is not None]
        if not ids:
            ui.notify("Select one or more rows first.", type="warning")
            return
        # Confirm via Quasar's built-in dialog. ``await`` only resolves
        # after the user clicks one of the buttons.
        with ui.dialog() as confirm, ui.card():
            ui.label(
                f"Permanently remove {len(ids)} {component.lower()} from "
                f"the network?"
            ).classes("text-subtitle1")
            ui.label(
                "Cascades to bay switches, HVDC triples, VL contents — "
                "not reversible by the Change Log."
            ).classes("text-caption")
            with ui.row().classes("justify-end w-full"):
                ui.button("Cancel", on_click=lambda: confirm.submit(False)).props("flat")
                ui.button(
                    "Delete",
                    on_click=lambda: confirm.submit(True),
                ).props("color=negative")
        ok = await confirm
        if not ok:
            return
        # Snapshot the to-be-removed rows for the ChangeLog before the
        # network forgets them.
        try:
            from iidm_viewer.data_view import get_enriched_dataframe
            df_before = get_enriched_dataframe(_state.network, component)
            snapshot_index = (
                df_before.set_index("id", drop=False)
                if "id" in df_before.columns
                else df_before
            )
        except Exception:
            snapshot_index = None
        try:
            removed = remove_elements(_state.network, component, ids)
        except Exception as exc:
            ui.notify(
                f"Delete failed — {component}/{len(ids)} rows: {exc}",
                type="negative",
            )
            return
        # Drop edit-log entries for removed ids (no longer revertable
        # via apply_cell_edit) and record the removal so the panel can
        # display it.
        removed_set = set(map(str, removed))
        for entry in list(_state.change_log.entries(component)):
            if str(entry.get("element_id")) in removed_set:
                try:
                    _state.change_log._entries.remove(entry)
                except ValueError:
                    pass
        _state.change_log.record_removal(component, removed, snapshot=snapshot_index)
        # Deletion always changes topology -> flush diagram caches.
        _nad_cache.clear()
        _sld_cache.clear()
        if _state.selected_vl:
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)
        ui.notify(
            f"{component}: removed {len(removed)} element(s)",
            type="positive",
            timeout=1500,
        )
        refresh()

    def on_csv_clicked() -> None:
        df = current_df["df"]
        if df is None or df.empty:
            ui.notify("Nothing to export — load a network first.", type="info")
            return
        label = select.value or "data"
        ui.download(
            dataframe_to_csv(df),
            filename=f"{label.lower().replace(' ', '_')}.csv",
        )

    download_btn.on_click(on_csv_clicked)
    vl_filter.on_value_change(lambda _e: refresh())
    bulk_button.on_click(lambda: on_bulk_apply(run_lf_after=False))
    bulk_button_lf.on_click(lambda: on_bulk_apply(run_lf_after=True))
    disconnect_button.on_click(on_bulk_disconnect)
    delete_button.on_click(on_bulk_delete)
    grid.on("cellValueChanged", on_cell_changed)
    select.on_value_change(lambda _e: refresh())

    # When the host's selected_vl changes (Map / NAD / SLD navigation),
    # refresh the data tab so the VL-filter widget and its caption
    # stay in sync with the active VL.
    _state.on_selected_vl_changed(lambda _vl: refresh())

    # --- Change log panel -------------------------------------------------
    _build_change_log_panel(refresh)

    return refresh


def _format_log_value(value) -> str:
    import math
    if value is None:
        return "—"
    if isinstance(value, float):
        if math.isnan(value):
            return "—"
        return format(value, ".4g")
    return str(value)


def _build_change_log_panel(on_revert_refresh) -> None:
    """Always-visible Change Log section under the data explorer.

    ``on_revert_refresh`` is the data-grid refresh closure; the panel
    invokes it after any revert so the grid reflects the live network
    state. The panel listens on ``_state.change_log`` so cell + bulk
    edits made elsewhere repaint here without manual wiring.
    """
    log = _state.change_log

    with ui.row().classes("q-pa-sm items-center w-full"):
        title = ui.label("Change Log (0)").classes("text-subtitle1")
        ui.space()
        revert_selected_btn = ui.button("Revert selected")
        revert_all_btn = ui.button("Revert all")
        clear_btn = ui.button("Clear").props("flat")

    # Removal-log label sits between the title row and the edits grid.
    # Always present so its visibility can flip without re-creating
    # widgets; updated by ``repaint`` below.
    removals_html = ui.html("").classes("w-full")
    removals_html.visible = False

    log_grid = ui.aggrid({
        "columnDefs": [
            {"headerName": "Component", "field": "component", "sortable": True, "filter": True, "floatingFilter": True},
            {"headerName": "Element", "field": "element_id", "sortable": True, "filter": True, "floatingFilter": True},
            {"headerName": "Property", "field": "property", "sortable": True, "filter": True, "floatingFilter": True},
            {"headerName": "Before", "field": "before"},
            {"headerName": "After", "field": "after"},
        ],
        "rowData": [],
        "rowSelection": "multiple",
        "defaultColDef": {"resizable": True},
    }).classes("w-full").style("height: 180px")

    def repaint() -> None:
        rows = [
            {
                "component": e.get("component", ""),
                "element_id": e.get("element_id", ""),
                "property": e.get("property", ""),
                "before": _format_log_value(e.get("before")),
                "after": _format_log_value(e.get("after")),
                # Hidden — used by revert handlers to recover the
                # original entry.
                "_idx": i,
            }
            for i, e in enumerate(log.entries())
        ]
        log_grid.options["rowData"] = rows
        log_grid.update()
        n_edits = len(log.entries())
        removals = log.removals()
        if removals:
            title.set_text(f"Change Log ({n_edits} edits · {len(removals)} removed)")
            # Group removals by component for a compact red line.
            from collections import defaultdict
            by_comp: dict[str, list[str]] = defaultdict(list)
            for r in removals:
                by_comp[str(r.get("component", ""))].append(str(r.get("element_id", "")))
            parts = []
            for component, ids in by_comp.items():
                shown = ", ".join(ids[:5])
                more = f" (+{len(ids) - 5} more)" if len(ids) > 5 else ""
                parts.append(f"<b>{component}</b>: {shown}{more}")
            removals_html.content = (
                "<div style='color:#b30000;padding:4px 8px;'>"
                "Removed — " + " · ".join(parts) + "</div>"
            )
            removals_html.visible = True
        else:
            title.set_text(f"Change Log ({n_edits})")
            removals_html.visible = False
        has_edits = n_edits > 0 and _state.network is not None
        has_anything = (n_edits + len(removals)) > 0
        revert_all_btn.set_enabled(has_edits)
        revert_selected_btn.set_enabled(has_edits)
        clear_btn.set_enabled(has_anything)

    log.on_changed(repaint)

    async def revert_selected() -> None:
        if _state.network is None:
            return
        selected_rows = await log_grid.get_selected_rows()
        # ``_idx`` is the row's position in ``log.entries()`` at repaint
        # time; mapping back to live entries is the only robust path.
        live = log.entries()
        touched: list[tuple[str, str]] = []
        skipped: list[str] = []
        for sr in selected_rows or []:
            idx = sr.get("_idx")
            if idx is None or idx >= len(live):
                continue
            entry = live[idx]
            try:
                log.revert(_state.network, entry)
            except Exception as exc:
                skipped.append(
                    f"{entry.get('component', '')}/{entry.get('element_id', '')}/"
                    f"{entry.get('property', '')}: {exc}"
                )
                continue
            touched.append((str(entry.get("component", "")), str(entry.get("property", ""))))
        if skipped:
            ui.notify("Some entries could not be reverted: " + "; ".join(skipped[:3]),
                      type="warning")
        if touched:
            _after_revert(touched, on_revert_refresh)

    async def revert_all_clicked() -> None:
        if _state.network is None or len(log) == 0:
            return
        targets = [(str(e.get("component", "")), str(e.get("property", ""))) for e in log.entries()]
        reverted, skipped = log.revert_all(_state.network)
        if skipped:
            ui.notify(
                f"Reverted {reverted}; {len(skipped)} skipped (no original value)",
                type="warning",
            )
        elif reverted:
            ui.notify(f"Reverted {reverted} entries", type="positive")
        if reverted:
            _after_revert(targets[:reverted], on_revert_refresh)

    def clear_clicked() -> None:
        if len(log) == 0:
            return
        log.clear()
        ui.notify("Change log cleared (network edits stay applied).", type="info")

    revert_selected_btn.on_click(revert_selected)
    revert_all_btn.on_click(revert_all_clicked)
    clear_btn.on_click(clear_clicked)

    repaint()


def _after_revert(touched, refresh_data_grid) -> None:
    """Post-revert: invalidate diagram caches for topology-affecting
    attributes and refresh the data grid so the current view reflects
    the reverted network state.
    """
    if any(attr in TOPOLOGY_AFFECTING_ATTRIBUTES for _, attr in touched):
        _nad_cache.clear()
        _sld_cache.clear()
        if _state.selected_vl:
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)
    refresh_data_grid()


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

        # AC load-flow trigger — disabled until a network is loaded;
        # status appears next to it via ui.notify when the run returns.
        run_lf_btn = ui.button("Run AC Load Flow").props("flat dense")
        run_lf_btn.set_enabled(False)
        lf_status_lbl = ui.label("").classes("text-caption q-ml-sm")

        async def on_run_lf() -> None:
            if _state.network is None:
                return
            lf_status_lbl.set_text("Running…")
            try:
                result = await asyncio.to_thread(_state.run_loadflow)
            except Exception as exc:
                lf_status_lbl.set_text(f"Failed: {exc}")
                ui.notify(f"Load flow failed: {exc}", type="negative")
                return
            status = result.status if result else "UNKNOWN"
            lf_status_lbl.set_text(f"LF: {status}")
            if result and result.converged:
                ui.notify(f"AC load flow: {status}", type="positive")
            else:
                ui.notify(f"AC load flow: {status}", type="warning")

        run_lf_btn.on_click(on_run_lf)

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
        # Enable the Run-LF button + reset status whenever the network
        # changes (load / clear).
        run_lf_btn.set_enabled(network is not None)
        lf_status_lbl.set_text("")
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

    def _on_loadflow_completed(result):
        """LF rewrites line P/Q/I + bus V/angle, baked into the SVGs and
        the enriched DataFrames. Flush the diagram caches and refresh
        whichever VL is active + the data grid.
        """
        _nad_cache.clear()
        _sld_cache.clear()
        if _state.selected_vl:
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)
        refresh_data_grid()

    # Listeners are registered fresh on every page connect; if a
    # previous registration is still around (browser refresh), the
    # old one calls into a stale `tabs` and would noop on a closed
    # client. NiceGUI is forgiving here for a single-user prototype.
    _state.on_network_changed(_on_state_network)
    _state.on_selected_vl_changed(_on_state_vl)
    _state.on_loadflow_completed(_on_loadflow_completed)

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
        elif component == "sld" and value.get("type") == "sld-breaker-click":
            _handle_sld_breaker_click(value)
        elif component == "sld" and value.get("type") == "sld-feeder-click":
            _handle_sld_feeder_click(value, tabs, map_tab)

    ui.on("iidm-component-ready", _on_component_ready)
    ui.on("iidm-component-value", _on_component_value)

    # If a network was loaded before this client connected (e.g. via
    # a CLI ``initial_file``), seed the just-built UI from current
    # state right away.
    if _state.network is not None:
        file_lbl.set_text("(pre-loaded)")
        run_lf_btn.set_enabled(True)
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
