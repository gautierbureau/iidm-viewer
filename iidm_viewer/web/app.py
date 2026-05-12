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
from typing import Any, Optional

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
    CREATABLE_BRANCHES,
    CREATABLE_COMPONENTS,
    CREATABLE_CONTAINERS,
    CREATABLE_HVDC_LINES,
    CREATABLE_TAP_CHANGERS,
    LOCATOR_FIELDS,
    OPERATIONAL_LIMIT_SIDES,
    OPERATIONAL_LIMIT_TYPES,
    OPERATIONAL_LIMITS_TARGETS,
    PERMANENT_DURATION,
    REACTIVE_LIMITS_MODES,
    REACTIVE_LIMITS_TARGETS,
    branch_side_locator_fields,
    coerce_field_values,
    create_branch_bay,
    create_component_bay,
    create_container,
    create_coupling_device,
    create_hvdc_line,
    create_operational_limits,
    create_reactive_limits,
    create_secondary_voltage_control,
    create_tap_changer,
    list_bus_ids,
    list_busbar_sections,
    list_converter_stations,
    list_node_breaker_voltage_levels,
    list_node_breaker_vls_with_multi_bbs,
    list_operational_limit_candidates,
    list_reactive_limit_candidates,
    list_substations_df,
    list_transformers_without_tap_changer,
    list_unit_candidates,
    next_free_node,
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
# Latest render payload per component. Two purposes: (a) queue the
# initial render when the iframe hasn't booted yet; (b) re-send when
# Quasar destroys + remounts the panel on tab switch (q-tab-panels
# defaults to ``keep-alive=false``), so the new iframe gets the
# diagram back instead of staying blank.
_last_map: Optional[dict] = None
_last_nad: Optional[dict] = None
_last_sld: Optional[dict] = None

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
from iidm_viewer.lf_parameters_schema import (
    coerce_provider_value,
    filter_changed_generic_params,
    filter_changed_provider_params,
    group_provider_params_by_category,
    parse_provider_options,
)
from iidm_viewer.io_options_schema import (
    csv_split as _csv_split,
    filter_changed_params,
    get_format_parameters,
    get_import_formats,
    get_import_post_processors,
    parse_possible_values,
)
from iidm_viewer.network_loader import (
    export_network,
    get_export_formats,
    guess_mime_for_export,
)
from iidm_viewer.lf_report import (
    SEVERITY_LEVELS,
    SEVERITY_ORDER,
    parse_report_to_tree,
)
from iidm_viewer.loadflow import (
    GENERIC_PARAMETERS,
    get_provider_parameters_df,
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
    global _last_map, _map_data_version
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
    _last_map = args
    if _map_ready:
        _send_render("map", args)


def _push_sld(vl_id: str) -> None:
    global _last_sld
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
    _last_sld = args
    if _sld_ready:
        _send_render("sld", args)


def _push_map_flyto(substation_id: str, zoom: float = 11) -> None:
    """Tell the map iframe to fly to ``substation_id`` (if known)."""
    import time
    global _last_map
    args = {
        "version": _map_data_version,
        "height": 670,
        "flyTo": {
            "substationId": substation_id,
            "zoom": zoom,
            "ts": int(time.monotonic() * 1000),
        },
    }
    # Merge the flyTo into the latest map args so a re-mount (after tab
    # switch) gets both the topology *and* the latest fly target.
    _last_map = dict(_last_map or {}, **args)
    if _map_ready:
        _send_render("map", args)


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
    global _last_nad
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
    _last_nad = args
    if _nad_ready:
        _send_render("nad", args)


def _open_lf_report_dialog(report_json: Optional[str]) -> None:
    """Open a modal showing the parsed LoadFlow report tree.

    Parsing — message-template interpolation, severity filter, and the
    "expand subtrees containing WARN/ERROR" heuristic — lives in
    :mod:`iidm_viewer.lf_report` so all three prototypes share it.
    The dialog rebuilds the tree on every severity-filter change.
    """
    if not report_json:
        ui.notify(
            "No load flow report available. Run a load flow first.",
            type="warning",
        )
        return

    severity_state: dict = {
        "selected": ["INFO", "WARN", "ERROR"],
    }

    with ui.dialog() as dialog, ui.card().style("min-width: 720px; max-width: 95vw"):
        ui.label("Load Flow Logs").classes("text-h6")
        ui.label(
            "Filter by severity. Subtrees containing a WARN or ERROR open by default."
        ).classes("text-caption q-mb-sm")
        sev_select = ui.select(
            options=list(SEVERITY_LEVELS),
            value=list(severity_state["selected"]),
            multiple=True,
            label="Show",
        ).props("dense outlined use-chips").classes("full-width q-mb-md")

        tree_container = ui.column().classes("full-width")

        def _build_q_tree_nodes(nodes: list[dict], prefix: str = "n") -> list[dict]:
            """Translate ``parse_report_to_tree`` output into the shape
            ``ui.tree`` expects (``id`` / ``label`` / ``children``)."""
            out: list[dict] = []
            for i, node in enumerate(nodes):
                node_id = f"{prefix}_{i}"
                label = (
                    f"{node['icon']} {node['message']}"
                    if node["icon"]
                    else node["message"]
                )
                out.append({
                    "id": node_id,
                    "label": label,
                    "expanded_default": node["expanded"],
                    "children": _build_q_tree_nodes(node["children"], node_id),
                })
            return out

        def _collect_expanded(nodes: list[dict], acc: list[str]) -> list[str]:
            for node in nodes:
                if node["expanded_default"] and node["children"]:
                    acc.append(node["id"])
                _collect_expanded(node["children"], acc)
            return acc

        def _rebuild_tree() -> None:
            tree_container.clear()
            selected = sev_select.value or []
            if not selected:
                with tree_container:
                    ui.label("Select at least one severity level.") \
                        .classes("text-caption")
                return
            min_severity = min(selected, key=lambda s: SEVERITY_ORDER.get(s, 2))
            try:
                nodes = parse_report_to_tree(report_json, min_severity=min_severity)
            except ValueError as exc:
                with tree_container:
                    ui.label(f"Failed to parse report: {exc}") \
                        .classes("text-caption text-negative")
                return
            if not nodes:
                with tree_container:
                    ui.label("No log entries match the selected severity filter.") \
                        .classes("text-caption")
                return
            q_nodes = _build_q_tree_nodes(nodes)
            expanded = _collect_expanded(q_nodes, [])
            with tree_container:
                ui.tree(q_nodes, label_key="label", node_key="id") \
                    .expand(expanded)

        sev_select.on("update:model-value", lambda *_: _rebuild_tree())
        _rebuild_tree()

        with ui.row().classes("full-width justify-end q-mt-md"):
            ui.button("Close", on_click=dialog.close).props("flat")

    dialog.open()


def _open_lf_parameters_dialog(on_save) -> None:
    """Open the "Load Flow Parameters" modal.

    Two tabs: **Generic** (from the shared
    :data:`GENERIC_PARAMETERS` schema) and **OpenLoadFlow** (from
    pypowsybl's ``get_provider_parameters()`` descriptor). On Save the
    trimmed-to-changed dicts are passed to ``on_save(generic_dict,
    provider_dict)`` — the host then writes them onto AppState so the
    next ``run_loadflow`` picks them up.
    """
    generic_overrides = dict(_state.lf_generic_params or {})
    provider_overrides = dict(_state.lf_provider_params or {})

    try:
        provider_df = get_provider_parameters_df()
    except Exception:
        provider_df = None

    generic_widgets: dict[str, Any] = {}
    provider_widgets: dict[str, tuple[str, Any]] = {}

    with ui.dialog() as dialog, ui.card().style(
        "min-width: 720px; max-width: 95vw; max-height: 90vh",
    ):
        ui.label("Load Flow Parameters").classes("text-h6")
        with ui.tabs().classes("w-full") as param_tabs:
            generic_tab = ui.tab("Generic Parameters")
            provider_tab = ui.tab("OpenLoadFlow Parameters")
        with ui.tab_panels(param_tabs, value=generic_tab) \
                .classes("w-full").style("max-height: 60vh; overflow: auto"):
            with ui.tab_panel(generic_tab):
                for param_def in GENERIC_PARAMETERS:
                    name, ptype, default, desc = (
                        param_def[0], param_def[1], param_def[2], param_def[3],
                    )
                    current = generic_overrides.get(name, default)
                    with ui.row().classes("items-center w-full"):
                        ui.label(desc).classes("w-1/2 text-caption")
                        if ptype == "bool":
                            w = ui.switch(value=bool(current))
                        elif ptype == "enum":
                            options = list(param_def[4])
                            w = ui.select(
                                options=options,
                                value=str(current) if str(current) in options else options[0],
                            ).props("dense outlined").classes("w-1/2")
                        elif ptype == "float":
                            try:
                                cur_float = float(current)
                            except (TypeError, ValueError):
                                cur_float = float(default)
                            w = ui.number(value=cur_float, format="%g") \
                                .props("dense outlined").classes("w-1/2")
                        else:
                            w = ui.input(value=str(current)) \
                                .props("dense outlined").classes("w-1/2")
                        generic_widgets[name] = (ptype, w)

            with ui.tab_panel(provider_tab):
                if provider_df is None or provider_df.empty:
                    ui.label("Provider parameters unavailable.") \
                        .classes("text-caption")
                else:
                    for category, rows in group_provider_params_by_category(provider_df):
                        with ui.expansion(category).classes("w-full"):
                            for name, row in rows.iterrows():
                                ptype = row["type"]
                                default = row["default"]
                                desc = row.get("description", "")
                                current = provider_overrides.get(name, default)
                                with ui.row().classes("items-center w-full"):
                                    lbl = ui.label(name).classes("w-1/3 text-caption")
                                    if desc:
                                        lbl.tooltip(desc)
                                    if ptype == "BOOLEAN":
                                        w = ui.switch(
                                            value=coerce_provider_value(
                                                ptype, current, default,
                                            ),
                                        )
                                    elif ptype == "INTEGER":
                                        w = ui.number(
                                            value=coerce_provider_value(
                                                ptype, current, default,
                                            ),
                                            step=1, format="%d",
                                        ).props("dense outlined").classes("w-1/2")
                                    elif ptype == "DOUBLE":
                                        w = ui.number(
                                            value=coerce_provider_value(
                                                ptype, current, default,
                                            ),
                                            format="%g",
                                        ).props("dense outlined").classes("w-1/2")
                                    elif ptype == "STRING":
                                        options = parse_provider_options(
                                            row.get("possible_values"),
                                        )
                                        if options:
                                            value = (
                                                str(current) if str(current) in options
                                                else options[0]
                                            )
                                            w = ui.select(
                                                options=options, value=value,
                                            ).props("dense outlined").classes("w-1/2")
                                        else:
                                            w = ui.input(
                                                value="" if current is None
                                                else str(current),
                                            ).props("dense outlined").classes("w-1/2")
                                    else:
                                        w = ui.input(
                                            value="" if current is None
                                            else str(current),
                                        ).props("dense outlined").classes("w-1/2")
                                    if desc:
                                        w.tooltip(desc)
                                    provider_widgets[name] = (ptype, w)

        with ui.row().classes("w-full justify-end q-mt-md"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            def _on_save_click() -> None:
                generic_raw = {
                    name: w.value for name, (_pt, w) in generic_widgets.items()
                }
                generic = filter_changed_generic_params(generic_raw)
                provider_raw = {
                    name: coerce_provider_value(ptype, w.value)
                    for name, (ptype, w) in provider_widgets.items()
                }
                provider = filter_changed_provider_params(provider_raw, provider_df)
                on_save(generic, provider)
                ui.notify("Load Flow parameters updated.", type="positive", timeout=1500)
                dialog.close()

            ui.button("Save", on_click=_on_save_click).props("color=primary")

    dialog.open()


def _render_params_form(container, df, initial: Optional[dict] = None):
    """Build NiceGUI inputs for an import/export parameters DataFrame.

    Each row gets a typed widget (switch for BOOLEAN, number for
    INTEGER / DOUBLE, multi-select for STRING_LIST with options, plain
    select for enumerated STRING, free input otherwise). Returns a
    callable ``read_values()`` that produces the
    ``{name: wire-string}`` dict matching pypowsybl's parameter shape.
    """
    seed = dict(initial or {})
    widgets: dict[str, tuple[str, Any]] = {}
    container.clear()
    with container:
        if df is None or df.empty:
            ui.label("No configurable options for this format.") \
                .classes("text-caption")

            def _empty_read() -> dict[str, str]:
                return {}

            return _empty_read

        for name, row in df.iterrows():
            ptype = str(row.get("type") or "STRING").upper()
            default = row.get("default") if "default" in df.columns else ""
            desc = str(row.get("description") or name)
            options = parse_possible_values(row.get("possible_values"))
            current = seed.get(str(name), default if default is not None else "")
            with ui.row().classes("items-center w-full"):
                lbl = ui.label(desc).classes("w-1/2 text-caption")
                lbl.tooltip(str(name))
                if ptype == "STRING_LIST" and options:
                    selected = [v for v in _csv_split(current) if v in options]
                    w = ui.select(
                        options=options, value=selected, multiple=True,
                    ).props("dense outlined use-chips").classes("w-1/2")
                elif options:
                    val = str(current) if str(current) in options else (
                        options[0] if options else ""
                    )
                    w = ui.select(options=options, value=val) \
                        .props("dense outlined").classes("w-1/2")
                elif ptype == "BOOLEAN":
                    bv = str(current).strip().lower() in ("true", "1", "yes", "on")
                    w = ui.switch(value=bv)
                elif ptype == "INTEGER":
                    try:
                        iv = int(float(current))
                    except (TypeError, ValueError):
                        try:
                            iv = int(float(default))
                        except (TypeError, ValueError):
                            iv = 0
                    w = ui.number(value=iv, step=1, format="%d") \
                        .props("dense outlined").classes("w-1/2")
                elif ptype in ("DOUBLE", "FLOAT"):
                    try:
                        fv = float(current)
                    except (TypeError, ValueError):
                        try:
                            fv = float(default)
                        except (TypeError, ValueError):
                            fv = 0.0
                    w = ui.number(value=fv, format="%g") \
                        .props("dense outlined").classes("w-1/2")
                else:
                    w = ui.input(value="" if current is None else str(current)) \
                        .props("dense outlined").classes("w-1/2")
                widgets[str(name)] = (ptype, w)

    def _read_values() -> dict[str, str]:
        out: dict[str, str] = {}
        for name, (ptype, w) in widgets.items():
            v = w.value
            if isinstance(v, list):
                out[name] = ",".join(str(x) for x in v)
            elif isinstance(v, bool):
                out[name] = "true" if v else "false"
            elif v is None:
                out[name] = ""
            else:
                out[name] = str(v)
        return out

    return _read_values


def _open_load_options_dialog() -> None:
    """Modal editor for the next file load's import options.

    Mirrors Streamlit's "Import options…" dialog: format selector
    (``Auto-detect`` + pypowsybl's import list), format-specific
    parameters rebuilt on every format change, and a post-processors
    checklist. On Save the trimmed dicts land on
    ``_state.import_format`` / ``_state.import_params`` /
    ``_state.import_post_processors`` so the next upload picks them up.
    """
    try:
        formats = get_import_formats()
    except Exception as exc:
        ui.notify(f"Failed to list import formats: {exc}", type="negative")
        return
    try:
        post_processors = get_import_post_processors()
    except Exception:
        post_processors = []

    auto = "Auto-detect"
    current_fmt_raw = _state.import_format or auto
    options = [auto] + list(formats)

    params_state: dict = {"read_values": lambda: {}}

    with ui.dialog() as dialog, ui.card().style(
        "min-width: 640px; max-width: 95vw; max-height: 90vh",
    ):
        ui.label("Import options").classes("text-h6")
        ui.label(
            'Configure how the next file is parsed. "Auto-detect" lets '
            "pypowsybl pick the format from the file extension."
        ).classes("text-caption q-mb-sm")
        with ui.row().classes("items-center w-full"):
            ui.label("Import format").classes("w-1/3 text-caption")
            fmt_select = ui.select(
                options=options,
                value=current_fmt_raw if current_fmt_raw in options else auto,
            ).props("dense outlined").classes("w-2/3")

        params_box = ui.expansion("Format parameters", value=True) \
            .classes("w-full")
        params_container = ui.column().classes("w-full")
        params_box.add(params_container)

        def _rebuild_params() -> None:
            fmt = fmt_select.value
            if fmt == auto or not fmt:
                params_container.clear()
                params_box.visible = False
                params_state["read_values"] = lambda: {}
                params_state["df"] = None
                return
            try:
                df = get_format_parameters("import", fmt)
            except Exception:
                df = None
            params_state["df"] = df
            seed = _state.import_params if (
                fmt == _state.import_format
            ) else {}
            params_state["read_values"] = _render_params_form(
                params_container, df, seed,
            )
            params_box.visible = True

        fmt_select.on("update:model-value", lambda *_: _rebuild_params())
        _rebuild_params()

        pp_box = ui.expansion("Post-processors", value=False).classes("w-full")
        pp_switches: dict[str, Any] = {}
        with pp_box:
            if not post_processors:
                ui.label("No post-processors reported.") \
                    .classes("text-caption")
            else:
                checked = set(_state.import_post_processors or [])
                for pp in post_processors:
                    pp_switches[pp] = ui.checkbox(pp, value=pp in checked)

        def _on_save_click() -> None:
            fmt = fmt_select.value
            _state.import_format = None if fmt == auto else fmt
            raw = params_state["read_values"]()
            df = params_state.get("df")
            if df is not None:
                _state.import_params = filter_changed_params(raw, df)
            else:
                _state.import_params = {}
            _state.import_post_processors = [
                name for name, sw in pp_switches.items() if sw.value
            ]
            ui.notify(
                f"Import options updated — format: "
                f"{_state.import_format or 'auto-detect'}, "
                f"{len(_state.import_params)} param override(s), "
                f"{len(_state.import_post_processors)} post-processor(s).",
                type="positive", timeout=2000,
            )
            dialog.close()

        with ui.row().classes("w-full justify-end q-mt-md"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=_on_save_click).props("color=primary")

    dialog.open()


def _open_save_network_dialog() -> None:
    """Open the "Save network" modal — Streamlit-style.

    Two-step flow that matches the existing Streamlit dialog:

    1. The user picks an export format from pypowsybl's list (XIIDM
       by default).
    2. They click "Download" — the dialog runs the shared
       :func:`iidm_viewer.network_loader.export_network` and streams
       the resulting bytes back to the browser via ``ui.download``.

    The export call runs through pypowsybl's worker thread, so the
    UI stays responsive while the bytes are being produced.
    """
    if _state.network is None:
        ui.notify("No network loaded.", type="warning")
        return
    try:
        formats = get_export_formats()
    except Exception as exc:
        ui.notify(f"Failed to list formats: {exc}", type="negative")
        return
    if not formats:
        ui.notify("No export formats available.", type="warning")
        return

    default_fmt = "XIIDM" if "XIIDM" in formats else formats[0]
    status_state: dict = {"label": None}
    # Format-specific parameter form — rebuilt every time the format
    # changes. ``params_state['read_values']`` returns the current
    # widget values; the host trims to overrides before exporting.
    params_state: dict = {"read_values": lambda: {}, "df": None}

    with ui.dialog() as dialog, ui.card().style(
        "min-width: 560px; max-width: 95vw; max-height: 90vh",
    ):
        ui.label("Save network").classes("text-h6")
        ui.label("Pick an export format:").classes("text-caption")
        fmt_select = ui.select(
            options=list(formats),
            value=default_fmt,
        ).props("dense outlined").classes("full-width q-mb-md")
        params_box = ui.expansion("Export parameters", value=False) \
            .classes("w-full")
        params_container = ui.column().classes("w-full")
        params_box.add(params_container)

        def _rebuild_params() -> None:
            fmt = fmt_select.value
            if not fmt:
                params_container.clear()
                params_box.visible = False
                params_state["read_values"] = lambda: {}
                params_state["df"] = None
                return
            try:
                df = get_format_parameters("export", fmt)
            except Exception:
                df = None
            params_state["df"] = df
            params_state["read_values"] = _render_params_form(
                params_container, df,
            )
            params_box.visible = True

        fmt_select.on("update:model-value", lambda *_: _rebuild_params())
        _rebuild_params()

        status_state["label"] = ui.label("").classes("text-caption q-mb-sm")

        async def _on_download_click() -> None:
            import asyncio

            fmt = fmt_select.value
            if not fmt or _state.network is None:
                return
            raw_params = params_state["read_values"]()
            df = params_state.get("df")
            params = filter_changed_params(raw_params, df) if df is not None else {}
            status_state["label"].set_text(f"Exporting to {fmt}…")
            try:
                data, ext = await asyncio.to_thread(
                    export_network, _state.network, fmt, params or None,
                )
            except Exception as exc:
                status_state["label"].set_text(f"Export failed: {exc}")
                ui.notify(f"Export failed: {exc}", type="negative")
                return
            mime = guess_mime_for_export(data)
            ui.download.content(
                data,
                filename=f"network.{ext.lower()}",
                media_type=mime,
            )
            status_state["label"].set_text(
                f"Downloaded network.{ext.lower()} ({len(data):,} bytes).",
            )
            ui.notify(
                f"Downloaded network.{ext.lower()}",
                type="positive", timeout=1500,
            )

        with ui.row().classes("full-width justify-end q-mt-md"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Download", on_click=_on_download_click).props("color=primary")

    dialog.open()


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


def _build_create_branch_panel_widgets(state: dict, refresh_after_create) -> None:
    """Materialise the "Create a new branch" expansion (Lines / 2W Transformers).

    Mirrors :func:`_build_create_panel_widgets` but lays out two side
    pickers (VL + busbar) instead of one, and dispatches through
    :func:`create_branch_bay` rather than ``create_component_bay``.
    """
    expansion = ui.expansion("Create a new branch", icon="cable").classes("w-full")
    expansion.visible = False
    state["expansion"] = expansion
    with expansion:
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Side 1 — VL:")
            vl1 = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-56")
            ui.label("Busbar:")
            bbs1 = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-40")
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Side 2 — VL:")
            vl2 = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-56")
            ui.label("Busbar:")
            bbs2 = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-40")
        fields_container = ui.row().classes("items-start w-full q-pa-sm flex-wrap")
        with ui.row().classes("items-center w-full q-pa-sm"):
            create_btn = ui.button("Create", icon="add_circle")
            status_label = ui.label("").classes("text-caption q-ml-md")

    state["vl1_select"] = vl1
    state["vl2_select"] = vl2
    state["bbs1_select"] = bbs1
    state["bbs2_select"] = bbs2
    state["fields_container"] = fields_container
    state["status_label"] = status_label
    state["create_btn"] = create_btn
    state["field_widgets"] = {}

    def _on_side_vl_change(side: int) -> None:
        vl_sel = vl1 if side == 1 else vl2
        bbs_sel = bbs1 if side == 1 else bbs2
        vl_id = vl_sel.value
        if not vl_id or _state.network is None:
            bbs_sel.options = []
            bbs_sel.value = None
            bbs_sel.update()
            return
        try:
            ids = list_busbar_sections(_state.network, str(vl_id))
        except Exception:
            ids = []
        bbs_sel.options = ids
        bbs_sel.value = ids[0] if ids else None
        bbs_sel.update()

    vl1.on_value_change(lambda _e: _on_side_vl_change(1))
    vl2.on_value_change(lambda _e: _on_side_vl_change(2))

    def _on_create_click() -> None:
        component = state.get("current_component")
        if not component or component not in CREATABLE_BRANCHES or _state.network is None:
            return
        if not bbs1.value or not bbs2.value:
            status_label.set_text("Pick a busbar section on both sides first.")
            return
        spec = CREATABLE_BRANCHES[component]
        all_fields = (
            list(spec["fields"])
            + list(branch_side_locator_fields(1))
            + list(branch_side_locator_fields(2))
        )
        raw = {f["name"]: _read_create_widget(state, f) for f in all_fields}
        values = coerce_field_values(all_fields, raw)
        values["bus_or_busbar_section_id_1"] = str(bbs1.value)
        values["bus_or_busbar_section_id_2"] = str(bbs2.value)
        try:
            create_branch_bay(_state.network, component, values)
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


def _refresh_create_branch_panel(state: dict, component: str) -> None:
    """Repopulate the branch-creation panel for ``component``.

    Hides the whole expansion when the component isn't a creatable
    branch or the network has no node-breaker VLs.
    """
    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if component not in CREATABLE_BRANCHES or _state.network is None:
        expansion.visible = False
        return

    try:
        vls = list_node_breaker_voltage_levels(_state.network)
    except Exception:
        vls = None
    vl_options = (
        {str(row["id"]): str(row["display"]) for _, row in vls.iterrows()}
        if vls is not None and not vls.empty else {}
    )
    if not vl_options:
        expansion.visible = False
        return
    items = list(vl_options.keys())
    for side, sel in (("1", state["vl1_select"]), ("2", state["vl2_select"])):
        sel.options = vl_options
        sel.value = items[0] if side == "1" else (items[1] if len(items) > 1 else items[0])
        sel.update()
    # Sync busbar combos to match the now-selected VLs.
    for sel_vl, sel_bbs in (
        (state["vl1_select"], state["bbs1_select"]),
        (state["vl2_select"], state["bbs2_select"]),
    ):
        try:
            ids = list_busbar_sections(_state.network, str(sel_vl.value))
        except Exception:
            ids = []
        sel_bbs.options = ids
        sel_bbs.value = ids[0] if ids else None
        sel_bbs.update()

    container = state["fields_container"]
    container.clear()
    state["field_widgets"] = {}
    spec = CREATABLE_BRANCHES[component]
    all_fields = (
        list(spec["fields"])
        + list(branch_side_locator_fields(1))
        + list(branch_side_locator_fields(2))
    )
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


def _build_create_container_panel_widgets(state: dict, refresh_after_create) -> None:
    """Materialise the "Create a new container" expansion.

    Substations / Voltage Levels / Busbar Sections share this panel.
    The "context" picker on top varies:

    * Substations         — no picker (top-level objects).
    * Voltage Levels      — optional substation picker.
    * Busbar Sections     — required node-breaker VL picker; the
      ``node`` default updates to ``next_free_node`` when the picker
      changes.
    """
    expansion = ui.expansion("Create a new container", icon="apartment").classes("w-full")
    expansion.visible = False
    state["expansion"] = expansion
    with expansion:
        picker_row = ui.row().classes("items-center w-full q-pa-sm")
        with picker_row:
            context_label = ui.label("")
            context_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
        picker_row.visible = False
        fields_container = ui.row().classes("items-start w-full q-pa-sm flex-wrap")
        with ui.row().classes("items-center w-full q-pa-sm"):
            create_btn = ui.button("Create", icon="add_circle")
            status_label = ui.label("").classes("text-caption q-ml-md")

    state["picker_row"] = picker_row
    state["context_select"] = context_select
    state["context_label"] = context_label
    state["fields_container"] = fields_container
    state["status_label"] = status_label
    state["create_btn"] = create_btn
    state["field_widgets"] = {}

    def _on_context_change(_e=None) -> None:
        # For Busbar Sections, refresh the ``node`` default to the next
        # free node in the freshly-picked VL.
        if state.get("current_component") != "Busbar Sections":
            return
        vl_id = context_select.value
        if not vl_id or _state.network is None:
            return
        try:
            suggested = next_free_node(_state.network, str(vl_id))
        except Exception:
            return
        node_w = state["field_widgets"].get("node")
        if node_w is not None:
            node_w.value = int(suggested)
            node_w.update()

    context_select.on_value_change(_on_context_change)

    def _on_create_click() -> None:
        component = state.get("current_component")
        if not component or component not in CREATABLE_CONTAINERS or _state.network is None:
            return
        spec = CREATABLE_CONTAINERS[component]
        raw = {f["name"]: _read_create_widget(state, f) for f in spec["fields"]}
        values = coerce_field_values(spec["fields"], raw)
        # Inject context from the picker.
        if component == "Voltage Levels":
            sub_id = context_select.value
            if sub_id:
                values["substation_id"] = str(sub_id)
        elif component == "Busbar Sections":
            vl_id = context_select.value
            if not vl_id:
                status_label.set_text("Pick a voltage level first.")
                return
            values["voltage_level_id"] = str(vl_id)
        try:
            create_container(_state.network, component, values)
        except Exception as exc:
            status_label.set_text(f"Create failed — {exc}")
            ui.notify(f"Create failed: {exc}", type="negative")
            return
        created_id = str(values.get("id") or "")
        status_label.set_text(f"Created {component.rstrip('s')} {created_id!r}.")
        ui.notify(f"Created {component.rstrip('s')} {created_id!r}",
                  type="positive", timeout=1500)
        # Topology changed (or geography for substations) -> flush diagram caches.
        _nad_cache.clear()
        _sld_cache.clear()
        if _state.selected_vl:
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)
        refresh_after_create()

    create_btn.on_click(_on_create_click)


def _refresh_create_container_panel(state: dict, component: str) -> None:
    """Repopulate the container-creation panel for ``component``.

    Hides the whole expansion when the component isn't a creatable
    container.
    """
    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if component not in CREATABLE_CONTAINERS or _state.network is None:
        expansion.visible = False
        return

    # Configure the context picker per component.
    picker_row = state["picker_row"]
    sel = state["context_select"]
    label = state["context_label"]
    if component == "Substations":
        picker_row.visible = False
    elif component == "Voltage Levels":
        label.set_text("Substation (optional):")
        try:
            subs = list_substations_df(_state.network)
        except Exception:
            subs = None
        options = {"": "(none — no substation)"}
        if subs is not None and not subs.empty:
            for _, row in subs.iterrows():
                options[str(row["id"])] = str(row["display"])
        sel.options = options
        sel.value = ""
        sel.update()
        picker_row.visible = True
    elif component == "Busbar Sections":
        label.set_text("Voltage level:")
        try:
            vls = list_node_breaker_voltage_levels(_state.network)
        except Exception:
            vls = None
        if vls is None or vls.empty:
            sel.options = {}
            sel.value = None
            sel.update()
            picker_row.visible = True
            ui.notify(
                "No node-breaker VLs — Busbar Sections can't be created.",
                type="info", timeout=2000,
            )
        else:
            options = {
                str(row["id"]): f"{row['display']} ({row['nominal_v']:.0f} kV)"
                for _, row in vls.iterrows()
            }
            sel.options = options
            sel.value = next(iter(options))
            sel.update()
            picker_row.visible = True

    # Rebuild the field widgets.
    container = state["fields_container"]
    container.clear()
    state["field_widgets"] = {}
    spec = CREATABLE_CONTAINERS[component]
    fields = list(spec["fields"])
    suggested_node: Optional[int] = None
    if component == "Busbar Sections" and sel.value:
        try:
            suggested_node = next_free_node(_state.network, str(sel.value))
        except Exception:
            suggested_node = None
    with container:
        for f in fields:
            label_text = f["label"] + (" *" if f.get("required") else "")
            help_text = f.get("help") or ""
            with ui.column().classes("q-mr-md q-mb-md"):
                ui.label(label_text).classes("text-caption")
                kind = f["kind"]
                default = f.get("default")
                if f["name"] == "node" and suggested_node is not None:
                    default = suggested_node
                if kind == "text":
                    w = ui.input(value=str(default or "")) \
                        .props("dense outlined")
                elif kind == "float":
                    w = ui.number(
                        value=float(default or 0.0),
                        min=f.get("min_value"),
                        format="%.6f",
                    ).props("dense outlined")
                elif kind == "int":
                    w = ui.number(
                        value=int(default or 0),
                        min=f.get("min_value"),
                        step=int(f.get("step", 1)),
                        format="%d",
                    ).props("dense outlined")
                elif kind == "bool":
                    w = ui.switch(value=bool(default))
                elif kind == "select":
                    options_list = list(f.get("options", []))
                    w = ui.select(
                        options=options_list,
                        value=default if default in options_list else (options_list[0] if options_list else None),
                    ).props("dense outlined").classes("w-40")
                else:
                    continue
                if help_text:
                    w.tooltip(help_text)
                state["field_widgets"][f["name"]] = w

    state["status_label"].set_text("")
    expansion.text = f"Create a new {component.lower().rstrip('s')}"
    expansion.visible = True


def _build_create_hvdc_panel_widgets(state: dict, refresh_after_create) -> None:
    """Materialise the "Create a new HVDC line" expansion.

    Two converter-station pickers + the electrical fields from
    :data:`CREATABLE_HVDC_LINES`. Auto-hides when the active
    component isn't "HVDC Lines" or the network has fewer than two
    converter stations.
    """
    expansion = ui.expansion("Create a new HVDC line", icon="bolt").classes("w-full")
    expansion.visible = False
    state["expansion"] = expansion
    with expansion:
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Converter station 1:")
            cs1 = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
            ui.label("Converter station 2:")
            cs2 = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
        fields_container = ui.row().classes("items-start w-full q-pa-sm flex-wrap")
        with ui.row().classes("items-center w-full q-pa-sm"):
            create_btn = ui.button("Create", icon="add_circle")
            status_label = ui.label("").classes("text-caption q-ml-md")

    state["cs1_select"] = cs1
    state["cs2_select"] = cs2
    state["fields_container"] = fields_container
    state["status_label"] = status_label
    state["create_btn"] = create_btn
    state["field_widgets"] = {}

    def _on_create_click() -> None:
        if _state.network is None or state.get("current_component") != "HVDC Lines":
            return
        if not cs1.value or not cs2.value:
            status_label.set_text("Pick both converter stations first.")
            return
        spec = CREATABLE_HVDC_LINES
        raw = {f["name"]: _read_create_widget(state, f) for f in spec["fields"]}
        values = coerce_field_values(spec["fields"], raw)
        values["converter_station1_id"] = str(cs1.value)
        values["converter_station2_id"] = str(cs2.value)
        try:
            create_hvdc_line(_state.network, values)
        except Exception as exc:
            status_label.set_text(f"Create failed — {exc}")
            ui.notify(f"Create failed: {exc}", type="negative")
            return
        created_id = str(values.get("id") or "")
        status_label.set_text(f"Created HVDC line {created_id!r}.")
        ui.notify(f"Created HVDC line {created_id!r}",
                  type="positive", timeout=1500)
        # HVDC creation also touches the two converter stations on
        # both sides — flush diagram caches.
        _nad_cache.clear()
        _sld_cache.clear()
        if _state.selected_vl:
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)
        refresh_after_create()

    create_btn.on_click(_on_create_click)


def _refresh_create_hvdc_panel(state: dict, component: str) -> None:
    """Repopulate the HVDC creation panel for ``component``.

    Hides the expansion when the active component isn't "HVDC Lines"
    or the network has fewer than two converter stations.
    """
    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if component != "HVDC Lines" or _state.network is None:
        expansion.visible = False
        return

    try:
        stations = list_converter_stations(_state.network)
    except Exception:
        stations = []
    if len(stations) < 2:
        expansion.visible = False
        return

    options = {sid: f"{sid} ({kind})" for sid, kind in stations}
    items = list(options.keys())
    state["cs1_select"].options = options
    state["cs1_select"].value = items[0]
    state["cs1_select"].update()
    state["cs2_select"].options = options
    state["cs2_select"].value = items[1] if len(items) > 1 else items[0]
    state["cs2_select"].update()

    container = state["fields_container"]
    container.clear()
    state["field_widgets"] = {}
    with container:
        for f in CREATABLE_HVDC_LINES["fields"]:
            label_text = f["label"] + (" *" if f.get("required") else "")
            help_text = f.get("help") or ""
            with ui.column().classes("q-mr-md q-mb-md"):
                ui.label(label_text).classes("text-caption")
                kind = f["kind"]
                default = f.get("default")
                if kind == "text":
                    w = ui.input(value=str(default or "")) \
                        .props("dense outlined")
                elif kind == "float":
                    w = ui.number(
                        value=float(default or 0.0),
                        min=f.get("min_value"),
                        format="%.6f",
                    ).props("dense outlined")
                elif kind == "int":
                    w = ui.number(
                        value=int(default or 0),
                        min=f.get("min_value"),
                        step=int(f.get("step", 1)),
                        format="%d",
                    ).props("dense outlined")
                elif kind == "bool":
                    w = ui.switch(value=bool(default))
                elif kind == "select":
                    options_list = list(f.get("options", []))
                    w = ui.select(
                        options=options_list,
                        value=default if default in options_list else (options_list[0] if options_list else None),
                    ).props("dense outlined").classes("w-56")
                else:
                    continue
                if help_text:
                    w.tooltip(help_text)
                state["field_widgets"][f["name"]] = w

    state["status_label"].set_text("")
    expansion.visible = True


def _build_create_tap_changer_panel_widgets(state: dict, refresh_after_create) -> None:
    """Materialise the "Create a tap changer on an existing 2WT" expansion.

    A kind picker (Ratio / Phase), a target-transformer picker (filtered
    to ones that don't already have that kind of tap changer), the main
    fields from :data:`CREATABLE_TAP_CHANGERS` and an editable steps
    table. Auto-hides when the active component isn't
    "2-Winding Transformers" or no transformer accepts a new tap changer.
    """
    expansion = ui.expansion(
        "Create a tap changer on a 2-winding transformer", icon="tune",
    ).classes("w-full")
    expansion.visible = False
    state["expansion"] = expansion
    with expansion:
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Kind:")
            kind_select = ui.select(
                options=list(CREATABLE_TAP_CHANGERS), value="Ratio",
            ).props("dense outlined").classes("w-32")
            ui.label("Target 2WT:")
            twt_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
        fields_container = ui.row().classes("items-start w-full q-pa-sm flex-wrap")
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Number of steps:")
            steps_count = ui.number(value=3, min=1, max=50, step=1, format="%d") \
                .props("dense outlined").classes("w-24")
        steps_container = ui.column().classes("w-full q-pa-sm")
        with ui.row().classes("items-center w-full q-pa-sm"):
            create_btn = ui.button("Create tap changer", icon="add_circle")
            status_label = ui.label("").classes("text-caption q-ml-md")

    state["kind_select"] = kind_select
    state["twt_select"] = twt_select
    state["fields_container"] = fields_container
    state["steps_count"] = steps_count
    state["steps_container"] = steps_container
    state["status_label"] = status_label
    state["create_btn"] = create_btn
    state["field_widgets"] = {}
    state["step_widgets"] = []  # list[dict[col -> ui.number]]

    def _rebuild_steps() -> None:
        kind = state["kind_select"].value
        spec = CREATABLE_TAP_CHANGERS.get(kind)
        if spec is None:
            return
        cols = spec["step_columns"]
        defaults = spec["step_defaults"]
        n = int(state["steps_count"].value or 0)
        if n < 1:
            n = 1
        steps_container.clear()
        state["step_widgets"] = []
        with steps_container:
            with ui.row().classes("items-center text-caption"):
                ui.label("step").classes("w-12")
                for col in cols:
                    ui.label(col).classes("w-20 text-center")
            for r in range(n):
                with ui.row().classes("items-center"):
                    ui.label(str(r)).classes("w-12 text-caption")
                    row_widgets: dict = {}
                    for col in cols:
                        w = ui.number(
                            value=float(defaults[col]), format="%.4f",
                        ).props("dense outlined").classes("w-20")
                        row_widgets[col] = w
                    state["step_widgets"].append(row_widgets)

    state["rebuild_steps"] = _rebuild_steps
    steps_count.on("update:model-value", lambda *_: _rebuild_steps())
    kind_select.on("update:model-value", lambda *_: (
        _refresh_create_tap_changer_panel(state, state.get("current_component", "")),
    ))

    def _on_create_click() -> None:
        if _state.network is None or state.get("current_component") != "2-Winding Transformers":
            return
        kind = state["kind_select"].value
        spec = CREATABLE_TAP_CHANGERS.get(kind)
        if spec is None:
            return
        transformer_id = state["twt_select"].value
        if not transformer_id:
            status_label.set_text("Pick a target transformer first.")
            return
        raw = {
            f["name"]: _read_create_widget(state, f)
            for f in spec["main_fields"]
        }
        main_fields = coerce_field_values(spec["main_fields"], raw)
        steps: list[dict] = []
        for row in state["step_widgets"]:
            step = {}
            for col, widget in row.items():
                try:
                    step[col] = float(widget.value)
                except (TypeError, ValueError):
                    step[col] = spec["step_defaults"][col]
            steps.append(step)
        try:
            create_tap_changer(
                _state.network, kind, str(transformer_id), main_fields, steps,
            )
        except Exception as exc:
            status_label.set_text(f"Create failed — {exc}")
            ui.notify(f"Create failed: {exc}", type="negative")
            return
        status_label.set_text(
            f"Created {kind.lower()} tap changer on {transformer_id!r} "
            f"({len(steps)} steps)."
        )
        ui.notify(
            f"Created {kind.lower()} tap changer on {transformer_id!r}",
            type="positive", timeout=1500,
        )
        refresh_after_create()

    create_btn.on_click(_on_create_click)


def _refresh_create_tap_changer_panel(state: dict, component: str) -> None:
    """Repopulate the tap-changer creation panel for ``component``.

    Hides the expansion when the active component isn't
    "2-Winding Transformers" or no transformer is missing the chosen kind.
    """
    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if component != "2-Winding Transformers" or _state.network is None:
        expansion.visible = False
        return

    kind = state["kind_select"].value or "Ratio"
    available = list_transformers_without_tap_changer(_state.network, kind)
    if not available:
        expansion.visible = False
        return

    state["twt_select"].options = available
    if state["twt_select"].value not in available:
        state["twt_select"].value = available[0]
    state["twt_select"].update()

    spec = CREATABLE_TAP_CHANGERS[kind]
    container = state["fields_container"]
    container.clear()
    state["field_widgets"] = {}
    with container:
        for f in spec["main_fields"]:
            label_text = f["label"] + (" *" if f.get("required") else "")
            help_text = f.get("help") or ""
            with ui.column().classes("q-mr-md q-mb-md"):
                ui.label(label_text).classes("text-caption")
                kind_f = f["kind"]
                default = f.get("default")
                if kind_f == "text":
                    w = ui.input(value=str(default or "")) \
                        .props("dense outlined")
                elif kind_f == "float":
                    w = ui.number(
                        value=float(default or 0.0),
                        min=f.get("min_value"),
                        format="%.6f",
                    ).props("dense outlined")
                elif kind_f == "int":
                    w = ui.number(
                        value=int(default or 0),
                        min=f.get("min_value"),
                        step=int(f.get("step", 1)),
                        format="%d",
                    ).props("dense outlined")
                elif kind_f == "bool":
                    w = ui.switch(value=bool(default))
                elif kind_f == "select":
                    options_list = list(f.get("options", []))
                    w = ui.select(
                        options=options_list,
                        value=default if default in options_list else (
                            options_list[0] if options_list else None
                        ),
                    ).props("dense outlined").classes("w-56")
                else:
                    continue
                if help_text:
                    w.tooltip(help_text)
                state["field_widgets"][f["name"]] = w

    state["rebuild_steps"]()
    state["status_label"].set_text("")
    expansion.visible = True


def _build_create_coupling_device_panel_widgets(
    state: dict, refresh_after_create,
) -> None:
    """Materialise the "Create a coupling device" expansion.

    A VL picker filtered to node-breaker VLs with ≥2 busbar sections,
    two BBS pickers populated from the active VL, and an optional
    switch-prefix input. Auto-hides when the active component isn't
    "Switches" or no VL is eligible.
    """
    expansion = ui.expansion("Create a coupling device", icon="link") \
        .classes("w-full")
    expansion.visible = False
    state["expansion"] = expansion
    with expansion:
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Voltage level:")
            vl_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("BBS 1:")
            bbs1_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-48")
            ui.label("BBS 2:")
            bbs2_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-48")
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Switch prefix:")
            prefix_input = ui.input(value="", placeholder="optional") \
                .props("dense outlined").classes("w-64")
        with ui.row().classes("items-center w-full q-pa-sm"):
            create_btn = ui.button("Create coupling device", icon="add_circle")
            status_label = ui.label("").classes("text-caption q-ml-md")

    state["vl_select"] = vl_select
    state["bbs1_select"] = bbs1_select
    state["bbs2_select"] = bbs2_select
    state["prefix_input"] = prefix_input
    state["status_label"] = status_label
    state["create_btn"] = create_btn

    def _refresh_bbs() -> None:
        if _state.network is None:
            return
        vl_id = state["vl_select"].value
        if not vl_id:
            return
        ids = list_busbar_sections(_state.network, vl_id)
        opts = {bid: bid for bid in ids}
        for sel, default_idx in (
            (state["bbs1_select"], 0),
            (state["bbs2_select"], 1 if len(ids) > 1 else 0),
        ):
            sel.options = opts
            sel.value = ids[default_idx] if ids else None
            sel.update()

    state["refresh_bbs"] = _refresh_bbs
    vl_select.on("update:model-value", lambda *_: _refresh_bbs())

    def _on_create_click() -> None:
        if _state.network is None or state.get("current_component") != "Switches":
            return
        bbs1 = state["bbs1_select"].value
        bbs2 = state["bbs2_select"].value
        prefix = (state["prefix_input"].value or "").strip() or None
        try:
            create_coupling_device(_state.network, str(bbs1), str(bbs2), prefix)
        except Exception as exc:
            status_label.set_text(f"Create failed — {exc}")
            ui.notify(f"Create failed: {exc}", type="negative")
            return
        status_label.set_text(f"Created coupling device between {bbs1} and {bbs2}.")
        ui.notify(
            f"Created coupling device between {bbs1} and {bbs2}",
            type="positive", timeout=1500,
        )
        # Coupling devices add switches in a VL — flush diagram caches so
        # the SLD repaints with the new breaker + disconnectors.
        _nad_cache.clear()
        _sld_cache.clear()
        if _state.selected_vl:
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)
        refresh_after_create()

    create_btn.on_click(_on_create_click)


def _refresh_create_coupling_device_panel(state: dict, component: str) -> None:
    """Repopulate the coupling-device panel for ``component``.

    Hides the expansion when the active component isn't "Switches" or
    no node-breaker VL with ≥2 BBS is available.
    """
    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if component != "Switches" or _state.network is None:
        expansion.visible = False
        return

    try:
        vls = list_node_breaker_vls_with_multi_bbs(_state.network)
    except Exception:
        vls = []
    if not vls:
        expansion.visible = False
        return

    options = {vl_id: f"{display} ({kv:.0f} kV)" for vl_id, display, kv in vls}
    items = list(options.keys())
    state["vl_select"].options = options
    if state["vl_select"].value not in items:
        state["vl_select"].value = items[0]
    state["vl_select"].update()
    state["refresh_bbs"]()
    state["status_label"].set_text("")
    expansion.visible = True


def _build_create_reactive_limits_panel_widgets(
    state: dict, refresh_after_create,
) -> None:
    """Materialise the "Attach reactive limits" expansion.

    A target picker (Generator / Battery / VSC), a mode toggle (min/max
    or curve), and the editable inputs for the chosen mode. Auto-hides
    when the active component isn't in :data:`REACTIVE_LIMITS_TARGETS`
    or no candidate element exists.
    """
    expansion = ui.expansion("Attach reactive limits", icon="show_chart") \
        .classes("w-full")
    expansion.visible = False
    state["expansion"] = expansion
    with expansion:
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Target:")
            target_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
            ui.label("Mode:")
            mode_select = ui.select(
                options={"minmax": "min/max", "curve": "curve"},
                value="minmax",
            ).props("dense outlined").classes("w-40")

        minmax_row = ui.row().classes("items-center w-full q-pa-sm")
        with minmax_row:
            ui.label("min_q (MVar):")
            min_q_input = ui.number(value=-100.0, format="%.3f") \
                .props("dense outlined").classes("w-40")
            ui.label("max_q (MVar):")
            max_q_input = ui.number(value=100.0, format="%.3f") \
                .props("dense outlined").classes("w-40")

        curve_container = ui.column().classes("w-full q-pa-sm")
        with curve_container:
            with ui.row().classes("items-center"):
                ui.label("Number of points:")
                point_count = ui.number(value=2, min=2, max=50, step=1, format="%d") \
                    .props("dense outlined").classes("w-24")
            points_container = ui.column().classes("w-full")

        with ui.row().classes("items-center w-full q-pa-sm"):
            create_btn = ui.button("Save reactive limits", icon="save")
            status_label = ui.label("").classes("text-caption q-ml-md")

    state["target_select"] = target_select
    state["mode_select"] = mode_select
    state["minmax_row"] = minmax_row
    state["min_q_input"] = min_q_input
    state["max_q_input"] = max_q_input
    state["curve_container"] = curve_container
    state["point_count"] = point_count
    state["points_container"] = points_container
    state["status_label"] = status_label
    state["create_btn"] = create_btn
    state["point_widgets"] = []  # list[dict[col -> ui.number]]

    def _seed_curve_defaults() -> list[tuple[float, float, float]]:
        return [(0.0, -100.0, 100.0), (100.0, -80.0, 80.0)]

    def _rebuild_points() -> None:
        n = int(state["point_count"].value or 2)
        if n < 2:
            n = 2
        defaults = _seed_curve_defaults()
        # Linearly extrapolate beyond the 2 seeded rows.
        def _row_default(r: int) -> tuple[float, float, float]:
            if r < len(defaults):
                return defaults[r]
            last_p, last_mn, last_mx = defaults[-1]
            return (last_p + 100.0 * (r - len(defaults) + 1), last_mn, last_mx)

        state["points_container"].clear()
        state["point_widgets"] = []
        with state["points_container"]:
            with ui.row().classes("items-center text-caption"):
                ui.label("point").classes("w-12")
                for col in ("p", "min_q", "max_q"):
                    ui.label(col).classes("w-24 text-center")
            for r in range(n):
                p_d, mn_d, mx_d = _row_default(r)
                with ui.row().classes("items-center"):
                    ui.label(str(r)).classes("w-12 text-caption")
                    p_w = ui.number(value=p_d, format="%.4f") \
                        .props("dense outlined").classes("w-24")
                    mn_w = ui.number(value=mn_d, format="%.4f") \
                        .props("dense outlined").classes("w-24")
                    mx_w = ui.number(value=mx_d, format="%.4f") \
                        .props("dense outlined").classes("w-24")
                    state["point_widgets"].append(
                        {"p": p_w, "min_q": mn_w, "max_q": mx_w},
                    )

    state["rebuild_points"] = _rebuild_points

    def _apply_mode_visibility() -> None:
        mode = state["mode_select"].value
        state["minmax_row"].visible = mode == "minmax"
        state["curve_container"].visible = mode == "curve"

    state["apply_mode_visibility"] = _apply_mode_visibility
    mode_select.on("update:model-value", lambda *_: _apply_mode_visibility())
    point_count.on("update:model-value", lambda *_: _rebuild_points())

    def _on_create_click() -> None:
        if (
            _state.network is None
            or state.get("current_component") not in REACTIVE_LIMITS_TARGETS
        ):
            return
        target_id = state["target_select"].value
        if not target_id:
            status_label.set_text("Pick a target first.")
            return
        mode = state["mode_select"].value or "minmax"
        if mode == "minmax":
            payload = [{
                "min_q": float(min_q_input.value),
                "max_q": float(max_q_input.value),
            }]
        else:
            payload = []
            for row in state["point_widgets"]:
                try:
                    payload.append({
                        "p": float(row["p"].value),
                        "min_q": float(row["min_q"].value),
                        "max_q": float(row["max_q"].value),
                    })
                except (TypeError, ValueError):
                    pass
        try:
            create_reactive_limits(_state.network, str(target_id), mode, payload)
        except Exception as exc:
            status_label.set_text(f"Save failed — {exc}")
            ui.notify(f"Save failed: {exc}", type="negative")
            return
        label = "min/max" if mode == "minmax" else "curve"
        status_label.set_text(f"Saved {label} reactive limits on {target_id!r}.")
        ui.notify(
            f"Saved {label} reactive limits on {target_id!r}",
            type="positive", timeout=1500,
        )
        refresh_after_create()

    create_btn.on_click(_on_create_click)


def _refresh_create_reactive_limits_panel(state: dict, component: str) -> None:
    """Repopulate the reactive-limits panel for ``component``.

    Hides the expansion when the active component isn't in
    :data:`REACTIVE_LIMITS_TARGETS` or no candidate target is available.
    """
    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if (
        component not in REACTIVE_LIMITS_TARGETS
        or _state.network is None
    ):
        expansion.visible = False
        return
    ids = list_reactive_limit_candidates(_state.network, component)
    if not ids:
        expansion.visible = False
        return
    options = {i: i for i in ids}
    state["target_select"].options = options
    if state["target_select"].value not in ids:
        state["target_select"].value = ids[0]
    state["target_select"].update()
    state["rebuild_points"]()
    state["apply_mode_visibility"]()
    state["status_label"].set_text("")
    expansion.visible = True


def _build_create_operational_limits_panel_widgets(
    state: dict, refresh_after_create,
) -> None:
    """Materialise the "Attach operational limits" expansion.

    A target picker (Line / 2WT / Dangling Line), side + type pickers, a
    group-name input, and a dynamic grid of limit rows (name / value /
    acceptable_duration / fictitious). Sized by a "Number of rows"
    spinner that grows the editor with linearly-bumped TATL defaults.
    Auto-hides when the active component isn't in
    :data:`OPERATIONAL_LIMITS_TARGETS` or no candidate exists.
    """
    expansion = ui.expansion("Attach operational limits", icon="speed") \
        .classes("w-full")
    expansion.visible = False
    state["expansion"] = expansion
    with expansion:
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Target:")
            target_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
            ui.label("Side:")
            side_select = ui.select(
                options=list(OPERATIONAL_LIMIT_SIDES),
                value=OPERATIONAL_LIMIT_SIDES[0],
            ).props("dense outlined").classes("w-24")
            ui.label("Type:")
            type_select = ui.select(
                options=list(OPERATIONAL_LIMIT_TYPES),
                value=OPERATIONAL_LIMIT_TYPES[0],
            ).props("dense outlined").classes("w-48")
            ui.label("Group:")
            group_input = ui.input(value="DEFAULT") \
                .props("dense outlined").classes("w-40")
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Number of rows:")
            row_count = ui.number(value=2, min=1, max=50, step=1, format="%d") \
                .props("dense outlined").classes("w-24")
        rows_container = ui.column().classes("w-full q-pa-sm")
        with ui.row().classes("items-center w-full q-pa-sm"):
            create_btn = ui.button("Save operational limits", icon="save")
            status_label = ui.label("").classes("text-caption q-ml-md")

    state["target_select"] = target_select
    state["side_select"] = side_select
    state["type_select"] = type_select
    state["group_input"] = group_input
    state["row_count"] = row_count
    state["rows_container"] = rows_container
    state["create_btn"] = create_btn
    state["status_label"] = status_label
    state["limit_widgets"] = []  # list[dict[col -> widget]]

    def _row_default(r: int) -> tuple[str, float, int, bool]:
        if r == 0:
            return ("permanent", 1000.0, PERMANENT_DURATION, False)
        if r == 1:
            return ("TATL_60", 1200.0, 60, False)
        duration = 300 * r
        return (f"TATL_{duration}", 1200.0, duration, False)

    def _rebuild_rows() -> None:
        n = int(state["row_count"].value or 1)
        if n < 1:
            n = 1
        state["rows_container"].clear()
        state["limit_widgets"] = []
        with state["rows_container"]:
            with ui.row().classes("items-center text-caption"):
                ui.label("#").classes("w-8")
                ui.label("name").classes("w-32")
                ui.label("value").classes("w-32 text-center")
                ui.label("acceptable_duration").classes("w-40 text-center")
                ui.label("fictitious").classes("w-24 text-center")
            for r in range(n):
                name_d, value_d, dur_d, fict_d = _row_default(r)
                with ui.row().classes("items-center"):
                    ui.label(str(r)).classes("w-8 text-caption")
                    name_w = ui.input(value=name_d) \
                        .props("dense outlined").classes("w-32")
                    value_w = ui.number(value=value_d, format="%.4f") \
                        .props("dense outlined").classes("w-32")
                    dur_w = ui.number(value=dur_d, step=1, format="%d") \
                        .props("dense outlined").classes("w-40")
                    fict_w = ui.switch(value=fict_d).classes("w-24")
                    state["limit_widgets"].append({
                        "name": name_w, "value": value_w,
                        "acceptable_duration": dur_w, "fictitious": fict_w,
                    })

    state["rebuild_rows"] = _rebuild_rows
    row_count.on("update:model-value", lambda *_: _rebuild_rows())

    def _on_create_click() -> None:
        if (
            _state.network is None
            or state.get("current_component") not in OPERATIONAL_LIMITS_TARGETS
        ):
            return
        element_id = state["target_select"].value
        if not element_id:
            status_label.set_text("Pick a target first.")
            return
        side = state["side_select"].value
        limit_type = state["type_select"].value
        group_name = (state["group_input"].value or "DEFAULT").strip() or "DEFAULT"
        limits: list[dict] = []
        for row in state["limit_widgets"]:
            try:
                value = float(row["value"].value)
                duration = int(row["acceptable_duration"].value)
            except (TypeError, ValueError):
                continue
            name = (row["name"].value or "").strip() or None
            limits.append({
                "name": name, "value": value,
                "acceptable_duration": duration,
                "fictitious": bool(row["fictitious"].value),
            })
        try:
            create_operational_limits(
                _state.network, str(element_id), side, limit_type, limits, group_name,
            )
        except Exception as exc:
            status_label.set_text(f"Save failed — {exc}")
            ui.notify(f"Save failed: {exc}", type="negative")
            return
        status_label.set_text(
            f"Saved {len(limits)} {limit_type.lower()} limit(s) on "
            f"{element_id} (side {side}, group {group_name!r})."
        )
        ui.notify(
            f"Saved {len(limits)} {limit_type.lower()} limit(s) on {element_id!r}",
            type="positive", timeout=1500,
        )
        refresh_after_create()

    create_btn.on_click(_on_create_click)


def _refresh_create_operational_limits_panel(state: dict, component: str) -> None:
    """Repopulate the operational-limits panel for ``component``.

    Hides the expansion when the active component isn't in
    :data:`OPERATIONAL_LIMITS_TARGETS` or no candidate target is available.
    """
    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if (
        component not in OPERATIONAL_LIMITS_TARGETS
        or _state.network is None
    ):
        expansion.visible = False
        return
    ids = list_operational_limit_candidates(_state.network, component)
    if not ids:
        expansion.visible = False
        return
    options = {i: i for i in ids}
    state["target_select"].options = options
    if state["target_select"].value not in ids:
        state["target_select"].value = ids[0]
    state["target_select"].update()
    state["rebuild_rows"]()
    state["status_label"].set_text("")
    expansion.visible = True


def _build_create_secondary_voltage_control_panel_widgets(
    state: dict, refresh_after_create,
) -> None:
    """Materialise the "Configure secondary voltage control" expansion.

    Two editable grids — zones (name / target_v / bus_ids) and units
    (unit_id / zone_name / participate) — sized by row-count spinners.
    Shows when the active component is "Voltage Levels"; pypowsybl
    replaces the whole SVC extension on submit.
    """
    expansion = ui.expansion(
        "Configure secondary voltage control", icon="hub",
    ).classes("w-full")
    expansion.visible = False
    state["expansion"] = expansion
    with expansion:
        ui.label(
            "Define control zones and the units that participate in each. "
            "pypowsybl replaces the whole secondaryVoltageControl extension "
            "on submit. bus_ids is space-separated if a zone has several "
            "pilot points."
        ).classes("text-caption q-pa-sm")
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Number of zones:")
            zone_count = ui.number(value=1, min=1, max=50, step=1, format="%d") \
                .props("dense outlined").classes("w-24")
        zones_container = ui.column().classes("w-full q-pa-sm")
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Number of units:")
            unit_count = ui.number(value=1, min=1, max=200, step=1, format="%d") \
                .props("dense outlined").classes("w-24")
        units_container = ui.column().classes("w-full q-pa-sm")
        with ui.row().classes("items-center w-full q-pa-sm"):
            create_btn = ui.button("Save secondary voltage control", icon="save")
            status_label = ui.label("").classes("text-caption q-ml-md")

    state["zone_count"] = zone_count
    state["zones_container"] = zones_container
    state["unit_count"] = unit_count
    state["units_container"] = units_container
    state["create_btn"] = create_btn
    state["status_label"] = status_label
    state["zone_widgets"] = []  # list[dict[col -> widget]]
    state["unit_widgets"] = []

    def _rebuild_zones() -> None:
        n = int(state["zone_count"].value or 1)
        if n < 1:
            n = 1
        state["zones_container"].clear()
        state["zone_widgets"] = []
        with state["zones_container"]:
            with ui.row().classes("items-center text-caption"):
                ui.label("#").classes("w-8")
                ui.label("name").classes("w-32")
                ui.label("target_v (kV)").classes("w-32 text-center")
                ui.label("bus_ids (space-separated)").classes("flex-grow text-center")
            for r in range(n):
                with ui.row().classes("items-center w-full"):
                    ui.label(str(r)).classes("w-8 text-caption")
                    name_w = ui.input(value=f"ZONE_{r + 1}") \
                        .props("dense outlined").classes("w-32")
                    target_w = ui.number(value=400.0, format="%.4f") \
                        .props("dense outlined").classes("w-32")
                    bus_w = ui.input(value="") \
                        .props("dense outlined").classes("flex-grow")
                    state["zone_widgets"].append({
                        "name": name_w, "target_v": target_w, "bus_ids": bus_w,
                    })

    def _rebuild_units() -> None:
        n = int(state["unit_count"].value or 1)
        if n < 1:
            n = 1
        state["units_container"].clear()
        state["unit_widgets"] = []
        with state["units_container"]:
            with ui.row().classes("items-center text-caption"):
                ui.label("#").classes("w-8")
                ui.label("unit_id").classes("w-40")
                ui.label("zone_name").classes("w-32")
                ui.label("participate").classes("w-24 text-center")
            for r in range(n):
                with ui.row().classes("items-center"):
                    ui.label(str(r)).classes("w-8 text-caption")
                    uid_w = ui.input(value="") \
                        .props("dense outlined").classes("w-40")
                    zone_w = ui.input(value="ZONE_1") \
                        .props("dense outlined").classes("w-32")
                    part_w = ui.switch(value=True).classes("w-24")
                    state["unit_widgets"].append({
                        "unit_id": uid_w, "zone_name": zone_w, "participate": part_w,
                    })

    state["rebuild_zones"] = _rebuild_zones
    state["rebuild_units"] = _rebuild_units
    zone_count.on("update:model-value", lambda *_: _rebuild_zones())
    unit_count.on("update:model-value", lambda *_: _rebuild_units())

    def _on_create_click() -> None:
        if _state.network is None or state.get("current_component") != "Voltage Levels":
            return
        zones: list[dict] = []
        for row in state["zone_widgets"]:
            name = (row["name"].value or "").strip()
            if not name:
                continue
            try:
                target_v = float(row["target_v"].value)
            except (TypeError, ValueError):
                target_v = None
            zones.append({
                "name": name, "target_v": target_v,
                "bus_ids": (row["bus_ids"].value or "").strip(),
            })
        units: list[dict] = []
        for row in state["unit_widgets"]:
            uid = (row["unit_id"].value or "").strip()
            if not uid:
                continue
            units.append({
                "unit_id": uid,
                "zone_name": (row["zone_name"].value or "").strip(),
                "participate": bool(row["participate"].value),
            })
        try:
            create_secondary_voltage_control(_state.network, zones, units)
        except Exception as exc:
            status_label.set_text(f"Save failed — {exc}")
            ui.notify(f"Save failed: {exc}", type="negative")
            return
        status_label.set_text(
            f"Saved {len(zones)} zone(s) and {len(units)} unit(s)."
        )
        ui.notify(
            f"Saved {len(zones)} zone(s) and {len(units)} unit(s).",
            type="positive", timeout=1500,
        )
        refresh_after_create()

    create_btn.on_click(_on_create_click)


def _refresh_create_secondary_voltage_control_panel(
    state: dict, component: str,
) -> None:
    """Toggle the SVC panel visibility on the active component."""
    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if component != "Voltage Levels" or _state.network is None:
        expansion.visible = False
        return
    state["rebuild_zones"]()
    state["rebuild_units"]()
    state["status_label"].set_text("")
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

    branch_create_state = {
        "vl1_select": None, "vl2_select": None,
        "bbs1_select": None, "bbs2_select": None,
        "field_widgets": {},
        "status_label": None,
        "expansion": None,
    }
    _build_create_branch_panel_widgets(
        branch_create_state, refresh_after_create=lambda: refresh(),
    )

    container_create_state = {
        "context_select": None,
        "context_label": None,
        "field_widgets": {},
        "status_label": None,
        "expansion": None,
    }
    _build_create_container_panel_widgets(
        container_create_state, refresh_after_create=lambda: refresh(),
    )

    hvdc_create_state = {
        "cs1_select": None, "cs2_select": None,
        "field_widgets": {},
        "status_label": None,
        "expansion": None,
    }
    _build_create_hvdc_panel_widgets(
        hvdc_create_state, refresh_after_create=lambda: refresh(),
    )

    tap_changer_create_state: dict = {
        "kind_select": None, "twt_select": None,
        "fields_container": None, "steps_count": None,
        "steps_container": None, "field_widgets": {},
        "step_widgets": [],
        "status_label": None,
        "expansion": None,
        "rebuild_steps": None,
        "current_component": "",
    }
    _build_create_tap_changer_panel_widgets(
        tap_changer_create_state, refresh_after_create=lambda: refresh(),
    )

    coupling_create_state: dict = {
        "vl_select": None, "bbs1_select": None, "bbs2_select": None,
        "prefix_input": None,
        "status_label": None, "expansion": None,
        "refresh_bbs": None,
        "current_component": "",
    }
    _build_create_coupling_device_panel_widgets(
        coupling_create_state, refresh_after_create=lambda: refresh(),
    )

    reactive_limits_create_state: dict = {
        "target_select": None, "mode_select": None,
        "minmax_row": None, "min_q_input": None, "max_q_input": None,
        "curve_container": None, "point_count": None,
        "points_container": None, "point_widgets": [],
        "status_label": None, "expansion": None,
        "rebuild_points": None, "apply_mode_visibility": None,
        "current_component": "",
    }
    _build_create_reactive_limits_panel_widgets(
        reactive_limits_create_state, refresh_after_create=lambda: refresh(),
    )

    operational_limits_create_state: dict = {
        "target_select": None, "side_select": None, "type_select": None,
        "group_input": None, "row_count": None,
        "rows_container": None, "limit_widgets": [],
        "status_label": None, "expansion": None,
        "rebuild_rows": None,
        "current_component": "",
    }
    _build_create_operational_limits_panel_widgets(
        operational_limits_create_state, refresh_after_create=lambda: refresh(),
    )

    svc_create_state: dict = {
        "zone_count": None, "zones_container": None, "zone_widgets": [],
        "unit_count": None, "units_container": None, "unit_widgets": [],
        "status_label": None, "expansion": None,
        "rebuild_zones": None, "rebuild_units": None,
        "current_component": "",
    }
    _build_create_secondary_voltage_control_panel_widgets(
        svc_create_state, refresh_after_create=lambda: refresh(),
    )

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
            # ``grid.options.update`` (not ``=``) so the wrapper's
            # ``theme`` / ``autoSizeStrategy`` defaults survive — AG Grid
            # 34 throws when ``options.theme`` is undefined.
            grid.options.update({
                "columnDefs": [], "rowData": [],
                "defaultColDef": _DEFAULT_COL_DEF,
                "rowSelection": "multiple",
            })
            grid.update()
            summary.set_text("No network loaded.")
            bulk_row.set_visibility(False)
            vl_filter.visible = False
            current_df["df"] = None
            return
        try:
            df_full = get_enriched_dataframe(_state.network, label)
        except Exception as exc:
            grid.options.update({
                "columnDefs": [], "rowData": [],
                "defaultColDef": _DEFAULT_COL_DEF,
                "rowSelection": "multiple",
            })
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
        # ``options.update`` keeps the wrapper-set ``theme`` /
        # ``autoSizeStrategy``; AG Grid 34 throws if either is missing.
        grid.options.update(_dataframe_to_aggrid_options(
            df, editable_cols=cols, filterable_cols=filterable_cols,
        ))
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
        # Refresh the create panels; each one auto-hides for the wrong
        # category (injections / branches / containers / HVDC).
        _refresh_create_panel(create_state, label)
        _refresh_create_branch_panel(branch_create_state, label)
        _refresh_create_container_panel(container_create_state, label)
        _refresh_create_hvdc_panel(hvdc_create_state, label)
        _refresh_create_tap_changer_panel(tap_changer_create_state, label)
        _refresh_create_coupling_device_panel(coupling_create_state, label)
        _refresh_create_reactive_limits_panel(reactive_limits_create_state, label)
        _refresh_create_operational_limits_panel(operational_limits_create_state, label)
        _refresh_create_secondary_voltage_control_panel(svc_create_state, label)
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
    global _map_ready, _nad_ready, _sld_ready
    _map_ready = False
    _nad_ready = False
    _sld_ready = False

    # Page-level bridge JS, head-injected so emitEvent is bound by the
    # time the iframes finish loading.
    ui.add_body_html(f"<script>{_BRIDGE_JS}</script>")

    # ------------------------------------------------------------------
    # Layout — Streamlit-style left drawer holds the global controls
    # (title, file picker, load-flow trigger); the main area below
    # carries the diagram + data tabs.
    # ------------------------------------------------------------------
    with ui.left_drawer(fixed=False, bordered=True) \
            .classes("bg-grey-2 q-pa-md") \
            .style("width: 280px"):
        ui.label("IIDM Viewer").classes("text-h6")
        file_lbl = ui.label("No file loaded.").classes("text-caption")
        vl_lbl = ui.label("VL: —").classes("text-caption q-mb-sm")

        async def handle_upload(e):
            # NiceGUI 3.x: the event carries a ``FileUpload`` on ``e.file``
            # with an async ``read()`` / ``save()``; 2.x had ``e.name`` and
            # a sync ``e.content`` stream. Support both so the prototype
            # runs against either version.
            upload = getattr(e, "file", None) or e
            name = upload.name
            tmp_path = f"/tmp/iidm_upload_{os.getpid()}_{os.path.basename(name)}"
            if hasattr(upload, "save"):
                await upload.save(tmp_path)
            else:  # NiceGUI 2.x fallback
                with open(tmp_path, "wb") as fh:
                    fh.write(upload.content.read())
            try:
                # The pypowsybl load is heavy I/O, so push it to a worker
                # thread to keep the event loop responsive. Listener
                # notifications (which build NiceGUI elements) MUST run
                # back on the event loop — the slot stack is empty in
                # the worker thread, which surfaces as
                #   "The current slot cannot be determined…"
                from iidm_viewer import network_loader

                # Thread the AppState-cached import overrides through —
                # set by the Import options modal. Pypowsybl auto-detects
                # the format from the extension when ``import_format``
                # is ``None`` (the dialog's "Auto-detect" value).
                network = await asyncio.to_thread(
                    network_loader.load_from_path,
                    tmp_path,
                    parameters=_state.import_params or None,
                    post_processors=_state.import_post_processors or None,
                )
                _state.install_network(network)
            except Exception as exc:
                ui.notify(f"Load failed: {exc}", type="negative")
                return
            file_lbl.set_text(os.path.basename(name))

        ui.upload(
            on_upload=handle_upload,
            auto_upload=True,
            label="Load network…",
        ).props("flat dense accept='.xiidm,.iidm,.xml,.zip,.mat,.uct'") \
         .classes("full-width q-mb-sm")

        # "Import options…" opens the load-options modal — sets the
        # format / params / post-processors used on the next upload.
        ui.button(
            "Import options…", on_click=_open_load_options_dialog,
        ).props("flat dense").classes("full-width q-mb-sm")

        # "Save network" mirrors the Streamlit dialog: pops a modal
        # with a format picker and downloads the exported bytes via
        # ``ui.download``. Disabled until a network is loaded.
        save_btn = ui.button(
            "Save network", on_click=_open_save_network_dialog,
        ).props("flat dense").classes("full-width q-mb-sm")
        save_btn.set_enabled(False)

        # Voltage Level picker (mirrors Streamlit's vl_selector).
        # Hidden until a network is loaded. The "Filter" input narrows
        # the dropdown to a substring match on the display name; the
        # select fires AppState.set_selected_vl on every change.
        ui.label("Voltage Level").classes("text-caption q-mt-sm")
        vl_filter_input = ui.input(placeholder="Filter voltage levels") \
            .props("dense outlined clearable").classes("full-width")
        vl_select = ui.select(options=[], value=None) \
            .props("dense outlined").classes("full-width q-mb-sm")
        vl_filter_input.visible = False
        vl_select.visible = False

        # Holds the latest pd.DataFrame so filter changes don't re-fetch.
        vl_picker_state: dict = {"df": None, "suppress_listener": False}

        def _rebuild_vl_options() -> None:
            df = vl_picker_state["df"]
            if df is None or df.empty:
                vl_select.options = {}
                vl_select.value = None
                vl_select.update()
                return
            from iidm_viewer.network_loader import (
                filter_voltage_levels as _filter,
            )
            filtered = _filter(df, vl_filter_input.value or "")
            options: dict[str, str] = {}
            for _, row in filtered.iterrows():
                kv = (
                    f" ({row['nominal_v']:.0f} kV)"
                    if "nominal_v" in row and row["nominal_v"] == row["nominal_v"]
                    else ""
                )
                options[row["id"]] = f"{row['display']}{kv}"
            current = vl_select.value if vl_select.value in options else None
            vl_select.options = options
            # Preserve the previous selection if still present, else fall
            # through and let the state-driven sync (_on_state_vl) set it.
            vl_select.value = current
            vl_select.update()

        def _on_vl_filter_changed(_e=None) -> None:
            _rebuild_vl_options()

        def _on_vl_select_changed(_e=None) -> None:
            # Skip while we're programmatically syncing the widget from
            # AppState (set_selected_vl will fire again otherwise).
            if vl_picker_state["suppress_listener"]:
                return
            vl_id = vl_select.value
            if vl_id:
                _state.set_selected_vl(str(vl_id))

        vl_filter_input.on("update:model-value", _on_vl_filter_changed)
        vl_select.on("update:model-value", _on_vl_select_changed)

        # AC load-flow trigger — disabled until a network is loaded;
        # status appears below it via ui.notify when the run returns.
        # "Run AC Load Flow" + a gear button that opens the LF
        # parameters dialog, mirroring Streamlit's sidebar pair.
        with ui.row().classes("full-width items-center no-wrap"):
            run_lf_btn = ui.button("Run AC Load Flow").props("flat dense") \
                .classes("col-grow")
            run_lf_btn.set_enabled(False)
            lf_params_btn = ui.button(icon="settings").props("flat dense round") \
                .tooltip("Load Flow Parameters")

        def _on_lf_params_save(generic: dict, provider: dict) -> None:
            _state.lf_generic_params = generic
            _state.lf_provider_params = provider

        lf_params_btn.on_click(
            lambda: _open_lf_parameters_dialog(_on_lf_params_save),
        )

        lf_status_lbl = ui.label("").classes("text-caption q-mt-sm")
        # "View Logs" opens a modal with the parsed report_json tree.
        # Disabled until a LF has produced a non-empty report.
        view_logs_btn = ui.button("View Logs").props("flat dense") \
            .classes("full-width")
        view_logs_btn.set_enabled(False)

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
            # Gate the "View Logs" button on the cached report.
            view_logs_btn.set_enabled(bool(_state.last_report_json))
            if result and result.converged:
                ui.notify(f"AC load flow: {status}", type="positive")
            else:
                ui.notify(f"AC load flow: {status}", type="warning")

        run_lf_btn.on_click(on_run_lf)
        view_logs_btn.on_click(
            lambda: _open_lf_report_dialog(_state.last_report_json),
        )

    with ui.tabs().classes("w-full") as tabs:
        map_tab = ui.tab("Network Map")
        nad_tab = ui.tab("Network Area Diagram")
        sld_tab = ui.tab("Single Line Diagram")
        data_tab = ui.tab("Data Explorer Components")
    panels = ui.tab_panels(tabs, value=map_tab).classes("w-full").props("keep-alive")
    with panels:
        with ui.tab_panel(map_tab).classes("q-pa-none w-full"):
            # ``sanitize=False`` because NiceGUI 3.x strips ``<iframe>`` tags
            # from sanitized HTML — the bundles are served from our own
            # static mount so the iframe is trusted. The wrapping
            # ``ui.html`` is forced to ``w-full`` so the iframe's
            # ``width:100%`` resolves against the full panel width
            # instead of collapsing to its natural width.
            ui.html(
                f'<iframe id="iidm-map-iframe" src="{_MAP_URL}/index.html" '
                'style="width:100%;height:670px;border:none;display:block"></iframe>',
                sanitize=False,
            ).classes("w-full")
        with ui.tab_panel(nad_tab).classes("q-pa-none w-full"):
            with ui.row().classes("q-pa-sm items-center"):
                ui.label("Depth:")
                depth_input = ui.number(value=_nad_depth, min=0, max=10, step=1, format="%d") \
                    .props("dense outlined").classes("w-24")
                nad_caption = ui.label(
                    "Click any node to jump to its Single Line Diagram."
                ).classes("text-caption q-ml-md")

                def _on_depth_changed(_e=None):
                    # NiceGUI 3.x's ``.on('update:model-value', …)`` hands
                    # back a ``GenericEventArguments`` (with ``args``) — not
                    # the 2.x ``ValueChangeEventArguments`` (with ``value``).
                    # Read the new value off the widget itself; that works
                    # on every NiceGUI version and ignores the event shape.
                    global _nad_depth
                    try:
                        _nad_depth = max(0, int(depth_input.value))
                    except (TypeError, ValueError):
                        return
                    if _state.selected_vl:
                        _push_nad(_state.selected_vl, _nad_depth)

                depth_input.on("update:model-value", _on_depth_changed)
            ui.html(
                f'<iframe id="iidm-nad-iframe" src="{_NAD_URL}/index.html" '
                'style="width:100%;height:700px;border:none;display:block"></iframe>',
                sanitize=False,
            ).classes("w-full")
        with ui.tab_panel(sld_tab).classes("q-pa-none w-full"):
            ui.html(
                f'<iframe id="iidm-sld-iframe" src="{_SLD_URL}/index.html" '
                'style="width:100%;height:700px;border:none;display:block"></iframe>',
                sanitize=False,
            ).classes("w-full")
        with ui.tab_panel(data_tab).classes("w-full"):
            refresh_data_grid = _build_data_explorer()

    # ------------------------------------------------------------------
    # Cross-tab navigation: substation click on map -> SLD tab on that VL.
    # ------------------------------------------------------------------
    def _on_state_network(network):
        # Enable the Run-LF button + reset status whenever the network
        # changes (load / clear). Also refresh the VL picker so it
        # carries the new network's voltage levels.
        run_lf_btn.set_enabled(network is not None)
        view_logs_btn.set_enabled(False)
        save_btn.set_enabled(network is not None)
        lf_status_lbl.set_text("")
        if network is None:
            vl_picker_state["df"] = None
            vl_filter_input.visible = False
            vl_select.visible = False
            _rebuild_vl_options()
            return
        try:
            from iidm_viewer.network_loader import (
                list_voltage_levels_for_selector,
            )
            vl_picker_state["df"] = list_voltage_levels_for_selector(network)
        except Exception:
            vl_picker_state["df"] = None
        vl_filter_input.visible = True
        vl_select.visible = True
        _rebuild_vl_options()
        _push_map()
        tabs.set_value(map_tab)
        refresh_data_grid()

    def _on_state_vl(vl_id):
        vl_lbl.set_text(f"VL: {vl_id}" if vl_id else "VL: —")
        # Keep the dropdown in sync with externally-set VLs (map / NAD
        # click, default-VL pick). Skip the listener so we don't loop.
        if vl_id and vl_id in (vl_select.options or {}):
            vl_picker_state["suppress_listener"] = True
            try:
                vl_select.value = vl_id
                vl_select.update()
            finally:
                vl_picker_state["suppress_listener"] = False
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
        # Every ``iidm-component-ready`` event resends the latest cached
        # args. q-tab-panels defaults to ``keep-alive=false``, so each
        # tab switch destroys and remounts the iframe; without this
        # resend the user sees a blank diagram after the first switch.
        global _map_ready, _nad_ready, _sld_ready
        component = e.args.get("component")
        if component == "map":
            _map_ready = True
            if _last_map is not None:
                _send_render("map", _last_map)
        elif component == "nad":
            _nad_ready = True
            if _last_nad is not None:
                _send_render("nad", _last_nad)
        elif component == "sld":
            _sld_ready = True
            if _last_sld is not None:
                _send_render("sld", _last_sld)

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


def _native_backend_available() -> bool:
    """Return True if pywebview can find a usable GUI backend.

    pywebview tries GTK (via PyGObject's ``gi``) on Linux first, then a
    Qt backend (PyQt5/PyQt6/PySide2/PySide6 with QtWebEngine). On macOS
    + Windows it uses Cocoa / EdgeChromium and the modules below aren't
    needed, so we treat those platforms as always-supported and only
    probe on Linux.
    """
    import platform
    if platform.system() != "Linux":
        return True
    for mod in (
        "gi",
        "PySide6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineWidgets",
        "PySide2.QtWebEngineWidgets",
        "PyQt5.QtWebEngineWidgets",
    ):
        try:
            __import__(mod)
            return True
        except Exception:
            continue
    return False


def run_app(initial_file: Optional[str] = None, native: bool = True, port: int = 8669) -> None:
    """Boot the NiceGUI server.

    ``native=True`` opens in a pywebview window — desktop-app feel.
    ``native=False`` runs as a plain localhost server you connect to
    from any browser; handy for testing without GUI libs.

    If ``native=True`` is requested but no pywebview backend is
    available (e.g. Linux without PyGObject or Qt+QtWebEngine), falls
    back to browser mode with a one-line warning so the app still runs.
    """
    if native and not _native_backend_available():
        import sys
        print(
            "warning: --native requested but no pywebview backend is available.\n"
            "  To enable a native window on Linux:\n"
            "    sudo apt install gir1.2-gtk-3.0 gir1.2-webkit2-4.1 "
            "libcairo2-dev libgirepository1.0-dev\n"
            "    pip install 'pywebview[gtk]'\n"
            "  (Qt alternative: pip install 'pywebview[qt]'.)\n"
            f"  Falling back to browser mode at http://localhost:{port}/.",
            file=sys.stderr,
        )
        native = False
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
