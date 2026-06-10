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

from iidm_viewer import script_recorder
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
    REMOVABLE_COMPONENTS,
    TOPOLOGY_AFFECTING_ATTRIBUTES,
    apply_cell_edit,
    editable_attributes,
    get_dataframe,
    is_editable,
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
    apply_and_log_bulk_disconnect,
    apply_and_log_bulk_edit,
    build_data_explorer_view_model,
    dataframe_to_csv,
    delete_and_log_elements,
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

# Diagram caches now live in ``_state.cache_backend`` — the same
# host-agnostic backend the PySide6 prototype uses and the Streamlit
# host's :mod:`iidm_viewer.caches` plugs into. ``_get_sld_cache`` /
# ``_get_nad_cache`` return the live slot dict so reads, writes and
# mutations stay routed through one place; the module-level
# ``__getattr__`` below keeps ``app._sld_cache`` / ``app._nad_cache``
# working for tests that still access them as attributes.
def _get_sld_cache() -> dict:
    from iidm_viewer.cache_backend import SLD
    return _state.cache_backend.setdefault(SLD, {})


def _get_nad_cache() -> dict:
    from iidm_viewer.cache_backend import NAD
    return _state.cache_backend.setdefault(NAD, {})


def _invalidate_diagram_caches() -> None:
    """Drop every cache slot affected by a topology mutation.

    Replaces the ``_nad_cache.clear(); _sld_cache.clear()`` pair that
    used to be sprinkled across every edit handler. Routed through
    :func:`cache_backend.invalidate_topology` so future tabs that
    register their own slots get the same lifecycle for free.
    """
    from iidm_viewer.cache_backend import invalidate_topology
    invalidate_topology(_state.cache_backend)


def __getattr__(name: str):
    """Module-level shim: expose ``_sld_cache`` / ``_nad_cache`` as
    live views of the cache-backend slots so test code that asserts
    on ``app._sld_cache == {}`` after :func:`_clear_diagrams` keeps
    working unchanged. PEP 562: only fires on module attribute
    lookups that miss ``__dict__``; in-module references must still
    call :func:`_get_sld_cache` / :func:`_get_nad_cache` directly.
    """
    if name == "_sld_cache":
        return _get_sld_cache()
    if name == "_nad_cache":
        return _get_nad_cache()
    raise AttributeError(name)

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
    create_empty as _create_empty_network,
    export_network,
    get_export_formats,
    guess_mime_for_export,
)
from iidm_viewer.network_reduction_actions import (
    REDUCTION_METHODS,
    list_voltage_level_ids,
    reduce_by_ids,
    reduce_by_ids_and_depths,
    reduce_by_voltage_range,
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


_sld_show_substation: bool = False


def _get_substation_for_vl(vl_id: str):
    """Return ``(substation_id, multi_vl)`` for *vl_id*, or ``(None, False)``."""
    if _state.network is None:
        return None, False
    try:
        vls = _state.network.get_voltage_levels(all_attributes=True)
        if vls.empty or "substation_id" not in vls.columns:
            return None, False
        row = vls.loc[vl_id] if vl_id in vls.index else None
        if row is None:
            return None, False
        sid = str(row["substation_id"]) if row.get("substation_id") else None
        if sid is None:
            return None, False
        multi = int((vls["substation_id"] == sid).sum()) > 1
        return sid, multi
    except Exception:
        return None, False


def _push_sld(vl_id: str) -> None:
    global _last_sld, _sld_show_substation
    if not vl_id or _state.network is None:
        return

    sid, multi_vl = _get_substation_for_vl(vl_id)

    if _sld_show_substation and sid:
        container_id = sid
        svg_type = "substation"
    else:
        container_id = vl_id
        svg_type = "voltage-level"

    entry = _get_sld_cache().get(container_id)
    if entry is None:
        try:
            entry = _generate_sld(_state.network, container_id)
        except Exception as exc:
            ui.notify(f"SLD generation failed for {container_id}: {exc}", type="negative")
            return
        _get_sld_cache()[container_id] = entry
    svg, metadata = entry
    args = {
        "svg": svg, "metadata": metadata,
        "height": 700, "svgType": svg_type,
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
    _invalidate_diagram_caches()
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
    entry = _get_nad_cache().get(key)
    if entry is None:
        try:
            entry = _generate_nad(_state.network, vl_id, int(depth))
        except Exception as exc:
            ui.notify(f"NAD generation failed for {vl_id}: {exc}", type="negative")
            return
        _get_nad_cache()[key] = entry
    svg, metadata = entry
    args = {"svg": svg, "metadata": metadata, "height": 700}
    _last_nad = args
    if _nad_ready:
        _send_render("nad", args)


def _clear_diagrams() -> None:
    """Wipe the NAD + SLD iframes and their caches.

    Called when the open network is swapped (load / start-empty /
    reduction). Without this the previous network's diagrams stay
    visible until the user picks a VL — and for an empty network no VL
    can be picked, so the stale SVG would never go away.
    """
    global _last_nad, _last_sld, _sld_show_substation
    # Network-swap context: wipe both diagram slots regardless of the
    # broader topology/LF semantics (``invalidate_topology`` keeps NAD
    # around per the Streamlit contract — but on a swap we want both
    # gone). The mid-app edit sites still go through
    # :func:`_invalidate_diagram_caches`.
    from iidm_viewer.cache_backend import NAD, SLD
    _state.cache_backend.pop(SLD, None)
    _state.cache_backend.pop(NAD, None)
    _sld_show_substation = False
    blank_sld = {"svg": "", "metadata": "", "height": 700,
                 "svgType": "voltage-level"}
    blank_nad = {"svg": "", "metadata": "", "height": 700}
    _last_sld = blank_sld
    _last_nad = blank_nad
    if _sld_ready:
        _send_render("sld", blank_sld)
    if _nad_ready:
        _send_render("nad", blank_nad)


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
        with params_box:
            params_container = ui.column().classes("w-full")

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


def _open_blank_network_dialog(file_lbl) -> None:
    """Mirror Streamlit's "Start with empty network" dialog.

    Prompts for a network id (default ``"network"``) then installs a
    fresh pypowsybl ``create_empty`` Network through the AppState.
    Users build it up via the Data Explorer's "Create a new …" forms.
    """
    with ui.dialog() as dialog, ui.card().style("min-width: 360px"):
        ui.label("Start with empty network").classes("text-h6")
        ui.label("Pick an id for the new network:").classes("text-caption")
        nid_input = ui.input(value="network") \
            .props("dense outlined").classes("full-width q-mb-md")

        def _on_create() -> None:
            network_id = (nid_input.value or "network").strip() or "network"
            try:
                network = _create_empty_network(network_id)
                _state.install_network(network)
                script_recorder.record_create_empty(network_id)
            except Exception as exc:
                ui.notify(f"Empty network failed: {exc}", type="negative")
                return
            file_lbl.set_text(f"(empty: {network_id})")
            ui.notify(
                f"Started empty network — id: {network_id}.",
                type="positive", timeout=1500,
            )
            dialog.close()

        with ui.row().classes("full-width justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=_on_create).props("color=primary")

    dialog.open()


def _open_session_script_dialog() -> None:
    """Open the "Session Script" modal — NiceGUI counterpart of the
    Streamlit + PySide6 dialogs.

    Renders the script produced by
    :func:`iidm_viewer.script_generator.generate_script` against the
    op log carried by :mod:`iidm_viewer.script_recorder`. Provides a
    Recording pause toggle, an "Include reverted edits" toggle, a
    download button and a clear-log button.
    """
    from datetime import datetime

    from iidm_viewer.script_generator import generate_script

    state: dict = {"include_reverted": False, "script": ""}

    with ui.dialog() as dialog, ui.card().style(
        "min-width: 720px; max-width: 95vw; max-height: 90vh",
    ):
        ui.label("Session Script").classes("text-h6")
        ui.label(
            "A runnable Python script that replays the operations you "
            "have performed in this session against any pypowsybl-"
            "loadable network."
        ).classes("text-caption")

        with ui.row().classes("items-center q-mb-sm"):
            recording_toggle = ui.switch(
                "Recording", value=not script_recorder.is_paused(),
            )
            include_reverted_toggle = ui.switch(
                "Include reverted edits", value=False,
            )

        paused_lbl = ui.label(
            "Recording is paused — new operations will not be captured."
        ).classes(
            "q-pa-sm bg-yellow-1 text-orange-9 rounded-borders"
        )
        paused_lbl.visible = script_recorder.is_paused()
        count_lbl = ui.label("").classes("text-caption q-mt-sm")
        preview = ui.codemirror(
            value="", language="python", theme="vscodeLight",
        ).props("readonly").classes("w-full").style("height: 360px")

        def _rerender() -> None:
            ops = script_recorder.get_log()
            include_reverted = bool(include_reverted_toggle.value)
            source_filename = script_recorder.get_source_filename()
            script = generate_script(
                ops,
                include_reverted=include_reverted,
                source_filename=source_filename,
            )
            state["script"] = script
            state["include_reverted"] = include_reverted
            preview.value = script
            preview.update()
            visible_count = sum(
                1 for o in ops if include_reverted or not o.get("reverted")
            )
            total = len(ops)
            reverted = total - sum(1 for o in ops if not o.get("reverted"))
            src_blurb = f" — source: {source_filename}" if source_filename else ""
            rev_blurb = f" ({reverted} reverted)" if reverted else ""
            count_lbl.set_text(
                f"{visible_count} of {total} operation(s) emitted{rev_blurb}{src_blurb}"
            )

        def _on_recording_changed(_e=None) -> None:
            paused = not bool(recording_toggle.value)
            script_recorder.set_paused(paused)
            paused_lbl.visible = paused

        recording_toggle.on("update:model-value", _on_recording_changed)
        include_reverted_toggle.on("update:model-value", lambda _e=None: _rerender())

        def _on_download() -> None:
            ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
            ui.download.content(
                state["script"].encode("utf-8"),
                filename=f"session_{ts_tag}.py",
                media_type="text/x-python",
            )

        def _on_clear() -> None:
            script_recorder.clear_log()
            recording_toggle.value = True
            recording_toggle.update()
            paused_lbl.visible = False
            _rerender()

        with ui.row().classes("full-width justify-end q-mt-md"):
            ui.button("Download", on_click=_on_download).props("color=primary")
            ui.button("Clear log", on_click=_on_clear).props("flat")
            ui.button("Close", on_click=dialog.close).props("flat")

    _rerender()
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
        with params_box:
            params_container = ui.column().classes("w-full")

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


def _open_network_reduction_dialog() -> None:
    """Modal for the three pypowsybl reduction methods.

    Irreversible — the warning banner at the top mirrors Streamlit's.
    On a successful Apply the helper calls
    :meth:`AppState.notify_network_changed` so every listener
    (diagram caches, data explorer, VL picker) refreshes against the
    reduced topology.
    """
    if _state.network is None:
        ui.notify("No network loaded.", type="warning")
        return
    try:
        vl_ids = list_voltage_level_ids(_state.network)
    except Exception:
        vl_ids = []

    panels_state: dict = {"applied": False}

    with ui.dialog() as dialog, ui.card().style(
        "min-width: 640px; max-width: 95vw; max-height: 90vh",
    ):
        ui.label("Network Reduction").classes("text-h6")
        ui.label(
            "⚠ Irreversible operation. The network will be permanently "
            "modified. To recover the original, reload the file.",
        ).classes("text-body2 q-pa-sm").style(
            "background: #fde3e3; color: #5a0000; border-radius: 3px; "
            "border: 1px solid #f5a5a5;",
        )

        method_select = ui.select(
            options=list(REDUCTION_METHODS),
            value=REDUCTION_METHODS[0],
            label="Reduction method",
        ).props("dense outlined").classes("full-width q-mt-md")
        with_boundary = ui.checkbox(
            "Replace cut lines with boundary lines",
        ).tooltip(
            "Lines cut at the reduction boundary are replaced by boundary lines.",
        )

        # One container per mode; toggled via method_select.
        range_box = ui.column().classes("full-width q-mt-sm")
        ids_box = ui.column().classes("full-width q-mt-sm")
        depths_box = ui.column().classes("full-width q-mt-sm")

        with range_box:
            ui.label(
                "Keep all elements whose nominal voltage is within the "
                "specified range (kV).",
            ).classes("text-caption")
            with ui.row().classes("full-width items-center"):
                ui.label("Minimum (kV)")
                v_min_input = ui.number(value=0.0, min=0.0, format="%.2f") \
                    .props("dense outlined").classes("w-32")
                ui.label("Maximum (kV)").classes("q-ml-md")
                v_max_input = ui.number(value=9999.0, min=0.0, format="%.2f") \
                    .props("dense outlined").classes("w-32")

        with ids_box:
            ui.label(
                "Keep only the specified voltage levels and all elements "
                "between them.",
            ).classes("text-caption")
            ids_select = ui.select(
                options=list(vl_ids),
                value=[], multiple=True,
                label="Voltage levels to keep",
            ).props("dense outlined use-chips").classes("full-width")

        with depths_box:
            ui.label(
                "Keep the specified voltage levels and their neighbours up to "
                "the given depth (applied to every selected voltage level).",
            ).classes("text-caption")
            depth_ids_select = ui.select(
                options=list(vl_ids),
                value=[], multiple=True,
                label="Voltage levels",
            ).props("dense outlined use-chips").classes("full-width")
            with ui.row().classes("items-center"):
                ui.label("Depth")
                depth_input = ui.number(
                    value=1, min=0, max=100, step=1, format="%d",
                ).props("dense outlined").classes("w-24")

        status_lbl = ui.label("").classes("text-caption text-negative q-mt-sm")

        def _refresh_panels() -> None:
            method = method_select.value
            range_box.visible = method == "By Voltage Range"
            ids_box.visible = method == "By Voltage Level IDs"
            depths_box.visible = method == "By Voltage Level IDs and Depths"
            status_lbl.set_text("")

        method_select.on("update:model-value", lambda *_: _refresh_panels())
        _refresh_panels()

        def _on_apply_click() -> None:
            status_lbl.set_text("")
            method = method_select.value
            wbl = bool(with_boundary.value)
            try:
                if method == "By Voltage Range":
                    reduce_by_voltage_range(
                        _state.network,
                        v_min_input.value, v_max_input.value,
                        with_boundary_lines=wbl,
                    )
                elif method == "By Voltage Level IDs":
                    reduce_by_ids(
                        _state.network,
                        ids_select.value or [],
                        with_boundary_lines=wbl,
                    )
                else:
                    reduce_by_ids_and_depths(
                        _state.network,
                        depth_ids_select.value or [],
                        depth_input.value or 0,
                        with_boundary_lines=wbl,
                    )
            except ValueError as exc:
                status_lbl.set_text(str(exc))
                return
            except Exception as exc:
                status_lbl.set_text(f"Reduction failed: {exc}")
                return
            panels_state["applied"] = True
            ui.notify("Network reduction applied.", type="positive", timeout=1500)
            dialog.close()
            _state.notify_network_changed()

        with ui.row().classes("full-width justify-end q-mt-md"):
            ui.button("Close", on_click=dialog.close).props("flat")
            ui.button("Apply Reduction", on_click=_on_apply_click) \
                .props("color=primary")

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
    # Wrap long pypowsybl column names (``regulated_element_id``,
    # ``voltage_regulator_on``, …) onto a second line instead of
    # truncating them with an ellipsis. ``autoHeaderHeight`` grows
    # the header row so the wrapped text stays visible.
    "wrapHeaderText": True,
    "autoHeaderHeight": True,
    # Minimum column width so wide tables (generators, lines) keep
    # cell values readable.  The grid scrolls horizontally instead
    # of squeezing every column into the viewport.
    "minWidth": 100,
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
        _invalidate_diagram_caches()
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

    Hidden entirely when the active component isn't in
    :data:`CREATABLE_COMPONENTS` or no network is loaded. When a
    network *is* loaded but it has no node-breaker voltage levels (or
    the selected VL has no busbar sections), the expansion stays
    visible with an inline info message — same UX as Streamlit's
    ``st.info`` placeholder. Never fires ``ui.notify`` from refresh:
    that toast pops on every redraw and feels like the app is blocking.
    """
    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if component not in CREATABLE_COMPONENTS or _state.network is None:
        expansion.visible = False
        return

    expansion.visible = True

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
    container = state["fields_container"]

    if not vl_options:
        # No node-breaker VLs — surface a friendly note inside the
        # form rather than spamming a toast.
        container.clear()
        state["field_widgets"] = {}
        with container:
            ui.label(
                f"{component} creation is currently limited to "
                "node-breaker voltage levels; none were found in this network.",
            ).classes("text-caption")
        state["bbs_select"].options = []
        state["bbs_select"].value = None
        state["bbs_select"].update()
        return

    # Populate busbar sections for the freshly-selected VL.
    try:
        ids = list_busbar_sections(_state.network, str(state["vl_select"].value))
    except Exception:
        ids = []
    state["bbs_select"].options = ids
    state["bbs_select"].value = ids[0] if ids else None
    state["bbs_select"].update()

    container.clear()
    state["field_widgets"] = {}
    if not ids:
        # The selected VL exists but carries no busbar sections —
        # creation needs one. Mirror Streamlit's inline warning.
        with container:
            ui.label(
                "No busbar sections found in the selected voltage level. "
                "Create one first to build feeders here.",
            ).classes("text-caption")
        return

    # Rebuild the field widgets.
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
        _invalidate_diagram_caches()
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
        _invalidate_diagram_caches()
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
        _invalidate_diagram_caches()
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
        _invalidate_diagram_caches()
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


def _build_create_extension_panel_widgets(state: dict, refresh_after_create) -> None:
    """Materialise the "Attach extension" expansion.

    Mirrors the Streamlit ``_render_create_extension_form``: an
    extension picker (filtered to entries whose ``targets`` map
    contains the active component), a target picker (existing element
    ids), and the per-field widgets built from the registry. The
    framework-agnostic registry + dispatcher live in
    :mod:`iidm_viewer.extension_creation`.
    """
    from iidm_viewer.extension_creation import CREATABLE_EXTENSIONS

    expansion = ui.expansion("Attach extension", icon="extension") \
        .classes("w-full")
    expansion.visible = False
    state["expansion"] = expansion
    state["current_component"] = ""
    state["extension"] = None
    state["field_widgets"] = {}

    with expansion:
        with ui.row().classes("items-center w-full q-pa-sm"):
            ui.label("Extension:")
            ext_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
            ui.label("Target:")
            target_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
        detail_label = ui.label("") \
            .classes("text-caption q-px-sm q-mb-sm text-italic")
        fields_container = ui.row().classes("items-start w-full q-pa-sm flex-wrap")
        with ui.row().classes("items-center w-full q-pa-sm"):
            create_btn = ui.button("Create", icon="add_circle")
            status_label = ui.label("").classes("text-caption q-ml-md")

    state["ext_select"] = ext_select
    state["target_select"] = target_select
    state["detail_label"] = detail_label
    state["fields_container"] = fields_container
    state["status_label"] = status_label
    state["create_btn"] = create_btn

    def _on_extension_changed(_e=None) -> None:
        # Rebuild the field widgets + target picker for the new pick.
        component = state.get("current_component") or ""
        _populate_for_extension(state, component, ext_select.value)

    def _on_create_click() -> None:
        from iidm_viewer.extension_creation import create_extension

        if _state.network is None:
            return
        ext_name = state.get("extension")
        if not ext_name:
            return
        target_id = state["target_select"].value
        if not target_id:
            status_label.set_text("Pick a target first.")
            return
        schema = CREATABLE_EXTENSIONS.get(ext_name)
        if schema is None:
            return
        values: dict = {}
        for f in schema["fields"]:
            widget = state["field_widgets"].get(f["name"])
            if widget is None:
                continue
            values[f["name"]] = getattr(widget, "value", None)
        try:
            create_extension(_state.network, ext_name, str(target_id), values)
        except Exception as exc:
            status_label.set_text(f"Create failed — {exc}")
            ui.notify(f"Create failed: {exc}", type="negative")
            return
        status_label.set_text(
            f"Created {ext_name!r} on {target_id!r}.",
        )
        ui.notify(
            f"Created {ext_name!r} on {target_id!r}",
            type="positive", timeout=1500,
        )
        # Topology / extension change — flush diagram caches + refresh data.
        _invalidate_diagram_caches()
        if _state.selected_vl:
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)
        refresh_after_create()

    ext_select.on("update:model-value", _on_extension_changed)
    create_btn.on_click(_on_create_click)


def _populate_for_extension(state: dict, component: str, ext_name) -> None:
    """Rebuild the target picker + per-field widgets for the picked
    extension. Pulled out so :func:`_refresh_create_extension_panel`
    can call it after switching the active component."""
    from iidm_viewer.extension_creation import (
        CREATABLE_EXTENSIONS,
        list_extension_candidates,
    )

    state["extension"] = str(ext_name) if ext_name else None
    schema = CREATABLE_EXTENSIONS.get(state["extension"] or "")
    if schema is None or _state.network is None or not component:
        state["detail_label"].set_text("")
        state["target_select"].options = []
        state["target_select"].value = None
        state["target_select"].update()
        state["fields_container"].clear()
        state["field_widgets"] = {}
        return

    state["detail_label"].set_text(str(schema.get("detail") or ""))

    # Target candidates.
    try:
        ids = list_extension_candidates(
            _state.network, state["extension"], component,
        )
    except Exception:
        ids = []
    state["target_select"].options = list(ids)
    state["target_select"].value = ids[0] if ids else None
    state["target_select"].update()

    # Per-field widgets.
    container = state["fields_container"]
    container.clear()
    state["field_widgets"] = {}
    with container:
        for f in schema["fields"]:
            label_text = f["name"] + (" *" if f.get("required") else "")
            help_text = f.get("help") or ""
            with ui.column().classes("q-mr-md q-mb-md"):
                ui.label(label_text).classes("text-caption")
                kind = f["kind"]
                default = f.get("default")
                if kind == "bool":
                    w = ui.switch(value=bool(default))
                elif kind == "int":
                    w = ui.number(
                        value=int(default) if default is not None else 0,
                        step=1, format="%d",
                    ).props("dense outlined")
                elif kind == "float":
                    try:
                        v = float(default) if default is not None else 0.0
                    except (TypeError, ValueError):
                        v = 0.0
                    w = ui.number(value=v, format="%g") \
                        .props("dense outlined")
                elif kind == "choice":
                    options = list(f.get("options") or [])
                    value = (
                        str(default) if str(default) in options
                        else (options[0] if options else None)
                    )
                    w = ui.select(options=options, value=value) \
                        .props("dense outlined").classes("w-40")
                else:  # str (and any unknown kind)
                    w = ui.input(value="" if default in (None,) else str(default)) \
                        .props("dense outlined")
                if help_text:
                    w.tooltip(help_text)
                state["field_widgets"][f["name"]] = w


def _refresh_create_extension_panel(state: dict, component: str) -> None:
    """Repopulate the extensions panel for ``component``.

    Hides the expansion when the component isn't a target of any
    creatable extension or no network is loaded.
    """
    from iidm_viewer.extension_creation import list_extensions_for_component

    expansion = state.get("expansion")
    if expansion is None:
        return
    state["current_component"] = component
    if _state.network is None or not component:
        expansion.visible = False
        return
    names = list_extensions_for_component(component)
    if not names:
        expansion.visible = False
        return
    expansion.visible = True
    # Repopulate the extension dropdown — preserve the prior pick when
    # the new component still offers it.
    current = state["ext_select"].value if state["ext_select"].value in names else names[0]
    state["ext_select"].options = names
    state["ext_select"].value = current
    state["ext_select"].update()
    _populate_for_extension(state, component, current)
    state["status_label"].set_text("")


def _build_data_explorer(on_topology_changed=None):
    """Materialise the Data Explorer panel and return a refresh closure.

    The closure re-fetches the DataFrame for whatever component is
    selected in the combo and pushes it into the ag-Grid. Filter +
    sort are handled inside ag-Grid (per-column floating filters,
    default sort on header click). Edits are dispatched here via the
    ``cellValueChanged`` event.

    ``on_topology_changed`` is an optional callback invoked after a
    create / delete / disconnect that changes the network topology, so
    the caller can rebuild the VL picker and flush diagram caches.
    """
    with ui.row().classes("q-pa-sm items-center w-full"):
        ui.label("Component:")
        select = ui.select(
            options=list(COMPONENT_GETTERS),
            value="Generators",
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
    def _refresh_after_create():
        refresh()
        if on_topology_changed:
            on_topology_changed()

    create_state = {
        "container": None,
        "vl_select": None,
        "bbs_select": None,
        "field_widgets": {},
        "status_label": None,
        "expansion": None,
    }
    _build_create_panel_widgets(create_state, refresh_after_create=_refresh_after_create)

    branch_create_state = {
        "vl1_select": None, "vl2_select": None,
        "bbs1_select": None, "bbs2_select": None,
        "field_widgets": {},
        "status_label": None,
        "expansion": None,
    }
    _build_create_branch_panel_widgets(
        branch_create_state, refresh_after_create=_refresh_after_create,
    )

    container_create_state = {
        "context_select": None,
        "context_label": None,
        "field_widgets": {},
        "status_label": None,
        "expansion": None,
    }
    _build_create_container_panel_widgets(
        container_create_state, refresh_after_create=_refresh_after_create,
    )

    hvdc_create_state = {
        "cs1_select": None, "cs2_select": None,
        "field_widgets": {},
        "status_label": None,
        "expansion": None,
    }
    _build_create_hvdc_panel_widgets(
        hvdc_create_state, refresh_after_create=_refresh_after_create,
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
        tap_changer_create_state, refresh_after_create=_refresh_after_create,
    )

    coupling_create_state: dict = {
        "vl_select": None, "bbs1_select": None, "bbs2_select": None,
        "prefix_input": None,
        "status_label": None, "expansion": None,
        "refresh_bbs": None,
        "current_component": "",
    }
    _build_create_coupling_device_panel_widgets(
        coupling_create_state, refresh_after_create=_refresh_after_create,
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
        reactive_limits_create_state, refresh_after_create=_refresh_after_create,
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
        operational_limits_create_state, refresh_after_create=_refresh_after_create,
    )

    svc_create_state: dict = {
        "zone_count": None, "zones_container": None, "zone_widgets": [],
        "unit_count": None, "units_container": None, "unit_widgets": [],
        "status_label": None, "expansion": None,
        "rebuild_zones": None, "rebuild_units": None,
        "current_component": "",
    }
    _build_create_secondary_voltage_control_panel_widgets(
        svc_create_state, refresh_after_create=_refresh_after_create,
    )

    extension_create_state: dict = {
        "expansion": None,
        "ext_select": None,
        "target_select": None,
        "detail_label": None,
        "fields_container": None,
        "status_label": None,
        "create_btn": None,
        "field_widgets": {},
        "extension": None,
        "current_component": "",
    }
    _build_create_extension_panel_widgets(
        extension_create_state, refresh_after_create=_refresh_after_create,
    )

    grid = ui.aggrid({
        "columnDefs": [], "rowData": [],
        "defaultColDef": _DEFAULT_COL_DEF,
        "rowSelection": "multiple",
    }, auto_size_columns=False).classes("w-full").style("height: 600px")

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
        # Filter-by-selected-VL: only meaningful when applicable. The
        # VL toggle's visibility is independent of the view-model
        # (it's a host-side widget affordance), so compute it here.
        vl_applicable = (
            label in VL_FILTERABLE
            and _state.selected_vl is not None
        )
        vl_filter.visible = vl_applicable
        if vl_applicable:
            vl_filter.text = f"Filter by VL: {_state.selected_vl}"
        elif vl_filter.value:
            # Switched to a non-VL-filterable component; auto-uncheck.
            vl_filter.value = False

        # One shared call assembles the filter chain + editable / removable
        # derivation. NiceGUI doesn't apply structured per-column filters
        # in Python (ag-Grid handles client-side column filtering), so
        # ``filter_specs`` is omitted; the host adds an id-substring
        # filter via ag-Grid too.
        try:
            vm = build_data_explorer_view_model(
                _state.network,
                label,
                selected_vl=_state.selected_vl,
                filter_by_vl=(vl_applicable and vl_filter.value),
            )
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
        df = vm.rows_df
        original_rows = vm.total_count
        cols = list(vm.editable_cols)
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
        _refresh_create_extension_panel(extension_create_state, label)
        is_disconnectable = label in DISCONNECTABLE_COMPONENTS
        # ``is_removable`` comes from the view-model so PySide6 + NiceGUI
        # share one removability source of truth.
        is_removable = vm.is_removable
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
            _invalidate_diagram_caches()
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
            outcome = apply_and_log_bulk_edit(
                _state.network, component, ids, attribute, new_value,
                change_log=_state.change_log,
            )
        except Exception as exc:
            ui.notify(
                f"Bulk edit rejected — {component}/{len(ids)} rows/{attribute}: {exc}",
                type="negative",
            )
            return
        ui.notify(
            f"{component}: {attribute} = {outcome['display_value']} applied to {len(ids)} rows",
            type="positive",
            timeout=1500,
        )
        bulk_value.value = ""
        bulk_value.update()
        # Topology-affecting bulk changes flush the diagram caches so
        # a subsequent tab switch shows the updated picture.
        if outcome["topology_affecting"]:
            _invalidate_diagram_caches()
            if _state.selected_vl:
                _push_sld(_state.selected_vl)
                _push_nad(_state.selected_vl, _nad_depth)
        # Refresh the grid so the new (possibly coerced) values appear.
        refresh()
        # "Apply & Run LF" path: kick off the load flow after the edit.
        # The state listener handles cache flush + diagram refresh.
        if run_lf_after:
            try:
                result = await asyncio.to_thread(_state.run_loadflow_no_notify)
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
            # Notify loadflow listeners back on the event loop.
            for listener in list(_state._loadflow_listeners):
                listener(result)

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
            apply_and_log_bulk_disconnect(
                _state.network, component, ids,
                change_log=_state.change_log,
            )
        except Exception as exc:
            ui.notify(
                f"Disconnect rejected — {component}/{len(ids)} rows: {exc}",
                type="negative",
            )
            return
        ui.notify(
            f"{component}: disconnected {len(ids)} row(s)",
            type="positive",
            timeout=1500,
        )
        # Disconnect always touches a topology-affecting attribute, so
        # flush the diagram caches unconditionally.
        _invalidate_diagram_caches()
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
            df_before = get_enriched_dataframe(_state.network, component)
            snapshot_index = (
                df_before.set_index("id", drop=False)
                if "id" in df_before.columns
                else df_before
            )
        except Exception:
            snapshot_index = None
        try:
            removed = delete_and_log_elements(
                _state.network, component, ids,
                change_log=_state.change_log,
                snapshot_df=snapshot_index,
            )
        except Exception as exc:
            ui.notify(
                f"Delete failed — {component}/{len(ids)} rows: {exc}",
                type="negative",
            )
            return
        # Deletion always changes topology -> flush diagram caches.
        _invalidate_diagram_caches()
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


def _build_extensions_explorer():
    """Materialise the "Data Explorer Extensions" tab.

    Mirrors Streamlit's ``render_extensions_explorer``: an extension
    name picker, an ID-substring filter, the extension's DataFrame in
    an ag-Grid with editable cells (for entries in
    :data:`EDITABLE_EXTENSIONS`) + a "Remove" checkbox column, and
    Apply / Remove buttons. The framework-agnostic listing + worker-
    routed mutations live in :mod:`iidm_viewer.extensions_data`.

    Returns a closure the host calls when the network changes so the
    extension list + table refresh against the new state.
    """
    import pandas as pd

    from iidm_viewer.extensions_data import (
        ExtensionsExplorerViewModel,
        get_extension_df,
        get_extensions_information,
        list_extension_names,
        remove_extension,
        update_extension,
    )

    vm = ExtensionsExplorerViewModel()

    with ui.row().classes("items-center w-full q-pa-sm"):
        ui.label("Extension:")
        ext_select = ui.select(options=[], value=None) \
            .props("dense outlined").classes("w-64")
        filter_input = ui.input(placeholder="Filter by ID (substring)") \
            .props("dense outlined clearable").classes("flex-grow q-ml-md")
        summary_lbl = ui.label("").classes("text-caption q-ml-md")
    detail_lbl = ui.label("").classes("text-caption q-px-sm q-mb-sm")

    grid = ui.aggrid({
        "columnDefs": [], "rowData": [],
        "defaultColDef": _DEFAULT_COL_DEF,
        "rowSelection": "multiple",
    }, auto_size_columns=False).classes("w-full").style("height: 500px")

    status_lbl = ui.label("").classes("text-caption q-mb-sm")
    with ui.row().classes("items-center q-pa-sm"):
        apply_btn = ui.button("Apply changes").props("color=primary")
        remove_btn = ui.button("Remove ticked rows").props("color=negative")
        apply_btn.set_enabled(False)
        remove_btn.set_enabled(False)

    def _current_ext() -> Optional[str]:
        return ext_select.value or None

    def _aggrid_options(view: pd.DataFrame) -> dict:
        if view.empty:
            return {
                "columnDefs": [], "rowData": [],
                "defaultColDef": _DEFAULT_COL_DEF,
                "rowSelection": "multiple",
            }
        readonly = vm.is_readonly()
        editable_cols = set(vm.editable_cols(view))
        column_defs: list[dict] = []
        if not readonly:
            column_defs.append({
                "headerName": "Remove",
                "field": "_remove",
                "checkboxSelection": False,
                "editable": True,
                "cellRenderer": "agCheckboxCellRenderer",
                "cellEditor": "agCheckboxCellEditor",
                "width": 90,
                "pinned": "left",
            })
        column_defs.append({
            "headerName": "id",
            "field": "id",
            "editable": False,
            "pinned": "left",
        })
        for col in view.columns:
            column_defs.append({
                "headerName": str(col),
                "field": str(col),
                "editable": (str(col) in editable_cols) and not readonly,
                "sortable": True,
                "filter": True,
            })

        rows: list[dict] = []
        for idx, row in view.iterrows():
            eid = str(idx)
            r: dict = {"id": eid}
            if not readonly:
                r["_remove"] = vm.is_ticked(eid)
            for col in view.columns:
                c = str(col)
                edit = vm.get_edit(eid, c)
                if edit is not None:
                    r[c] = edit
                else:
                    v = row[col]
                    try:
                        if isinstance(v, float) and pd.isna(v):
                            r[c] = None
                            continue
                    except Exception:
                        pass
                    r[c] = v
            rows.append(r)
        return {
            "columnDefs": column_defs,
            "rowData": rows,
            "defaultColDef": _DEFAULT_COL_DEF,
            "rowSelection": "multiple",
        }

    def _empty_grid_options() -> dict:
        return {
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
            "rowSelection": "multiple",
        }

    def refresh() -> None:
        ext = _current_ext()
        status_lbl.set_text("")
        if _state.network is None or not ext:
            grid.options.update(_empty_grid_options())
            grid.update()
            summary_lbl.set_text("No network loaded." if _state.network is None else "Pick an extension.")
            detail_lbl.set_text("")
            apply_btn.set_enabled(False)
            remove_btn.set_enabled(False)
            return
        try:
            df = get_extension_df(_state.network, ext)
        except Exception as exc:
            summary_lbl.set_text(f"Failed to load {ext!r}: {exc}")
            grid.options.update(_empty_grid_options())
            grid.update()
            return
        vm.set_data(ext, df if df is not None else pd.DataFrame())
        detail_lbl.set_text(vm.detail())
        if vm.current_df.empty:
            summary_lbl.set_text(f"No {ext!r} extensions found.")
            grid.options.update(_empty_grid_options())
            grid.update()
            apply_btn.set_enabled(False)
            remove_btn.set_enabled(False)
            return
        total = len(vm.current_df)
        view = vm.filtered_view(filter_input.value or "")
        if view.empty:
            summary_lbl.set_text(f"No {ext!r} extensions match the filter.")
            grid.options.update(_empty_grid_options())
            grid.update()
            return
        readonly = vm.is_readonly()
        editable_cols = vm.editable_cols(view)
        grid.options.update(_aggrid_options(view))
        grid.update()
        if len(view) == total:
            summary_lbl.set_text(f"{total} {ext!r} extension(s)")
        else:
            summary_lbl.set_text(f"{len(view)} of {total} {ext!r} extension(s)")
        apply_btn.set_enabled(bool(editable_cols) and not readonly)
        remove_btn.set_enabled(not readonly)

    def _populate_extensions(network) -> None:
        if network is None:
            ext_select.options = []
            ext_select.value = None
            ext_select.update()
            vm.clear()
            return
        try:
            names = list_extension_names()
        except Exception:
            names = []
        try:
            vm.set_info(get_extensions_information())
        except Exception:
            vm.set_info(pd.DataFrame())
        ext_select.options = list(names)
        ext_select.value = names[0] if names else None
        ext_select.update()

    def _on_extension_changed(_e=None) -> None:
        vm.reset_pending()
        refresh()

    def _on_filter_changed(_e=None) -> None:
        refresh()

    ext_select.on("update:model-value", _on_extension_changed)
    filter_input.on_value_change(_on_filter_changed)

    def _on_cell_value_changed(e) -> None:
        """ag-Grid cell edit handler — caches pending edits + removals."""
        args = e.args or {}
        data = args.get("data") or {}
        col_id = args.get("colId") or args.get("column", {}).get("colId")
        new_value = args.get("newValue")
        element_id = str(data.get("id") or "")
        if not element_id or col_id is None:
            return
        ext = _current_ext()
        if not ext:
            return
        if col_id == "_remove":
            vm.tick_remove(element_id, bool(new_value))
            return
        # Cast based on the source DataFrame's column dtype.
        df = vm.current_df
        if element_id not in df.index or col_id not in df.columns:
            return
        casted = _cast_value_for_col(df[col_id], new_value)
        vm.add_edit(element_id, col_id, casted)

    grid.on("cellValueChanged", _on_cell_value_changed)

    def _on_apply_click() -> None:
        ext = _current_ext()
        if not ext or _state.network is None:
            return
        if not vm.has_edits():
            status_lbl.set_text("No pending changes.")
            return
        changes_df = vm.edits_changes_df()
        try:
            update_extension(_state.network, ext, changes_df)
        except Exception as exc:
            ui.notify(f"Update failed: {exc}", type="negative")
            return
        n = len(changes_df)
        vm.clear_edits()
        ui.notify(
            f"Applied {n} change(s) to {ext!r}.",
            type="positive", timeout=1500,
        )
        status_lbl.set_text(f"Applied {n} change(s).")
        refresh()

    def _on_remove_click() -> None:
        ext = _current_ext()
        if not ext or _state.network is None:
            return
        ids = vm.removals_list()
        if not ids:
            status_lbl.set_text("Tick at least one row to remove.")
            return
        try:
            remove_extension(_state.network, ext, ids)
        except Exception as exc:
            ui.notify(f"Remove failed: {exc}", type="negative")
            return
        vm.clear_removals()
        # Drop any cached edits for the just-removed rows.
        vm.drop_edits_for(ids)
        ui.notify(
            f"Removed {len(ids)} {ext!r} extension row(s).",
            type="positive", timeout=1500,
        )
        status_lbl.set_text(f"Removed {len(ids)} row(s).")
        refresh()

    apply_btn.on_click(_on_apply_click)
    remove_btn.on_click(_on_remove_click)

    # Listener on AppState — keeps the extensions tab in step with
    # network loads, reductions and other "the whole topology may have
    # changed" events. The host also calls ``refresh()`` from the LF
    # listener so post-LF extension columns surface.
    def _on_network_changed(network) -> None:
        _populate_extensions(network)
        vm.reset_pending()
        refresh()

    _state.on_network_changed(_on_network_changed)
    _populate_extensions(_state.network)
    refresh()

    return refresh


def _build_reactive_curves():
    """Materialise the "Reactive Capability Curves" tab.

    Renders the shared :class:`~iidm_viewer.reactive_curves.ReactiveCurvesViewModel`
    + :class:`GeneratorPlotData` + :class:`ContainmentSummary` against
    NiceGUI widgets:

    * a "Only generators in <VL>" checkbox when an upstream VL is
      selected,
    * a generator picker (the same list the view model exposes),
    * a metrics row (target_p / target_q / min_q / max_q / type),
    * a ``ui.plotly`` chart with the closed polygon + operating point
      + status-coloured target diamond,
    * a containment summary expansion with the four sub-frames the
      shared helper bucketises.

    Returns a closure the page-wide listeners call when the network or
    load-flow state changes.
    """
    import plotly.graph_objects as go

    from iidm_viewer.reactive_curves import (
        STATUS_DIAMOND_COLOR,
        build_containment_summary,
        build_generator_plot_data,
        build_reactive_curves_view_model,
    )

    state: dict = {"vm": None, "gen_id": None}

    only_vl_row = ui.row().classes("items-center q-pa-sm w-full")
    with only_vl_row:
        only_vl_checkbox = ui.checkbox(
            "Only generators in selected VL", value=False,
        )
    only_vl_row.visible = False

    gen_row = ui.row().classes("items-center q-pa-sm w-full")
    with gen_row:
        ui.label("Generator:")
        gen_select = ui.select(options=[], value=None) \
            .props("dense outlined").classes("w-64")
        gen_count_lbl = ui.label("").classes("text-caption q-ml-md")

    metrics_row = ui.row().classes("items-stretch q-pa-sm w-full no-wrap")
    with metrics_row:
        target_p_lbl = ui.label("target_p: —").classes("col")
        target_q_lbl = ui.label("target_q: —").classes("col")
        min_q_lbl = ui.label("min_q @ tp: —").classes("col")
        max_q_lbl = ui.label("max_q @ tp: —").classes("col")
        type_lbl = ui.label("Type: —").classes("col")
    sensitivity_caption = ui.label("").classes("text-caption q-pa-sm")
    sensitivity_caption.visible = False

    plot = ui.plotly(go.Figure()).classes("w-full").style("height: 500px")
    plot_caption = ui.label("").classes("text-caption q-pa-sm")
    placeholder = ui.label(
        "No generators with reactive limits in this network."
    ).classes("text-caption q-pa-md")
    placeholder.visible = False

    summary_expansion = ui.expansion(
        "Target P/Q containment", icon="rule", value=False,
    ).classes("w-full")
    with summary_expansion:
        summary_metrics = ui.row().classes("q-pa-sm w-full")
        with summary_metrics:
            inside_lbl = ui.label("Inside: —")
            warning_lbl = ui.label("Edge/Near: —")
            action_lbl = ui.label("Outside/Saturated: —")
            unknown_lbl = ui.label("Unknown: —")
        summary_caption = ui.label("").classes("text-caption q-pa-sm")
        summary_caption.visible = False
        summary_body = ui.column().classes("w-full")

    def _render_subset(label, df, *, default_open):
        if df.empty:
            return
        title = f"{label} — {len(df)}"
        with summary_body:
            with ui.expansion(title, value=default_open).classes("w-full"):
                ui.aggrid({
                    "columnDefs": [{"field": c, "headerName": c}
                                   for c in df.columns],
                    "rowData": df.reset_index().fillna("").to_dict("records"),
                    "defaultColDef": _DEFAULT_COL_DEF,
                }, auto_size_columns=False).classes("w-full").style("height: 240px")

    def _set_plot(vm, gen_id):
        plot_data = build_generator_plot_data(
            gen_id, vm.gens_df, vm.curves_df, vm.classified, vm.curve_gen_ids,
        )
        if plot_data is None:
            plot.figure = go.Figure()
            plot.update()
            plot_caption.set_text("")
            return
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=plot_data.polygon_p, y=plot_data.polygon_q,
            fill="toself",
            fillcolor="rgba(99, 110, 250, 0.15)",
            line=dict(color="rgb(99, 110, 250)"),
            name=plot_data.curve_label,
        ))
        if plot_data.operating_point is not None:
            op_p, op_q = plot_data.operating_point
            fig.add_trace(go.Scatter(
                x=[op_p], y=[op_q],
                mode="markers",
                marker=dict(size=12, color="red", symbol="x"),
                name=f"Operating (P={op_p:.1f}, Q={op_q:.1f})",
            ))
        if plot_data.target_point is not None:
            tp, tq, status, regulation = plot_data.target_point
            fig.add_trace(go.Scatter(
                x=[tp], y=[tq],
                mode="markers",
                marker=dict(
                    size=12,
                    color=STATUS_DIAMOND_COLOR.get(status, "green"),
                    symbol="diamond",
                ),
                name=(
                    f"Target [{regulation}] (P={tp:.1f}, Q={tq:.1f}, {status})"
                ),
            ))
        fig.update_layout(
            xaxis_title="P (MW)",
            yaxis_title="Q (MVar)",
            title=f"Reactive Capability Curve — {gen_id}",
            showlegend=True,
            height=500,
        )
        plot.figure = fig
        plot.update()
        if plot_data.has_curve and plot_data.curve_points is not None:
            plot_caption.set_text(
                f"{len(plot_data.curve_points)} curve points for {gen_id}"
            )
        else:
            plot_caption.set_text(f"Min-max reactive limits for {gen_id}")

    def _render_summary(vm):
        summary = build_containment_summary(vm.classified, vm.gens_df)
        inside_lbl.set_text(f"Inside: {summary.n_inside}")
        warning_lbl.set_text(f"Edge/Near: {summary.n_warning}")
        action_lbl.set_text(
            f"Outside/Saturated: {summary.n_action}"
            + (f" (PV→PQ: {summary.n_saturated})" if summary.n_saturated else "")
        )
        unknown_lbl.set_text(f"Unknown/Needs LF: {summary.n_unknown}")
        if summary.n_needs_lf:
            summary_caption.set_text(
                f"{summary.n_needs_lf} PV generator(s) need a load flow to "
                "evaluate their operating point against the diagram."
            )
            summary_caption.visible = True
        else:
            summary_caption.visible = False
        summary_body.clear()
        if summary.n_action + summary.n_warning == 0:
            with summary_body:
                ui.label("All targets are inside their capability curves.") \
                    .classes("text-positive q-pa-sm")
            return
        _render_subset("PQ outside (target_q infeasible)",
                       summary.pq_outside, default_open=True)
        _render_subset("PV saturated (LF clamped Q, switched to PQ)",
                       summary.pv_saturated, default_open=True)
        _render_subset("PQ on edge", summary.pq_edge, default_open=False)
        _render_subset("PV near saturation",
                       summary.pv_near_saturation, default_open=False)

    def _render_selected_gen():
        vm = state["vm"]
        gen_id = state["gen_id"]
        if vm is None or gen_id is None or gen_id not in vm.gens_df.index:
            for lbl, prefix in (
                (target_p_lbl, "target_p"),
                (target_q_lbl, "target_q"),
                (min_q_lbl, "min_q @ tp"),
                (max_q_lbl, "max_q @ tp"),
                (type_lbl, "Type"),
            ):
                lbl.set_text(f"{prefix}: —")
            sensitivity_caption.visible = False
            plot.figure = go.Figure()
            plot.update()
            plot_caption.set_text("")
            return
        gen_row = vm.gens_df.loc[gen_id]
        classified_row = (
            vm.classified.loc[gen_id]
            if gen_id in vm.classified.index
            else pd.Series(dtype="object")
        )
        target_p_lbl.set_text(
            f"target_p: {gen_row.get('target_p', float('nan')):.1f} MW"
        )
        target_q_lbl.set_text(
            f"target_q: {gen_row.get('target_q', float('nan')):.1f} MVar"
        )
        min_q_lbl.set_text(
            f"min_q @ tp: {gen_row.get('min_q_at_target_p', float('nan')):.1f} MVar"
        )
        max_q_lbl.set_text(
            f"max_q @ tp: {gen_row.get('max_q_at_target_p', float('nan')):.1f} MVar"
        )
        type_lbl.set_text(f"Type: {classified_row.get('regulation', '?')}")

        # Sensitivity caption — only for voltage-regulating gens.
        sensitivity_caption.visible = False
        if bool(gen_row.get("voltage_regulator_on", False)):
            try:
                from iidm_viewer.reactive_curves import (
                    compute_target_v_q_sensitivity,
                )
                sens = compute_target_v_q_sensitivity(_state.network, gen_id)
            except Exception:
                sens = None
            if sens is not None:
                dq_dv, q_ref = sens
                sensitivity_caption.set_text(
                    f"dQ_bus / dV_target ≈ {dq_dv:+.2f} MVar/kV "
                    f"(BUS_REACTIVE_POWER ref = {q_ref:.2f} MVar)."
                )
                sensitivity_caption.visible = True

        _set_plot(vm, gen_id)

    def _on_gen_changed(_e=None):
        state["gen_id"] = gen_select.value
        _render_selected_gen()

    gen_select.on("update:model-value", _on_gen_changed)

    def _on_only_vl_changed(_e=None):
        refresh()

    only_vl_checkbox.on("update:model-value", _on_only_vl_changed)

    def refresh() -> None:
        if _state.network is None:
            state["vm"] = None
            state["gen_id"] = None
            placeholder.set_text("Load a network first.")
            placeholder.visible = True
            only_vl_row.visible = False
            gen_select.options = []
            gen_select.update()
            gen_count_lbl.set_text("")
            summary_expansion.value = False
            _render_selected_gen()
            return
        # Sync "only_vl" affordance with the upstream-selected VL.
        only_vl_label = (
            f"Only generators in VL {_state.selected_vl}"
            if _state.selected_vl else ""
        )
        if _state.selected_vl:
            only_vl_checkbox.set_text(only_vl_label)
            only_vl_row.visible = True
        else:
            only_vl_row.visible = False
        only_vl = bool(only_vl_checkbox.value) and bool(_state.selected_vl)
        try:
            vm = build_reactive_curves_view_model(
                _state.network,
                only_vl=_state.selected_vl if only_vl else None,
            )
        except Exception as exc:
            placeholder.set_text(f"Reactive curves failed: {exc}")
            placeholder.visible = True
            state["vm"] = None
            state["gen_id"] = None
            gen_select.options = []
            gen_select.update()
            _render_selected_gen()
            return
        if vm is None or vm.gens_df.empty:
            placeholder.set_text("No generators with reactive limits in this network.")
            placeholder.visible = True
            state["vm"] = None
            state["gen_id"] = None
            gen_select.options = []
            gen_select.update()
            gen_count_lbl.set_text("")
            _render_selected_gen()
            return
        placeholder.visible = False
        state["vm"] = vm
        gen_ids = list(vm.gens_df.index)
        gen_count_lbl.set_text(f"{len(gen_ids)} generators with reactive limits")
        gen_select.options = gen_ids
        current = state["gen_id"]
        if current not in gen_ids:
            current = gen_ids[0]
        state["gen_id"] = current
        gen_select.value = current
        gen_select.update()
        _render_selected_gen()
        _render_summary(vm)

    refresh()
    return refresh


def _build_operational_limits():
    """Materialise the "Operational Limits" tab.

    Composes the shared :class:`~iidm_viewer.operational_limits.OperationalLimitsViewModel`
    + :func:`build_element_chart` against NiceGUI widgets:

    * a "Most loaded" section with a threshold slider, an ag-Grid
      table sorted by descending loading %,
    * a per-element detail view: ID-substring filter, a generator-
      style ``ui.select``, a metric label for losses, the shared
      Plotly chart, and a raw limits table for the selected element.

    Returns a closure the page-wide listeners call when the network or
    load-flow state changes.
    """
    import pandas as pd
    import plotly.graph_objects as go

    from iidm_viewer.operational_limits import (
        build_element_chart,
        build_operational_limits_view_model,
    )

    state: dict = {
        "vm": None, "element_id": None,
        "threshold": 50, "id_filter": "",
    }

    placeholder = ui.label(
        "Load a network to see operational limits."
    ).classes("text-caption q-pa-md")

    # --- Most loaded section --------------------------------------------
    most_loaded_group = ui.column().classes("w-full q-pa-sm")
    with most_loaded_group:
        ui.label("Most loaded elements").classes("text-h6")
        with ui.row().classes("items-center w-full"):
            ui.label("Show elements loaded above")
            threshold_slider = ui.slider(
                min=0, max=100, value=50, step=1,
            ).props("label-always").classes("flex-grow")
            ui.label("%")
        loading_caption = ui.label("").classes("text-caption q-mb-sm")
        loading_placeholder = ui.label(
            "No loading data available (run a load flow first)."
        ).classes("text-caption text-orange")
        loading_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 280px")

    # --- Element detail section ----------------------------------------
    element_group = ui.column().classes("w-full q-pa-sm")
    with element_group:
        ui.label("Element detail").classes("text-h6")
        with ui.row().classes("items-center w-full"):
            id_filter_input = ui.input(
                placeholder="Filter by element ID (substring, case-insensitive)",
            ).props("dense outlined clearable").classes("w-96")
            element_count_lbl = ui.label("").classes("text-caption q-ml-md")
        with ui.row().classes("items-center w-full"):
            ui.label("Element:")
            element_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-72")
        losses_lbl = ui.label("").classes("q-pa-sm")
        element_plot = ui.plotly(go.Figure()).classes("w-full") \
            .style("height: 450px")
        ui.label("Limits for the selected element:") \
            .classes("text-caption q-mt-sm")
        element_limits_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 220px")

    def _set_data_visible(visible: bool) -> None:
        most_loaded_group.visible = visible
        element_group.visible = visible
        placeholder.visible = not visible

    def _render_loading_table() -> None:
        vm = state["vm"]
        if vm is None:
            return
        threshold = int(state["threshold"])
        loading = vm.loading_df
        if loading is None or loading.empty:
            loading_placeholder.visible = True
            loading_grid.options.update({"columnDefs": [], "rowData": []})
            loading_grid.update()
            loading_caption.set_text("")
            return
        loading_placeholder.visible = False
        above = loading[loading["loading_pct"] >= threshold].copy()
        if above.empty:
            loading_caption.set_text(f"No elements loaded above {threshold}%.")
            loading_grid.options.update({"columnDefs": [], "rowData": []})
            loading_grid.update()
            return
        loading_caption.set_text(f"{len(above)} elements above {threshold}%")
        show = above[["element_id", "element_name", "element_type", "side",
                      "current", "permanent_limit", "loading_pct", "losses"]].copy()
        show.columns = ["Element", "Name", "Type", "Worst side",
                        "I (A)", "Permanent limit (A)", "Loading (%)",
                        "Losses (MW)"]
        show["Worst side"] = show["Worst side"].map(
            {"ONE": "Side 1", "TWO": "Side 2"})
        show["I (A)"] = show["I (A)"].round(1)
        show["Loading (%)"] = show["Loading (%)"].round(1)
        show["Losses (MW)"] = show["Losses (MW)"].round(3)
        # Color-code Loading (%) ≥ 80 (orange) / ≥ 100 (red) via an
        # ag-Grid cellClassRules expression.
        column_defs = []
        for col in show.columns:
            defn: dict = {"headerName": col, "field": col}
            if col == "Loading (%)":
                defn["cellClassRules"] = {
                    "bg-red-3 text-white": "x >= 100",
                    "bg-orange-3 text-white": "x >= 80 && x < 100",
                }
            column_defs.append(defn)
        loading_grid.options.update({
            "columnDefs": column_defs,
            "rowData": show.to_dict("records"),
            "defaultColDef": _DEFAULT_COL_DEF,
        })
        loading_grid.update()

    def _render_selected_element() -> None:
        vm = state["vm"]
        element_id = state["element_id"]
        if vm is None or element_id is None:
            losses_lbl.set_text("")
            element_plot.figure = go.Figure()
            element_plot.update()
            element_limits_grid.options.update({"columnDefs": [], "rowData": []})
            element_limits_grid.update()
            return
        elem_limits = vm.display_limits_df[
            vm.display_limits_df["element_id"] == element_id
        ]
        if elem_limits.empty:
            losses_lbl.set_text("")
            element_plot.figure = go.Figure()
            element_plot.update()
            element_limits_grid.options.update({"columnDefs": [], "rowData": []})
            element_limits_grid.update()
            return
        # Losses metric.
        loss = vm.losses.get(element_id)
        if loss is not None and pd.notna(loss):
            losses_lbl.set_text(f"Active-power losses: {loss:.3f} MW")
        else:
            losses_lbl.set_text(
                "Losses unavailable (run a load flow to compute p1 + p2)."
            )
        # Plotly chart.
        fig = build_element_chart(
            element_id, elem_limits, vm.flows.get(element_id),
        )
        element_plot.figure = fig
        element_plot.update()
        # Raw limits table.
        cols = [c for c in
                ("side", "name", "acceptable_duration", "value", "element_type")
                if c in elem_limits.columns]
        show = elem_limits[cols].sort_values(
            ["side", "acceptable_duration"]
            if "acceptable_duration" in cols else cols,
        )
        element_limits_grid.options.update({
            "columnDefs": [{"field": c, "headerName": c} for c in show.columns],
            "rowData": show.to_dict("records"),
            "defaultColDef": _DEFAULT_COL_DEF,
        })
        element_limits_grid.update()

    def _refresh_element_choices() -> None:
        vm = state["vm"]
        if vm is None:
            element_select.options = []
            element_select.update()
            element_count_lbl.set_text("")
            return
        candidates = list(vm.element_ids)
        id_filter = (state["id_filter"] or "").strip()
        if id_filter:
            f = id_filter.lower()
            candidates = [e for e in candidates if f in str(e).lower()]
        element_count_lbl.set_text(f"{len(candidates)} elements with limits")
        element_select.options = candidates
        current = state["element_id"]
        if current not in candidates:
            current = candidates[0] if candidates else None
        state["element_id"] = current
        element_select.value = current
        element_select.update()

    def _on_threshold_changed(_e=None) -> None:
        try:
            state["threshold"] = int(threshold_slider.value or 0)
        except (TypeError, ValueError):
            return
        _render_loading_table()

    threshold_slider.on("update:model-value", _on_threshold_changed)

    def _on_id_filter_changed(_e=None) -> None:
        state["id_filter"] = id_filter_input.value or ""
        _refresh_element_choices()
        _render_selected_element()

    id_filter_input.on_value_change(_on_id_filter_changed)

    def _on_element_changed(_e=None) -> None:
        state["element_id"] = element_select.value
        _render_selected_element()

    element_select.on("update:model-value", _on_element_changed)

    def refresh() -> None:
        if _state.network is None:
            state["vm"] = None
            state["element_id"] = None
            placeholder.set_text("Load a network to see operational limits.")
            _set_data_visible(False)
            return
        try:
            vm = build_operational_limits_view_model(_state.network)
        except Exception as exc:
            placeholder.set_text(f"Operational limits failed: {exc}")
            state["vm"] = None
            _set_data_visible(False)
            return
        if vm is None:
            placeholder.set_text("No operational limits found in this network.")
            state["vm"] = None
            _set_data_visible(False)
            return
        state["vm"] = vm
        _set_data_visible(True)
        _render_loading_table()
        _refresh_element_choices()
        _render_selected_element()

    refresh()
    return refresh


def _build_security_analysis():
    """Materialise the "Security Analysis" tab.

    Ports the Streamlit Security Analysis tab: the automatic
    N-1 / N-2 contingency builder, the advanced configuration
    (monitored elements, limit reductions, remedial actions, operator
    strategies), an AC run, and a results overview. JSON import stays
    Streamlit-only — file upload is host-specific.

    All pypowsybl work + the config builders / validators go through
    the shared :mod:`iidm_viewer.security_analysis` core, so this tab
    and the Streamlit / PySide6 ones stay in lockstep.

    Returns a closure the page-wide listeners call on network change.
    """
    import pandas as pd

    from iidm_viewer.security_analysis import (
        ACTION_FIELDS,
        AUTO_MODES,
        CONDITION_TYPES,
        CTX_TYPES,
        ELEMENT_TYPES,
        SecurityAnalysisViewModel,
        VIOLATION_TYPES,
        action_summary,
        build_n1_contingencies,
        build_n2_contingencies,
        get_element_ids,
        get_nominal_voltages,
        limit_reduction_summary,
        monitored_element_summary,
        operator_strategy_summary,
        run_security_analysis,
        summarize_security_results,
    )

    # Five configuration lists, results dict and element-ids cache all
    # live on the shared view-model so the add / remove / validate /
    # store flow matches the PySide6 + Streamlit tabs byte-for-byte.
    vm = SecurityAnalysisViewModel()

    placeholder = ui.label(
        "Load a network to run a security analysis."
    ).classes("text-caption q-pa-md")

    config_card = ui.card().classes("w-full q-pa-sm")
    with config_card:
        ui.label("Contingency configuration").classes("text-h6")
        with ui.row().classes("items-center w-full"):
            ui.label("Mode:")
            mode_select = ui.select(options=list(AUTO_MODES), value="N-1") \
                .props("dense outlined").classes("w-32")
            ui.label("Element type:")
            element_select = ui.select(
                options=list(ELEMENT_TYPES), value=ELEMENT_TYPES[0],
            ).props("dense outlined").classes("w-64")
        with ui.row().classes("items-center w-full"):
            ui.label("Nominal voltage filter (optional):")
            nominal_v_select = ui.select(
                options=[], value=[], multiple=True,
            ).props("dense outlined").classes("w-72")
        with ui.row().classes("items-center w-full"):
            build_btn = ui.button("Build contingency list")
            contingency_count_lbl = ui.label("").classes("text-caption q-ml-md")

    # --- Advanced configuration ----------------------------------------
    adv_expansion = ui.expansion(
        "Advanced configuration", icon="tune",
    ).classes("w-full")
    with adv_expansion:
        # Monitored elements.
        with ui.expansion("Monitored elements").classes("w-full"):
            with ui.row().classes("items-center w-full"):
                mon_ctx = ui.select(options=list(CTX_TYPES), value="ALL") \
                    .props("dense outlined").classes("w-40")
                mon_cids = ui.select(options=[], value=[], multiple=True) \
                    .props("dense outlined").classes("w-56")
            with ui.row().classes("items-center w-full"):
                mon_branches = ui.select(options=[], value=[], multiple=True) \
                    .props("dense outlined").classes("w-56")
                mon_vls = ui.select(options=[], value=[], multiple=True) \
                    .props("dense outlined").classes("w-56")
                mon_3wt = ui.select(options=[], value=[], multiple=True) \
                    .props("dense outlined").classes("w-56")
            ui.button("Add monitored rule", on_click=lambda: _add_monitored())
            mon_list = ui.column().classes("w-full")

        # Limit reductions.
        with ui.expansion("Limit reductions").classes("w-full"):
            with ui.row().classes("items-center w-full"):
                lr_value = ui.number(
                    "Value (0–1)", value=0.9, min=0.0, max=1.0, step=0.05,
                ).props("dense outlined").classes("w-40")
                lr_perm = ui.checkbox("Permanent", value=True)
                lr_temp = ui.checkbox("Temporary", value=True)
            ui.button("Add limit reduction", on_click=lambda: _add_reduction())
            lr_list = ui.column().classes("w-full")

        # Remedial actions.
        with ui.expansion("Remedial actions").classes("w-full"):
            with ui.row().classes("items-center w-full"):
                act_type = ui.select(
                    options=list(ACTION_FIELDS), value=list(ACTION_FIELDS)[0],
                ).props("dense outlined").classes("w-64")
                act_id = ui.input("Action ID") \
                    .props("dense outlined").classes("w-48")
            act_fields_row = ui.column().classes("w-full")
            ui.button("Add action", on_click=lambda: _add_action())
            act_list = ui.column().classes("w-full")

        # Operator strategies.
        with ui.expansion("Operator strategies").classes("w-full"):
            with ui.row().classes("items-center w-full"):
                strat_id = ui.input("Strategy ID") \
                    .props("dense outlined").classes("w-48")
                strat_cid = ui.select(options=[], value=None) \
                    .props("dense outlined").classes("w-56")
            with ui.row().classes("items-center w-full"):
                strat_actions = ui.select(options=[], value=[], multiple=True) \
                    .props("dense outlined").classes("w-56")
                strat_condition = ui.select(
                    options=list(CONDITION_TYPES), value=CONDITION_TYPES[0],
                ).props("dense outlined").classes("w-72")
                strat_vtypes = ui.select(
                    options=list(VIOLATION_TYPES), value=[], multiple=True,
                ).props("dense outlined").classes("w-56")
            ui.button("Add strategy", on_click=lambda: _add_strategy())
            strat_list = ui.column().classes("w-full")

    run_row = ui.row().classes("items-center w-full q-pa-sm")
    with run_row:
        run_btn = ui.button("Run security analysis")
        run_status = ui.label("").classes("text-caption q-ml-md")

    # --- Advanced-config field widgets (registry-driven actions) -------
    act_field_widgets: dict = {}

    def _rebuild_action_fields() -> None:
        act_fields_row.clear()
        act_field_widgets.clear()
        spec = ACTION_FIELDS.get(act_type.value)
        if spec is None:
            return
        ids = vm.element_ids.get(spec["id_key"]) or []
        with act_fields_row:
            with ui.row().classes("items-center w-full"):
                for fdef in spec["fields"]:
                    kind = fdef["kind"]
                    if kind == "id":
                        w = ui.select(
                            options=list(ids),
                            value=(ids[0] if ids else None),
                        ).props("dense outlined").classes("w-56")
                    elif kind == "bool":
                        w = ui.checkbox(
                            fdef["label"], value=fdef.get("default", False),
                        )
                    elif kind == "choice":
                        w = ui.select(
                            options=list(fdef["options"]),
                            value=fdef.get("default"),
                        ).props("dense outlined").classes("w-40")
                    elif kind == "int":
                        w = ui.number(
                            fdef["label"], value=fdef.get("default", 0),
                            step=1, format="%d",
                        ).props("dense outlined").classes("w-40")
                    else:  # float
                        w = ui.number(
                            fdef["label"], value=fdef.get("default", 0.0),
                            step=10.0,
                        ).props("dense outlined").classes("w-40")
                    act_field_widgets[fdef["name"]] = (fdef, w)

    act_type.on("update:model-value", lambda _e=None: _rebuild_action_fields())

    def _render_entry_list(container, entries, summary_fn, remover) -> None:
        container.clear()
        with container:
            for i, entry in enumerate(entries):
                with ui.row().classes("items-center w-full"):
                    ui.label(summary_fn(entry)).classes("text-caption col")
                    ui.button(
                        icon="delete",
                        on_click=lambda _e=None, idx=i: remover(idx),
                    ).props("flat dense")

    def _re_render_monitored() -> None:
        _render_entry_list(mon_list, vm.monitored,
                           monitored_element_summary, _remove_monitored)

    def _re_render_reductions() -> None:
        _render_entry_list(lr_list, vm.reductions,
                           limit_reduction_summary, _remove_reduction)

    def _re_render_strategies() -> None:
        _render_entry_list(strat_list, vm.strategies,
                           operator_strategy_summary, _remove_strategy)

    def _add_monitored() -> None:
        errors = vm.add_monitored(
            mon_ctx.value,
            contingency_ids=mon_cids.value,
            branch_ids=mon_branches.value,
            voltage_level_ids=mon_vls.value,
            three_windings_transformer_ids=mon_3wt.value,
        )
        if errors:
            ui.notify("; ".join(errors), type="warning")
            return
        _re_render_monitored()

    def _remove_monitored(i):
        vm.remove_monitored(i)
        _re_render_monitored()

    def _add_reduction() -> None:
        errors = vm.add_reduction(
            float(lr_value.value or 0),
            permanent=lr_perm.value,
            temporary=lr_temp.value,
        )
        if errors:
            ui.notify("; ".join(errors), type="warning")
            return
        _re_render_reductions()

    def _remove_reduction(i):
        vm.remove_reduction(i)
        _re_render_reductions()

    def _add_action() -> None:
        fields = {}
        for name, (fdef, w) in act_field_widgets.items():
            val = w.value
            if fdef["kind"] == "int":
                val = int(val or 0)
            elif fdef["kind"] == "float":
                val = float(val or 0.0)
            elif fdef["kind"] == "bool":
                val = bool(val)
            fields[name] = val
        errors = vm.add_action(act_type.value, act_id.value, fields)
        if errors:
            ui.notify("; ".join(errors), type="warning")
            return
        act_id.value = ""
        act_id.update()
        _refresh_action_dependents()

    def _remove_action(i):
        # Cascade: drop the removed action id from every strategy so
        # the run helper doesn't see a dangling reference. The
        # view-model exposes the lists by reference, so this mutates
        # them in place.
        if 0 <= i < len(vm.actions):
            removed_id = vm.actions[i].get("action_id")
            vm.remove_action(i)
            if removed_id:
                for s in vm.strategies:
                    s["action_ids"] = [
                        a for a in s["action_ids"] if a != removed_id
                    ]
        _refresh_action_dependents()

    def _add_strategy() -> None:
        errors = vm.add_strategy(
            strat_id.value,
            strat_cid.value,
            strat_actions.value,
            condition_type=strat_condition.value,
            violation_subject_ids=[],
            violation_types=strat_vtypes.value,
        )
        if not strat_cid.value:
            errors = list(errors) + ["Pick a triggering contingency."]
        if errors:
            ui.notify("; ".join(errors), type="warning")
            return
        strat_id.value = ""
        strat_id.update()
        _re_render_strategies()

    def _remove_strategy(i):
        vm.remove_strategy(i)
        _re_render_strategies()

    def _refresh_action_dependents() -> None:
        """Re-render the action list + the strategy action picker."""
        _render_entry_list(act_list, vm.actions,
                           action_summary, _remove_action)
        action_ids = vm.action_ids()
        strat_actions.options = action_ids
        strat_actions.value = [
            a for a in (strat_actions.value or []) if a in action_ids
        ]
        strat_actions.update()

    results_card = ui.card().classes("w-full q-pa-sm")
    with results_card:
        ui.label("Results").classes("text-h6")
        pre_status_lbl = ui.label("").classes("text-subtitle2")
        summary_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 240px")
        ui.label("Per-contingency limit violations:") \
            .classes("text-caption q-mt-sm")
        violations_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 260px")
    results_card.visible = False

    def _render_results() -> None:
        results = vm.results
        if results is None:
            results_card.visible = False
            return
        results_card.visible = True
        pre = results.get("pre_status", "?")
        pre_status_lbl.set_text(f"Pre-contingency load flow: {pre}")
        summary = vm.results_summary()
        summary_grid.options.update({
            "columnDefs": [{"field": c, "headerName": c}
                           for c in summary.columns],
            "rowData": summary.to_dict("records"),
            "defaultColDef": _DEFAULT_COL_DEF,
        })
        summary_grid.update()
        # Concatenate every post-contingency violation frame, tagged
        # with its contingency id.
        frames = []
        for cid, cr in (results.get("post") or {}).items():
            viol = cr.get("limit_violations")
            if viol is not None and not viol.empty:
                tagged = viol.copy()
                tagged.insert(0, "contingency_id", cid)
                frames.append(tagged)
        if frames:
            all_viol = pd.concat(frames, ignore_index=True)
            violations_grid.options.update({
                "columnDefs": [{"field": str(c), "headerName": str(c)}
                               for c in all_viol.columns],
                "rowData": all_viol.fillna("").astype(str).to_dict("records"),
                "defaultColDef": _DEFAULT_COL_DEF,
            })
        else:
            violations_grid.options.update({"columnDefs": [], "rowData": []})
        violations_grid.update()

    def _sync_contingency_dependents() -> None:
        """Feed the built contingency ids into the monitored + strategy
        pickers."""
        cids = vm.contingency_ids()
        mon_cids.options = cids
        mon_cids.value = [c for c in (mon_cids.value or []) if c in cids]
        mon_cids.update()
        strat_cid.options = cids
        if strat_cid.value not in cids:
            strat_cid.value = cids[0] if cids else None
        strat_cid.update()

    async def _on_build() -> None:
        if _state.network is None:
            return
        mode = mode_select.value
        element_type = element_select.value
        selected_v = nominal_v_select.value or []
        nominal_v_set = {float(v) for v in selected_v} if selected_v else None
        contingency_count_lbl.set_text("Building contingencies…")
        builder = (
            build_n1_contingencies if mode == "N-1" else build_n2_contingencies
        )
        try:
            contingencies = await asyncio.to_thread(
                builder, _state.network, element_type, nominal_v_set,
            )
        except Exception as exc:
            contingency_count_lbl.set_text(f"Build failed: {exc}")
            return
        vm.set_contingencies(contingencies)
        n = len(vm.contingencies)
        contingency_count_lbl.set_text(
            f"{n} contingenc{'y' if n == 1 else 'ies'} ready."
        )
        run_btn.set_enabled(n > 0)
        _sync_contingency_dependents()

    build_btn.on_click(_on_build)

    async def _on_run() -> None:
        if _state.network is None:
            return
        if not vm.contingencies:
            run_status.set_text("Build a contingency list first.")
            return
        n = len(vm.contingencies)
        run_status.set_text(
            f"Running AC security analysis on {n} "
            f"contingenc{'y' if n == 1 else 'ies'}…"
        )
        try:
            results = await asyncio.to_thread(
                lambda: run_security_analysis(
                    _state.network,
                    vm.contingencies,
                    monitored_elements=vm.monitored,
                    limit_reductions=vm.reductions,
                    actions=vm.actions,
                    operator_strategies=vm.strategies,
                ),
            )
        except Exception as exc:
            run_status.set_text(f"Security analysis failed: {exc}")
            return
        vm.store_results(results)
        run_status.set_text(
            f"Done — {n} contingenc{'y' if n == 1 else 'ies'} analysed."
        )
        _render_results()

    run_btn.on_click(_on_run)

    def refresh() -> None:
        if _state.network is None:
            placeholder.set_text("Load a network to run a security analysis.")
            placeholder.visible = True
            config_card.visible = False
            adv_expansion.visible = False
            run_row.visible = False
            results_card.visible = False
            vm.clear()
            return
        placeholder.visible = False
        config_card.visible = True
        adv_expansion.visible = True
        run_row.visible = True
        # Repopulate the nominal-voltage filter.
        try:
            voltages = get_nominal_voltages(_state.network)
        except Exception:
            voltages = []
        nominal_v_select.options = [str(v) for v in voltages]
        nominal_v_select.value = []
        nominal_v_select.update()
        # Fetch element-id buckets for the advanced-config selectors.
        try:
            vm.set_element_ids(get_element_ids(_state.network))
        except Exception:
            vm.set_element_ids({})
        mon_branches.options = list(vm.element_ids.get("branches", []))
        mon_vls.options = list(vm.element_ids.get("voltage_levels", []))
        mon_3wt.options = list(vm.element_ids.get("three_windings_transformers", []))
        for w in (mon_branches, mon_vls, mon_3wt):
            w.value = []
            w.update()
        # Reset the per-network configuration lists + results (the
        # element-ids cache was just refilled above, so we use
        # ``clear`` + re-set rather than a clear after).
        vm.contingencies.clear()
        vm.monitored.clear()
        vm.reductions.clear()
        vm.actions.clear()
        vm.strategies.clear()
        vm.clear_results()
        contingency_count_lbl.set_text("")
        run_btn.set_enabled(False)
        run_status.set_text("")
        _render_results()
        _rebuild_action_fields()
        _sync_contingency_dependents()
        for container, summary_fn, remover in (
            (mon_list, monitored_element_summary, _remove_monitored),
            (lr_list, limit_reduction_summary, _remove_reduction),
            (strat_list, operator_strategy_summary, _remove_strategy),
        ):
            _render_entry_list(container, [], summary_fn, remover)
        _refresh_action_dependents()

    refresh()
    return refresh


def _build_short_circuit_analysis():
    """Materialise the "Short Circuit Analysis" tab.

    Ports the Streamlit Short Circuit Analysis tab: bus-fault list
    (filtered by nominal voltage), analysis parameters form, an AC
    run, and a results view (per-fault summary + drill-down with
    feeder contributions and limit violations).

    All pypowsybl work goes through the shared
    :mod:`iidm_viewer.short_circuit_analysis` core so the Streamlit,
    PySide6 and NiceGUI hosts stay in lockstep.

    Returns a closure the page-wide listeners call on network change.
    """
    import pandas as pd

    from iidm_viewer.short_circuit_analysis import (
        FAULT_TYPES,
        STUDY_TYPES,
        ShortCircuitViewModel,
        build_bus_faults,
        default_hv_preselect,
        format_fault_type,
        get_nominal_voltages,
        make_sc_params,
        run_short_circuit_analysis,
    )

    # Faults list, results dict and derived summary / fault_options
    # all live on the shared view-model so PySide6 + Streamlit see the
    # same state machine.
    vm = ShortCircuitViewModel()

    placeholder = ui.label(
        "Load a network to run a short circuit analysis."
    ).classes("text-caption q-pa-md")

    config_card = ui.card().classes("w-full q-pa-sm")
    with config_card:
        ui.label("Fault configuration").classes("text-h6")
        with ui.row().classes("items-center w-full"):
            ui.label("Fault type:")
            fault_type_select = ui.select(
                options={ft: format_fault_type(ft) for ft in FAULT_TYPES},
                value=FAULT_TYPES[0],
            ).props("dense outlined").classes("w-64")
        with ui.row().classes("items-center w-full"):
            ui.label("Nominal voltage filter (kV, leave empty for all):")
            nominal_v_select = ui.select(
                options=[], value=[], multiple=True,
            ).props("dense outlined").classes("w-72")
        with ui.row().classes("items-center w-full"):
            build_btn = ui.button("Build fault list")
            fault_count_lbl = ui.label("").classes("text-caption q-ml-md")

    params_card = ui.card().classes("w-full q-pa-sm")
    with params_card:
        ui.label("Analysis parameters").classes("text-h6")
        with ui.row().classes("items-center w-full"):
            study_select = ui.select(
                options=list(STUDY_TYPES), value=STUDY_TYPES[0],
            ).props("dense outlined").classes("w-48") \
             .tooltip(
                "SUB_TRANSIENT uses subtransient reactances (default); "
                "TRANSIENT uses transient reactances."
            )
            feeder_chk = ui.checkbox(
                "Compute feeder contributions", value=True,
            ).tooltip("Break down fault current by contributing feeder.")
            violations_chk = ui.checkbox(
                "Check limit violations", value=True,
            ).tooltip("Detect currents exceeding operational limits.")
        with ui.row().classes("items-center w-full"):
            min_drop_input = ui.number(
                "Min voltage drop (%)", value=0.0,
                min=0.0, max=100.0, step=1.0,
            ).props("dense outlined").classes("w-40") \
             .tooltip(
                "Only report buses with a voltage drop above this threshold."
            )

    run_row = ui.row().classes("items-center w-full q-pa-sm")
    with run_row:
        run_btn = ui.button("Run short circuit analysis")
        run_status = ui.label("").classes("text-caption q-ml-md")

    results_card = ui.card().classes("w-full q-pa-sm")
    with results_card:
        ui.label("Results").classes("text-h6")
        with ui.row().classes("items-center w-full"):
            metric_simulated = ui.label("Faults simulated: 0") \
                .classes("text-subtitle2 q-mr-md")
            metric_failed = ui.label("Failed: 0") \
                .classes("text-subtitle2 q-mr-md")
            metric_violations = ui.label("With violations: 0") \
                .classes("text-subtitle2")
        slider_row = ui.row().classes("items-center w-full")
        with slider_row:
            ui.label("Show faults with fault power ≥")
            pwr_slider = ui.slider(
                min=0, max=1, value=0, step=1,
            ).props("label-always").classes("flex-grow")
            ui.label("MVA")
        slider_row.visible = False
        summary_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 240px")

        ui.label("Fault detail").classes("text-subtitle1 q-mt-md")
        with ui.row().classes("items-center w-full"):
            fault_filter_input = ui.input(
                placeholder="Filter by fault ID (substring, case-insensitive)",
            ).props("dense outlined clearable").classes("w-96")
            fault_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
        detail_status_lbl = ui.label("").classes("text-subtitle2")
        with ui.row().classes("items-center w-full"):
            detail_power_lbl = ui.label("Fault power: —") \
                .classes("q-mr-md")
            detail_current_lbl = ui.label("Fault current: —")
        ui.label("Feeder contributions:").classes("text-caption q-mt-sm")
        feeder_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 180px")
        ui.label("Limit violations:").classes("text-caption q-mt-sm")
        violations_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 220px")
    results_card.visible = False

    def _set_grid_from_df(grid, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            grid.options.update({
                "columnDefs": [], "rowData": [],
                "defaultColDef": _DEFAULT_COL_DEF,
            })
        else:
            grid.options.update({
                "columnDefs": [
                    {"field": str(c), "headerName": str(c)} for c in df.columns
                ],
                "rowData": df.fillna("").astype(str).to_dict("records"),
                "defaultColDef": _DEFAULT_COL_DEF,
            })
        grid.update()

    def _apply_pwr_filter() -> None:
        df = vm.summary_df()
        if df.empty:
            _set_grid_from_df(summary_grid, df)
            return
        threshold = float(pwr_slider.value or 0)
        if threshold <= 0:
            _set_grid_from_df(summary_grid, df)
            return
        mask = df["Fault power (MVA)"].isna() | (
            df["Fault power (MVA)"] >= threshold
        )
        _set_grid_from_df(summary_grid, df[mask].reset_index(drop=True))

    pwr_slider.on(
        "update:model-value", lambda _e=None: _apply_pwr_filter(),
    )

    def _render_fault_detail(fid):
        if not fid or not vm.has_results():
            detail_status_lbl.set_text("")
            detail_power_lbl.set_text("Fault power: —")
            detail_current_lbl.set_text("Fault current: —")
            _set_grid_from_df(feeder_grid, pd.DataFrame())
            _set_grid_from_df(violations_grid, pd.DataFrame())
            return
        fr = vm.results.get("fault_results", {}).get(fid, {})
        status = fr.get("status", "UNKNOWN")
        detail_status_lbl.set_text(f"Status: {status}")
        pwr = fr.get("short_circuit_power_mva")
        cur = fr.get("current_kA")
        detail_power_lbl.set_text(
            f"Fault power: {pwr:.1f} MVA" if pwr is not None
            else "Fault power: —"
        )
        detail_current_lbl.set_text(
            f"Fault current: {cur:.3f} kA" if cur is not None
            else "Fault current: —"
        )
        _set_grid_from_df(feeder_grid, fr.get("feeder_results", pd.DataFrame()))
        _set_grid_from_df(violations_grid, fr.get("limit_violations", pd.DataFrame()))

    def _refresh_fault_options() -> None:
        # Master list comes from the view-model's summary (only the
        # faults the runner actually analysed).
        master = vm.fault_options()
        sub = (fault_filter_input.value or "").strip().lower()
        if sub:
            opts = [fid for fid in master if sub in fid.lower()]
        else:
            opts = list(master)
        fault_select.options = opts
        if opts:
            new_value = (
                fault_select.value if fault_select.value in opts else opts[0]
            )
            fault_select.value = new_value
        else:
            fault_select.value = None
        fault_select.update()
        _render_fault_detail(fault_select.value)

    fault_filter_input.on(
        "update:model-value", lambda _e=None: _refresh_fault_options(),
    )
    fault_select.on(
        "update:model-value", lambda _e=None: _render_fault_detail(fault_select.value),
    )

    def _render_results() -> None:
        if not vm.has_results():
            results_card.visible = False
            return
        results_card.visible = True
        faults = vm.results.get("faults", [])
        metric_simulated.set_text(f"Faults simulated: {len(faults)}")
        metric_failed.set_text(f"Failed: {vm.failure_count()}")
        metric_violations.set_text(
            f"With violations: {vm.with_violations_count()}"
        )
        max_pwr = int(round(vm.max_fault_power_mva()))
        slider_row.visible = max_pwr > 0
        pwr_slider.max = max(max_pwr, 1)
        pwr_slider.value = 0
        pwr_slider.update()
        _apply_pwr_filter()
        _refresh_fault_options()

    async def _on_build() -> None:
        if _state.network is None:
            return
        fault_type = fault_type_select.value or FAULT_TYPES[0]
        chosen = nominal_v_select.value or []
        nominal_v_set = {float(v) for v in chosen} if chosen else None
        fault_count_lbl.set_text("Building fault list…")
        try:
            faults = await asyncio.to_thread(
                build_bus_faults, _state.network, nominal_v_set, fault_type,
            )
        except Exception as exc:
            fault_count_lbl.set_text(f"Build failed: {exc}")
            return
        vm.set_faults(faults)
        n = len(vm.faults)
        fault_count_lbl.set_text(
            f"{n} bus fault{'' if n == 1 else 's'} ready."
        )
        run_btn.set_enabled(n > 0)

    build_btn.on_click(_on_build)

    async def _on_run() -> None:
        if _state.network is None or not vm.faults:
            run_status.set_text("Build a fault list first.")
            return
        sc_params = make_sc_params(
            study_type=study_select.value or STUDY_TYPES[0],
            with_feeder_result=feeder_chk.value,
            with_limit_violations=violations_chk.value,
            min_voltage_drop_percent=float(min_drop_input.value or 0),
        )
        n = len(vm.faults)
        run_status.set_text(
            f"Running short circuit analysis on {n} fault"
            f"{'' if n == 1 else 's'}…"
        )
        try:
            results = await asyncio.to_thread(
                run_short_circuit_analysis,
                _state.network, vm.faults, sc_params,
            )
        except Exception as exc:
            run_status.set_text(f"Short circuit analysis failed: {exc}")
            return
        vm.store_results(results)
        run_status.set_text(
            f"Done — {n} fault{'' if n == 1 else 's'} analysed."
        )
        _render_results()

    run_btn.on_click(_on_run)

    def refresh() -> None:
        if _state.network is None:
            placeholder.visible = True
            config_card.visible = False
            params_card.visible = False
            run_row.visible = False
            results_card.visible = False
            vm.clear()
            return
        placeholder.visible = False
        config_card.visible = True
        params_card.visible = True
        run_row.visible = True
        try:
            voltages = get_nominal_voltages(_state.network)
        except Exception:
            voltages = []
        preselect = default_hv_preselect(voltages)
        nominal_v_select.options = [str(v) for v in voltages]
        nominal_v_select.value = [str(v) for v in preselect]
        nominal_v_select.update()
        fault_count_lbl.set_text("")
        run_status.set_text("")
        run_btn.set_enabled(False)
        vm.clear()
        _render_results()

    refresh()
    return refresh


def _build_overview():
    """Materialise the "Overview" tab.

    Composes the shared :mod:`iidm_viewer.network_info_core` core with
    NiceGUI widgets:

    * a four-metric header (ID / Name / Format / Case Date),
    * a "Generation and Consumption by Country" aggrid with a
      "actual values populate after a load flow" caption when the
      ``*_actual_mw`` columns are still NaN,
    * a "Network Losses" metric trio (total / lines / transformers)
      plus an optional per-country aggrid,
    * a collapsible "Component Statistics" grid of metric labels.

    Returns a closure the page-wide listeners call on network /
    load-flow changes.
    """
    import pandas as pd

    from iidm_viewer.network_info_core import (
        COUNTRY_TOTALS_DISPLAY_COLUMNS,
        LOSSES_BY_COUNTRY_COLUMNS,
        OverviewMetadata,
        build_country_totals_display,
        build_losses_by_country_display,
        compute_overview_data,
        country_totals_has_lf,
    )

    state: dict = {
        "metadata": OverviewMetadata("", "", "", ""),
        "country_totals": pd.DataFrame(),
        "losses": {"total": 0.0, "lines": 0.0, "transformers": 0.0,
                   "has_data": False},
        "losses_by_country": pd.Series(dtype=float),
        "component_counts": {},
    }

    placeholder = ui.label(
        "Load a network to see the overview.",
    ).classes("text-caption q-pa-md")

    # ------------------------------------------------------------------
    # Metadata header
    # ------------------------------------------------------------------
    meta_row = ui.row().classes("items-stretch q-gutter-sm no-wrap w-full")
    with meta_row:
        meta_id_lbl = ui.html("").classes("col text-center q-pa-sm") \
            .style("border: 1px solid #ddd; border-radius: 4px;")
        meta_name_lbl = ui.html("").classes("col text-center q-pa-sm") \
            .style("border: 1px solid #ddd; border-radius: 4px;")
        meta_format_lbl = ui.html("").classes("col text-center q-pa-sm") \
            .style("border: 1px solid #ddd; border-radius: 4px;")
        meta_case_date_lbl = ui.html("").classes("col text-center q-pa-sm") \
            .style("border: 1px solid #ddd; border-radius: 4px;")
    meta_row.visible = False

    # ------------------------------------------------------------------
    # Country totals
    # ------------------------------------------------------------------
    country_card = ui.card().classes("w-full q-pa-sm")
    with country_card:
        ui.label("Generation and Consumption by Country").classes("text-h6")
        country_empty_lbl = ui.label(
            "No generation or consumption data available.",
        ).classes("text-caption text-grey-7")
        country_empty_lbl.visible = False
        country_lf_caption = ui.label(
            "Actual values populate once a load flow has run.",
        ).classes("text-caption q-mt-xs")
        country_lf_caption.visible = False
        country_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 260px")
    country_card.visible = False

    # ------------------------------------------------------------------
    # Losses
    # ------------------------------------------------------------------
    losses_card = ui.card().classes("w-full q-pa-sm")
    with losses_card:
        ui.label("Network Losses").classes("text-h6")
        losses_empty_lbl = ui.label(
            "No loss data available (run a load flow first).",
        ).classes("text-caption text-grey-7")
        losses_empty_lbl.visible = False
        losses_metrics_row = ui.row() \
            .classes("items-stretch q-gutter-sm no-wrap w-full")
        with losses_metrics_row:
            losses_total_lbl = ui.html("").classes("col text-center q-pa-sm") \
                .style("border: 1px solid #ddd; border-radius: 4px;")
            losses_lines_lbl = ui.html("").classes("col text-center q-pa-sm") \
                .style("border: 1px solid #ddd; border-radius: 4px;")
            losses_xfmr_lbl = ui.html("").classes("col text-center q-pa-sm") \
                .style("border: 1px solid #ddd; border-radius: 4px;")
        losses_by_country_caption = ui.label(
            "Losses by country — cross-border branches split 50/50.",
        ).classes("text-caption q-mt-xs")
        losses_by_country_caption.visible = False
        losses_by_country_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 200px")
        losses_by_country_grid.visible = False
    losses_card.visible = False

    # ------------------------------------------------------------------
    # Component statistics (collapsible)
    # ------------------------------------------------------------------
    counts_card = ui.card().classes("w-full q-pa-sm")
    with counts_card:
        ui.label("Component Statistics").classes("text-h6")
        counts_expansion = ui.expansion(
            "Show component counts", value=False,
        ).classes("w-full")
        with counts_expansion:
            counts_grid_container = ui.grid(columns=4).classes("w-full q-gutter-sm")
        counts_empty_lbl = ui.label(
            "No components found in this network.",
        ).classes("text-caption text-grey-7")
        counts_empty_lbl.visible = False
    counts_card.visible = False

    def _set_grid_from_df(grid, df, columns=None) -> None:
        cols = list(columns) if columns is not None else list(df.columns)
        if df.empty:
            grid.options.update({
                "columnDefs": [{"field": c, "headerName": c} for c in cols],
                "rowData": [],
                "defaultColDef": _DEFAULT_COL_DEF,
            })
        else:
            grid.options.update({
                "columnDefs": [
                    {"field": c, "headerName": c} for c in df.columns
                ],
                "rowData": df.fillna("").astype(str).to_dict("records"),
                "defaultColDef": _DEFAULT_COL_DEF,
            })
        grid.update()

    def _render_metadata(metadata) -> None:
        meta_id_lbl.set_content(
            f"<b>Network ID</b><br>{metadata.network_id or '—'}",
        )
        meta_name_lbl.set_content(
            f"<b>Name</b><br>{metadata.name or '—'}",
        )
        meta_format_lbl.set_content(
            f"<b>Format</b><br>{metadata.source_format or '—'}",
        )
        meta_case_date_lbl.set_content(
            f"<b>Case Date</b><br>{metadata.case_date or '—'}",
        )

    def _render_country_totals(df) -> None:
        if df.empty:
            country_empty_lbl.visible = True
            country_lf_caption.visible = False
            _set_grid_from_df(
                country_grid,
                pd.DataFrame(columns=COUNTRY_TOTALS_DISPLAY_COLUMNS),
                columns=COUNTRY_TOTALS_DISPLAY_COLUMNS,
            )
            country_grid.style("height: 0px")
            return
        country_empty_lbl.visible = False
        country_lf_caption.visible = not country_totals_has_lf(df)
        _set_grid_from_df(country_grid, build_country_totals_display(df))
        country_grid.style("height: 260px")

    def _render_losses(losses, by_country) -> None:
        has_data = bool(losses.get("has_data"))
        if not has_data:
            losses_empty_lbl.visible = True
            losses_metrics_row.visible = False
            losses_by_country_caption.visible = False
            losses_by_country_grid.visible = False
            return
        losses_empty_lbl.visible = False
        losses_metrics_row.visible = True
        losses_total_lbl.set_content(
            f"<b>Total losses</b><br>{losses['total']:.2f} MW",
        )
        losses_lines_lbl.set_content(
            f"<b>Line losses</b><br>{losses['lines']:.2f} MW",
        )
        losses_xfmr_lbl.set_content(
            f"<b>Transformer losses</b><br>{losses['transformers']:.2f} MW",
        )
        if by_country.empty:
            losses_by_country_caption.visible = False
            losses_by_country_grid.visible = False
            return
        losses_by_country_caption.visible = True
        losses_by_country_grid.visible = True
        _set_grid_from_df(
            losses_by_country_grid,
            build_losses_by_country_display(by_country),
        )

    def _render_component_counts(counts) -> None:
        # Rebuild the grid each refresh — counts change per load.
        counts_grid_container.clear()
        if not counts:
            counts_empty_lbl.visible = True
            return
        counts_empty_lbl.visible = False
        with counts_grid_container:
            for label, count in counts.items():
                ui.html(
                    f"<b>{label}</b><br>{count}",
                ).classes("text-center q-pa-sm") \
                 .style("border: 1px solid #ddd; border-radius: 4px;")

    async def refresh() -> None:
        if _state.network is None:
            placeholder.set_text("Load a network to see the overview.")
            placeholder.visible = True
            meta_row.visible = False
            country_card.visible = False
            losses_card.visible = False
            counts_card.visible = False
            return
        try:
            data = await asyncio.to_thread(
                compute_overview_data, _state.network,
            )
        except Exception as exc:
            placeholder.set_text(f"Overview failed: {exc}")
            placeholder.visible = True
            meta_row.visible = False
            country_card.visible = False
            losses_card.visible = False
            counts_card.visible = False
            return
        state["metadata"] = data.metadata
        state["country_totals"] = data.country_totals
        state["losses"] = data.losses
        state["losses_by_country"] = data.losses_by_country
        state["component_counts"] = data.component_counts

        placeholder.visible = False
        meta_row.visible = True
        country_card.visible = True
        losses_card.visible = True
        counts_card.visible = True

        _render_metadata(data.metadata)
        _render_country_totals(data.country_totals)
        _render_losses(data.losses, data.losses_by_country)
        _render_component_counts(data.component_counts)

    return refresh


def _build_pmax_visualization():
    """Materialise the "Pmax Visualization" tab.

    Composes the shared :mod:`iidm_viewer.pmax_visualization` core
    (compute + chart + filter + classifier) with NiceGUI widgets:

    * a "Only lines connected to VL X" checkbox when an upstream VL
      is selected,
    * a summary aggrid colour-coded via the shared classifiers
      (``ratio_color`` / ``margin_color``),
    * a line picker + a four-metric row,
    * a ``ui.plotly`` chart with the P-δ characteristic.

    Returns a closure the page-wide listeners call on network /
    selected-VL / load-flow changes.
    """
    import pandas as pd
    import plotly.graph_objects as go

    from iidm_viewer.pmax_visualization import (
        DISPLAY_COLUMNS,
        PmaxViewModel,
        build_pangle_chart,
        compute_pmax_data,
        margin_color,
        ratio_color,
    )

    # The unfiltered DataFrame + VL-filter toggle state live on the
    # shared view-model so PySide6 + Streamlit consume the same
    # rows_df / display_df / line_ids surface.
    vm = PmaxViewModel()

    ui.label(
        "For each line: Pmax = V₁ × V₂ / X  (V in kV, X in Ω, "
        "result in MW). The ratio P/Pmax = sin(δ) shows proximity "
        "to the steady-state stability limit — the operating point "
        "reaches the limit when δ = 90°."
    ).classes("text-caption q-pa-sm")

    placeholder = ui.label(
        "Load a network and run a load flow to see Pmax visualization."
    ).classes("text-caption q-pa-md")

    only_vl_row = ui.row().classes("items-center q-pa-sm w-full")
    with only_vl_row:
        only_vl_checkbox = ui.checkbox(
            "Only lines connected to selected VL", value=False,
        )
    only_vl_row.visible = False

    summary_card = ui.card().classes("w-full q-pa-sm")
    with summary_card:
        ui.label("Lines sorted by proximity to stability limit") \
            .classes("text-h6")
        summary_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 280px")
    summary_card.visible = False

    detail_card = ui.card().classes("w-full q-pa-sm")
    with detail_card:
        ui.label("Power-angle characteristic").classes("text-h6")
        with ui.row().classes("items-center w-full"):
            ui.label("Line:")
            line_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-64")
        metrics_row = ui.row().classes("items-stretch q-pa-sm w-full no-wrap")
        with metrics_row:
            pmax_lbl = ui.label("Pmax: —").classes("col")
            pactual_lbl = ui.label("P: —").classes("col")
            ratio_lbl = ui.label("P/Pmax: —").classes("col")
            delta_lbl = ui.label("δ: —").classes("col")
        plot = ui.plotly(go.Figure()).classes("w-full").style("height: 460px")
    detail_card.visible = False

    def _set_summary_grid(df: pd.DataFrame) -> None:
        if df.empty:
            summary_grid.options.update({
                "columnDefs": [], "rowData": [],
                "defaultColDef": _DEFAULT_COL_DEF,
            })
            summary_grid.update()
            return
        # Per-cell colour for P/Pmax + Margin via ag-grid cellStyle JS.
        col_defs = []
        for col in df.columns:
            entry = {"field": col, "headerName": col}
            if col == "P/Pmax":
                entry["cellStyle"] = (
                    "function(params){"
                    "var v=params.value;"
                    "if(v==null||v===''){return null;}"
                    "var n=Number(v);"
                    "if(isNaN(n)){return null;}"
                    "if(n>=0.8){return {'backgroundColor':'#ff4b4b','color':'white'};}"
                    "if(n>=0.6){return {'backgroundColor':'#ffa500'};}"
                    "return null;}"
                )
            elif col == "Margin (%)":
                entry["cellStyle"] = (
                    "function(params){"
                    "var v=params.value;"
                    "if(v==null||v===''){return null;}"
                    "var n=Number(v);"
                    "if(isNaN(n)){return null;}"
                    "if(n<=20){return {'backgroundColor':'#ff4b4b','color':'white'};}"
                    "if(n<=40){return {'backgroundColor':'#ffa500'};}"
                    "return null;}"
                )
            col_defs.append(entry)
        summary_grid.options.update({
            "columnDefs": col_defs,
            "rowData": df.fillna("").astype(str).to_dict("records"),
            "defaultColDef": _DEFAULT_COL_DEF,
        })
        summary_grid.update()

    def _render_detail(line_id) -> None:
        rows_df = vm.rows_df()
        if not line_id or rows_df.empty or line_id not in rows_df.index:
            pmax_lbl.set_text("Pmax: —")
            pactual_lbl.set_text("P: —")
            ratio_lbl.set_text("P/Pmax: —")
            delta_lbl.set_text("δ: —")
            plot.update_figure(go.Figure())
            return
        row = rows_df.loc[line_id]
        pmax_lbl.set_text(f"Pmax: {row['pmax_mw']:.1f} MW")
        pactual_lbl.set_text(f"P: {row['p_actual_mw']:.1f} MW")
        ratio_val = row["p_pmax_ratio"]
        margin_val = row["margin_pct"]
        if pd.notna(ratio_val):
            text = f"P/Pmax: {ratio_val:.1%}"
            if pd.notna(margin_val):
                text += f"  (margin {margin_val:.1f} %)"
            ratio_lbl.set_text(text)
        else:
            ratio_lbl.set_text("P/Pmax: N/A")
        delta = row["delta_deg"]
        delta_lbl.set_text(
            f"δ: {delta:.1f}°" if pd.notna(delta) else "δ: N/A",
        )
        plot.update_figure(build_pangle_chart(line_id, row))

    def _refresh_line_select() -> None:
        line_ids = vm.line_ids()
        line_select.options = line_ids
        if line_select.value not in line_ids:
            line_select.value = line_ids[0] if line_ids else None
        line_select.update()
        _render_detail(line_select.value)

    line_select.on(
        "update:model-value",
        lambda _e=None: _render_detail(line_select.value),
    )

    def _apply_vl_filter() -> None:
        """Read the current ``_state.selected_vl`` + the checkbox value
        and recompute the display frame."""
        vm.set_selected_vl(_state.selected_vl)
        vm.set_only_vl(bool(only_vl_checkbox.value))
        if vm.is_empty():
            summary_card.visible = False
            detail_card.visible = False
            placeholder.visible = True
            return
        rows_df = vm.rows_df()
        if rows_df.empty:
            placeholder.set_text("No lines match the current filter.")
            placeholder.visible = True
            summary_card.visible = False
            detail_card.visible = False
            return
        placeholder.visible = False
        summary_card.visible = True
        detail_card.visible = True
        _set_summary_grid(vm.display_df())
        _refresh_line_select()

    only_vl_checkbox.on(
        "update:model-value", lambda _e=None: _apply_vl_filter(),
    )

    def _update_vl_visibility() -> None:
        """Show / hide the 'Only lines connected to VL X' checkbox
        based on the view-model's VL subset state."""
        vm.set_selected_vl(_state.selected_vl)
        if vm.has_vl_subset():
            only_vl_checkbox.text = (
                f"Only lines connected to VL {_state.selected_vl}"
            )
            only_vl_row.visible = True
            only_vl_checkbox.update()
        else:
            only_vl_row.visible = False
            only_vl_checkbox.value = False
            only_vl_checkbox.update()

    async def refresh() -> None:
        if _state.network is None:
            vm.clear()
            placeholder.set_text(
                "Load a network and run a load flow to see "
                "Pmax visualization.",
            )
            placeholder.visible = True
            only_vl_row.visible = False
            summary_card.visible = False
            detail_card.visible = False
            return
        try:
            df = await asyncio.to_thread(
                compute_pmax_data, _state.network,
            )
        except Exception as exc:
            placeholder.set_text(f"Pmax visualization failed: {exc}")
            placeholder.visible = True
            summary_card.visible = False
            detail_card.visible = False
            vm.set_data(None)
            return
        vm.set_data(df)
        if vm.is_empty():
            placeholder.set_text(
                "No data available. Make sure a load flow has been "
                "run and the network contains transmission lines.",
            )
            placeholder.visible = True
            summary_card.visible = False
            detail_card.visible = False
            return
        _update_vl_visibility()
        _apply_vl_filter()

    return refresh


def _build_voltage_analysis():
    """Materialise the "Voltage Analysis" tab.

    Composes the shared :mod:`iidm_viewer.voltage_analysis_core` core
    (compute + summary / detail / shunt / SVC display builders) with
    NiceGUI widgets:

    * a bus-voltage summary aggrid + a per-nominal drill-down whose
      ``V (pu)`` cells turn red when outside the user-set lo/hi band,
    * a geographical voltage map (Leaflet markers per VL, fanned or
      per-substation worst) hosted in a ``srcdoc`` iframe and driven
      by :func:`iidm_viewer.voltage_map.build_voltage_map_html`,
    * three shunt-group cards (capacitive, inductive, unknown), each
      with active / available / capacity totals + a detail aggrid,
    * an SVC card with active injection + controllable range totals.

    Returns a closure the page-wide listeners call on network /
    load-flow changes.
    """
    import html as html_lib

    import pandas as pd

    from iidm_viewer.voltage_analysis_core import (
        BUS_DETAIL_COLUMNS,
        SHUNT_DISPLAY_COLUMNS,
        SVC_DISPLAY_COLUMNS,
        build_bus_detail,
        build_bus_summary,
        build_shunt_display,
        build_svc_display,
        bus_pu_classify,
        compute_voltage_analysis,
        has_loadflow,
        list_nominal_voltages,
        shunt_totals,
        split_shunts_by_b,
        svc_totals,
    )
    from iidm_viewer.voltage_map import (
        _LAYOUT_OPTIONS,
        _VIEW_OPTIONS,
        TRANSPORT_NOMINAL_V_THRESHOLD,
        _extract_voltage_map_data,
        build_voltage_map_html,
        nominal_voltage_options,
        voltage_map_caption,
    )

    state: dict = {
        "buses": pd.DataFrame(),
        "shunts": pd.DataFrame(),
        "svcs": pd.DataFrame(),
        # ``None`` until the worker fetch returns; an empty
        # ``{"records": [], "has_lf": False}`` is also legal — the
        # render code distinguishes the two via record count.
        "map_data": None,
    }

    placeholder = ui.label(
        "Load a network to see voltage analysis.",
    ).classes("text-caption q-pa-md")

    # ------------------------------------------------------------------
    # Bus voltages section
    # ------------------------------------------------------------------
    bus_card = ui.card().classes("w-full q-pa-sm")
    with bus_card:
        ui.label("Bus voltages by nominal level").classes("text-h6")
        lf_warning = ui.label(
            "Voltage magnitudes are not available — run a load flow first.",
        ).classes("text-caption q-pa-xs")
        lf_warning.style("color: #b35a00; background: #fff7e6; "
                          "border: 1px solid #ffd591; border-radius: 4px;")
        lf_warning.visible = False
        summary_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 220px")

        ui.label("Bus detail").classes("text-subtitle1 q-mt-sm")
        detail_controls = ui.row().classes("items-center w-full no-wrap q-gutter-sm")
        with detail_controls:
            ui.label("Nominal voltage (kV):")
            nom_select = ui.select(options=[], value=None) \
                .props("dense outlined").classes("w-32")
            ui.label("Low (pu):")
            lo_input = ui.number(value=0.95, step=0.01, format="%.3f") \
                .props("dense outlined").classes("w-24")
            ui.label("High (pu):")
            hi_input = ui.number(value=1.05, step=0.01, format="%.3f") \
                .props("dense outlined").classes("w-24")
        detail_caption = ui.label("").classes("text-caption q-mt-xs")
        detail_grid = ui.aggrid({
            "columnDefs": [], "rowData": [],
            "defaultColDef": _DEFAULT_COL_DEF,
        }).classes("w-full").style("height: 340px")
    bus_card.visible = False

    # ------------------------------------------------------------------
    # Geographical voltage map section
    # ------------------------------------------------------------------
    map_card = ui.card().classes("w-full q-pa-sm")
    with map_card:
        ui.label("Geographical voltage map").classes("text-h6")
        map_status_lbl = ui.label("").classes("text-caption text-grey-7")
        map_status_lbl.visible = False
        map_controls_row = ui.row() \
            .classes("items-center q-gutter-md no-wrap w-full")
        with map_controls_row:
            ui.label("Nominal:")
            map_nom_select = ui.select(
                options={None: "All nominal voltages"}, value=None,
            ).props("dense outlined").classes("w-64")
            ui.label("Layout:")
            map_layout_select = ui.select(
                options=dict(_LAYOUT_OPTIONS),
                value=next(iter(_LAYOUT_OPTIONS.values())),
            ).props("dense outlined").classes("w-48")
            ui.label("View:")
            map_view_select = ui.select(
                options=dict(_VIEW_OPTIONS),
                value=next(iter(_VIEW_OPTIONS.values())),
            ).props("dense outlined").classes("w-48")
            ui.label("Full-scale ± pu:")
            map_vrange_input = ui.number(
                value=0.05, min=0.005, max=0.5, step=0.005, format="%.3f",
            ).props("dense outlined").classes("w-28")
        map_iframe_holder = ui.html("", sanitize=False).classes("w-full")
        map_caption_lbl = ui.label("").classes("text-caption q-mt-xs")
    map_card.visible = False

    # ------------------------------------------------------------------
    # Reactive compensation section
    # ------------------------------------------------------------------
    reactive_card = ui.card().classes("w-full q-pa-sm")
    with reactive_card:
        ui.label("Reactive compensation").classes("text-h6")
        reactive_caption = ui.label(
            "Current Q — Q from the network file when available, otherwise "
            "estimated as −b × V²_nom. Sign convention: Q < 0 for "
            "capacitors, Q > 0 for reactors.",
        ).classes("text-caption q-pa-xs")
        reactive_caption.style(
            "color: #1b5e8b; background: #eef5fb; "
            "border: 1px solid #b6d7ee; border-radius: 4px;",
        )

        reactive_empty_lbl = ui.label(
            "No reactive compensation equipment found in this network.",
        ).classes("text-caption q-pa-sm")
        reactive_empty_lbl.visible = False

        # Shunt sub-section.
        shunt_label = ui.label("Shunt compensators") \
            .classes("text-subtitle1 q-mt-sm")
        shunt_lf_note = ui.label(
            "No load flow — injections estimated as b × nominal_v².",
        ).classes("text-caption text-grey-7")
        shunt_lf_note.visible = False

        def _new_shunt_group(title: str):
            """Bundle of widgets for one shunt group (cap / ind / unknown)."""
            card = ui.card().classes("w-full q-pa-sm q-mt-sm")
            with card:
                ui.label(title).classes("text-subtitle2")
                info_lbl = ui.label("").classes("text-caption text-grey-7")
                info_lbl.visible = False
                with ui.row().classes("items-stretch q-gutter-sm no-wrap"):
                    active_lbl = ui.label("—").classes("col text-center q-pa-xs") \
                        .style("border: 1px solid #ddd; border-radius: 4px;")
                    available_lbl = ui.label("—").classes("col text-center q-pa-xs") \
                        .style("border: 1px solid #ddd; border-radius: 4px;")
                    capacity_lbl = ui.label("—").classes("col text-center q-pa-xs") \
                        .style("border: 1px solid #ddd; border-radius: 4px;")
                grid = ui.aggrid({
                    "columnDefs": [], "rowData": [],
                    "defaultColDef": _DEFAULT_COL_DEF,
                }).classes("w-full").style("height: 220px")
            return {
                "card": card,
                "info": info_lbl,
                "active": active_lbl,
                "available": available_lbl,
                "capacity": capacity_lbl,
                "grid": grid,
            }

        cap_widgets = _new_shunt_group(
            "Capacitive (b > 0, Q < 0) — injects reactive power, raises voltage",
        )
        ind_widgets = _new_shunt_group(
            "Inductive (b < 0, Q > 0) — absorbs reactive power, lowers voltage",
        )
        unk_widgets = _new_shunt_group(
            "Unclassified (b per section unknown — fully disconnected)",
        )

        # SVC sub-section.
        svc_label = ui.label("Static VAR compensators") \
            .classes("text-subtitle1 q-mt-sm")
        svc_card = ui.card().classes("w-full q-pa-sm q-mt-sm")
        with svc_card:
            with ui.row().classes("items-stretch q-gutter-sm no-wrap"):
                svc_active_lbl = ui.label("—").classes("col text-center q-pa-xs") \
                    .style("border: 1px solid #ddd; border-radius: 4px;")
                svc_range_lbl = ui.label("—").classes("col text-center q-pa-xs") \
                    .style("border: 1px solid #ddd; border-radius: 4px;")
            svc_grid = ui.aggrid({
                "columnDefs": [], "rowData": [],
                "defaultColDef": _DEFAULT_COL_DEF,
            }).classes("w-full").style("height: 220px")
    reactive_card.visible = False

    # ------------------------------------------------------------------
    # Grid helpers
    # ------------------------------------------------------------------
    def _set_grid_from_df(grid, df: pd.DataFrame, columns=None) -> None:
        cols = list(columns) if columns is not None else list(df.columns)
        if df.empty:
            grid.options.update({
                "columnDefs": [{"field": c, "headerName": c} for c in cols],
                "rowData": [],
                "defaultColDef": _DEFAULT_COL_DEF,
            })
        else:
            grid.options.update({
                "columnDefs": [
                    {"field": c, "headerName": c} for c in df.columns
                ],
                "rowData": df.fillna("").astype(str).to_dict("records"),
                "defaultColDef": _DEFAULT_COL_DEF,
            })
        grid.update()

    def _set_detail_grid(df: pd.DataFrame, lo: float, hi: float) -> None:
        """Bus detail grid with V (pu) cell colouring driven by the
        shared :func:`bus_pu_classify`."""
        col_defs = []
        for col in BUS_DETAIL_COLUMNS:
            entry = {"field": col, "headerName": col}
            if col == "V (pu)":
                entry["cellStyle"] = (
                    "function(params){"
                    "var v=params.value;"
                    "if(v==null||v===''){return null;}"
                    "var n=Number(v);"
                    "if(isNaN(n)){return null;}"
                    f"if(n<{lo}||n>{hi}){{"
                    "return {'backgroundColor':'#ff4b4b','color':'white'};}"
                    "return null;}"
                )
            col_defs.append(entry)
        detail_grid.options.update({
            "columnDefs": col_defs,
            "rowData": df.fillna("").astype(str).to_dict("records"),
            "defaultColDef": _DEFAULT_COL_DEF,
        })
        detail_grid.update()

    # ------------------------------------------------------------------
    # Render: bus voltages
    # ------------------------------------------------------------------
    def _render_detail() -> None:
        if state["buses"].empty or not has_loadflow(state["buses"]):
            _set_grid_from_df(
                detail_grid,
                pd.DataFrame(columns=BUS_DETAIL_COLUMNS),
                columns=BUS_DETAIL_COLUMNS,
            )
            detail_caption.set_text("")
            return
        try:
            nominal = float(nom_select.value) if nom_select.value is not None else None
        except (TypeError, ValueError):
            nominal = None
        if nominal is None:
            _set_grid_from_df(
                detail_grid,
                pd.DataFrame(columns=BUS_DETAIL_COLUMNS),
                columns=BUS_DETAIL_COLUMNS,
            )
            detail_caption.set_text("")
            return
        try:
            lo = float(lo_input.value)
            hi = float(hi_input.value)
        except (TypeError, ValueError):
            lo, hi = 0.95, 1.05
        df = build_bus_detail(state["buses"], nominal)
        _set_detail_grid(df, lo, hi)
        if df.empty:
            outside = 0
        else:
            outside = int(
                df["V (pu)"]
                .apply(lambda v: bus_pu_classify(v, lo, hi) == "warning")
                .sum()
            )
        detail_caption.set_text(
            f"{len(df)} buses at {nominal} kV — "
            f"{outside} outside [{lo:.3f}, {hi:.3f}] pu"
        )

    nom_select.on(
        "update:model-value", lambda _e=None: _render_detail(),
    )
    lo_input.on(
        "update:model-value", lambda _e=None: _render_detail(),
    )
    hi_input.on(
        "update:model-value", lambda _e=None: _render_detail(),
    )

    def _render_bus_section() -> None:
        buses = state["buses"]
        lf = has_loadflow(buses)
        lf_warning.visible = not lf
        _set_grid_from_df(summary_grid, build_bus_summary(buses))
        detail_controls.visible = lf
        detail_caption.visible = lf
        detail_grid.visible = lf
        if not lf:
            return
        nom_options = [str(v) for v in list_nominal_voltages(buses)]
        previous = nom_select.value
        nom_select.options = nom_options
        if previous in nom_options:
            nom_select.value = previous
        elif nom_options:
            nom_select.value = nom_options[0]
        else:
            nom_select.value = None
        nom_select.update()
        _render_detail()

    # ------------------------------------------------------------------
    # Render: geographical voltage map
    # ------------------------------------------------------------------
    def _set_map_unavailable(message: str) -> None:
        map_status_lbl.set_text(message)
        map_status_lbl.visible = True
        map_controls_row.visible = False
        map_iframe_holder.set_content("")
        map_caption_lbl.set_text("")

    def _render_map() -> None:
        data = state.get("map_data")
        if data is None:
            _set_map_unavailable(
                "No geographical data available. The network needs a "
                "'substationPosition' extension with latitude/longitude "
                "coordinates."
            )
            return
        records = data.get("records") or []
        transport = [
            r for r in records
            if r["nominal_v"] >= TRANSPORT_NOMINAL_V_THRESHOLD
        ]
        if not transport:
            _set_map_unavailable(
                f"No voltage levels at or above "
                f"{TRANSPORT_NOMINAL_V_THRESHOLD:g} kV with geographical "
                "coordinates in this network."
            )
            return
        if not data.get("has_lf"):
            _set_map_unavailable(
                "Voltage magnitudes are not available on the map — "
                "run a load flow first."
            )
            return

        map_status_lbl.visible = False
        map_controls_row.visible = True

        # Sync the nominal-voltage picker with the data on hand.
        counts: dict[float, int] = {}
        for r in transport:
            key = round(r["nominal_v"], 3)
            counts[key] = counts.get(key, 0) + 1
        options: dict = {None: "All nominal voltages"}
        for nv in nominal_voltage_options(transport):
            options[float(nv)] = f"{nv:g} kV ({counts.get(nv, 0)} VL)"
        previous = map_nom_select.value
        map_nom_select.options = options
        if previous not in options:
            map_nom_select.value = None
        map_nom_select.update()

        try:
            v_range = float(map_vrange_input.value)
        except (TypeError, ValueError):
            v_range = 0.05
        try:
            sel_nom = (
                float(map_nom_select.value)
                if map_nom_select.value is not None else None
            )
        except (TypeError, ValueError):
            sel_nom = None
        html_doc, display = build_voltage_map_html(
            records,
            sel_nom=sel_nom,
            layout=map_layout_select.value or "per_vl",
            mode=map_view_select.value or "icons",
            v_range=v_range,
        )
        if not html_doc:
            map_iframe_holder.set_content("")
            map_caption_lbl.set_text(
                "No voltage levels match the current filter.",
            )
            return
        # ``srcdoc`` keeps the Leaflet document self-contained and
        # sandboxed against the rest of the page — no need to mount a
        # static file. Wrap it in an iframe sized to match Streamlit.
        escaped = html_lib.escape(html_doc, quote=True)
        map_iframe_holder.set_content(
            f'<iframe srcdoc="{escaped}" '
            'style="width:100%;height:640px;border:none;display:block" '
            'sandbox="allow-scripts"></iframe>'
        )
        map_caption_lbl.set_text(voltage_map_caption(
            display, sel_nom=sel_nom, layout=map_layout_select.value or "per_vl",
        ))

    for widget in (
        map_nom_select, map_layout_select, map_view_select, map_vrange_input,
    ):
        widget.on("update:model-value", lambda _e=None: _render_map())

    # ------------------------------------------------------------------
    # Render: reactive compensation
    # ------------------------------------------------------------------
    def _render_shunt_group(widgets, group, has_lf: bool, empty_msg: str) -> None:
        if group.empty:
            widgets["info"].set_text(empty_msg)
            widgets["info"].visible = True
            widgets["active"].set_text("—")
            widgets["available"].set_text("—")
            widgets["capacity"].set_text("—")
            _set_grid_from_df(
                widgets["grid"],
                pd.DataFrame(columns=SHUNT_DISPLAY_COLUMNS),
                columns=SHUNT_DISPLAY_COLUMNS,
            )
            return
        widgets["info"].visible = False
        active, available, capacity = shunt_totals(group)
        label_active = "Active" if has_lf else "Estimated"
        widgets["active"].set_text(f"{label_active} (MVAr): {active:.2f}")
        widgets["available"].set_text(
            f"Available not activated (MVAr): {available:.2f}",
        )
        widgets["capacity"].set_text(
            f"Total capacity (MVAr): {capacity:.2f}",
        )
        _set_grid_from_df(widgets["grid"], build_shunt_display(group))

    def _render_reactive_section() -> None:
        shunts = state["shunts"]
        svcs = state["svcs"]
        has_shunts = not shunts.empty
        has_svcs = not svcs.empty
        if not has_shunts and not has_svcs:
            reactive_empty_lbl.visible = True
            reactive_caption.visible = False
            shunt_label.visible = False
            shunt_lf_note.visible = False
            cap_widgets["card"].visible = False
            ind_widgets["card"].visible = False
            unk_widgets["card"].visible = False
            svc_label.visible = False
            svc_card.visible = False
            return
        reactive_empty_lbl.visible = False
        reactive_caption.visible = True

        # Shunts
        shunt_label.visible = True
        if has_shunts:
            has_lf = bool(shunts["q"].notna().any())
            shunt_lf_note.visible = not has_lf
            cap, ind, unk = split_shunts_by_b(shunts)
            cap_widgets["card"].visible = True
            _render_shunt_group(
                cap_widgets, cap, has_lf,
                "No capacitive shunt compensators in this network.",
            )
            ind_widgets["card"].visible = True
            _render_shunt_group(
                ind_widgets, ind, has_lf,
                "No inductive shunt compensators in this network.",
            )
            unk_widgets["card"].visible = not unk.empty
            if not unk.empty:
                _render_shunt_group(unk_widgets, unk, has_lf, "")
        else:
            shunt_lf_note.visible = False
            cap_widgets["card"].visible = True
            _render_shunt_group(
                cap_widgets, pd.DataFrame(), False,
                "No shunt compensators in this network.",
            )
            ind_widgets["card"].visible = False
            unk_widgets["card"].visible = False

        # SVCs
        svc_label.visible = True
        svc_card.visible = True
        if has_svcs:
            has_lf = bool(svcs["current_q_mvar"].notna().any())
            active, total_range = svc_totals(svcs)
            if has_lf:
                svc_active_lbl.set_text(
                    f"Active injection (MVAr): {active:.2f}",
                )
            else:
                svc_active_lbl.set_text(
                    "Active injection (MVAr): — (run a load flow first)",
                )
            svc_range_lbl.set_text(
                f"Total controllable range (MVAr): {total_range:.2f}",
            )
            _set_grid_from_df(svc_grid, build_svc_display(svcs))
        else:
            svc_active_lbl.set_text(
                "No static VAR compensators in this network.",
            )
            svc_range_lbl.set_text("")
            _set_grid_from_df(
                svc_grid,
                pd.DataFrame(columns=SVC_DISPLAY_COLUMNS),
                columns=SVC_DISPLAY_COLUMNS,
            )

    # ------------------------------------------------------------------
    # Refresh closure
    # ------------------------------------------------------------------
    async def refresh() -> None:
        if _state.network is None:
            state["buses"] = pd.DataFrame()
            state["shunts"] = pd.DataFrame()
            state["svcs"] = pd.DataFrame()
            state["map_data"] = None
            placeholder.set_text("Load a network to see voltage analysis.")
            placeholder.visible = True
            bus_card.visible = False
            map_card.visible = False
            reactive_card.visible = False
            return
        try:
            data = await asyncio.to_thread(
                compute_voltage_analysis, _state.network,
            )
        except Exception as exc:
            placeholder.set_text(f"Voltage analysis failed: {exc}")
            placeholder.visible = True
            bus_card.visible = False
            map_card.visible = False
            reactive_card.visible = False
            state["buses"] = pd.DataFrame()
            state["shunts"] = pd.DataFrame()
            state["svcs"] = pd.DataFrame()
            state["map_data"] = None
            return
        state["buses"] = data.buses
        state["shunts"] = data.shunts
        state["svcs"] = data.svcs
        if data.buses.empty:
            placeholder.set_text("No bus data available in this network.")
            placeholder.visible = True
            bus_card.visible = False
            map_card.visible = False
            reactive_card.visible = False
            return
        # Map is best-effort — its fetch failing shouldn't hide the
        # bus and reactive sections.
        try:
            state["map_data"] = await asyncio.to_thread(
                _extract_voltage_map_data, _state.network,
            )
        except Exception:
            state["map_data"] = None
        placeholder.visible = False
        bus_card.visible = True
        map_card.visible = True
        reactive_card.visible = True
        _render_bus_section()
        _render_map()
        _render_reactive_section()

    return refresh


def _build_injection_map():
    """Materialise the "Injection Map" tab.

    Composes the shared :mod:`iidm_viewer.injection_map` helpers with
    NiceGUI widgets: a metric (P / Q) radio, a view-mode (icons /
    gradient) radio, a full-scale ± unit number, a ``srcdoc`` iframe
    that hosts the standalone Leaflet HTML returned by
    :func:`~iidm_viewer.injection_map.build_injection_map_html`, plus
    a caption below the map.

    The data fetch (``_extract_injection_data``) hops to a worker
    thread via ``asyncio.to_thread`` and runs once per network.
    Control changes drive an in-memory HTML re-build — no pypowsybl
    re-query.
    """
    import html as html_lib

    from iidm_viewer.injection_map import (
        TRANSPORT_NOMINAL_V_THRESHOLD,
        _METRIC_OPTIONS,
        _VIEW_OPTIONS,
        InjectionMapViewModel,
        _extract_injection_data,
        build_injection_map_html,
        injection_map_caption,
        metric_unit,
    )

    vm = InjectionMapViewModel()

    ui.label(
        "Net active or reactive power per substation. "
        "Green = net exporter (generation > load), red = net importer "
        "(load > generation). Marker size scales with the absolute net "
        "injection.",
    ).classes("text-caption q-pa-sm")

    status_lbl = ui.label("").classes("text-caption q-pa-md")
    status_lbl.visible = False

    controls_row = ui.row().classes("items-center q-gutter-md no-wrap w-full")
    with controls_row:
        ui.label("Metric:")
        metric_select = ui.select(
            options=dict(_METRIC_OPTIONS),
            value=next(iter(_METRIC_OPTIONS.values())),
        ).props("dense outlined").classes("w-48")
        ui.label("View:")
        view_select = ui.select(
            options=dict(_VIEW_OPTIONS),
            value=next(iter(_VIEW_OPTIONS.values())),
        ).props("dense outlined").classes("w-48")
        scale_label = ui.label("Full-scale ± MW:")
        scale_input = ui.number(
            value=500.0, min=1.0, max=100000.0, step=50.0, format="%.0f",
        ).props("dense outlined").classes("w-36")
    controls_row.visible = False

    lf_note_lbl = ui.label("").classes("text-caption text-grey-7 q-pa-xs")
    lf_note_lbl.visible = False

    map_iframe_holder = ui.html("", sanitize=False).classes("w-full")
    caption_lbl = ui.label("").classes("text-caption q-mt-xs")

    def _current_metric() -> str:
        return metric_select.value or "P"

    def _current_mode() -> str:
        return view_select.value or "icons"

    def _set_unavailable(message: str) -> None:
        status_lbl.set_text(message)
        status_lbl.visible = True
        controls_row.visible = False
        lf_note_lbl.visible = False
        map_iframe_holder.set_content("")
        caption_lbl.set_text("")

    def _update_lf_note() -> None:
        if vm.data is None:
            lf_note_lbl.visible = False
            return
        metric = _current_metric()
        if vm.has_lf(metric):
            lf_note_lbl.visible = False
            return
        fallback = "p0" if metric == "P" else "q0"
        lf_note_lbl.set_text(
            f"No terminal {metric} values populated (no load flow). "
            f"Showing scheduled setpoints (target_{metric.lower()} / "
            f"{fallback})."
        )
        lf_note_lbl.visible = True

    def _seed_default_scale(records) -> None:
        metric = _current_metric()
        target = vm.get_scale(metric, records=records)
        vm.set_scale(metric, target)
        scale_label.set_text(f"Full-scale ± {metric_unit(metric)}:")
        scale_input.value = target
        scale_input.update()

    def _render_map() -> None:
        if vm.data is None:
            return
        metric = _current_metric()
        try:
            full_scale = float(scale_input.value)
        except (TypeError, ValueError):
            full_scale = 500.0
        vm.set_scale(metric, full_scale)
        html_doc, transport = build_injection_map_html(
            vm.records(),
            metric=metric,
            mode=_current_mode(),
            full_scale=full_scale,
        )
        _update_lf_note()
        if not html_doc:
            map_iframe_holder.set_content("")
            caption_lbl.set_text(
                f"No substations with a voltage level at or above "
                f"{TRANSPORT_NOMINAL_V_THRESHOLD:g} kV match the filter."
            )
            return
        # ``srcdoc`` keeps the Leaflet document self-contained and
        # sandboxed against the rest of the page — same trick as the
        # geographical voltage map.
        escaped = html_lib.escape(html_doc, quote=True)
        map_iframe_holder.set_content(
            f'<iframe srcdoc="{escaped}" '
            'style="width:100%;height:640px;border:none;display:block" '
            'sandbox="allow-scripts"></iframe>'
        )
        caption_lbl.set_text(injection_map_caption(transport, metric))

    def _on_metric_changed(_e=None) -> None:
        if vm.data is None:
            return
        _seed_default_scale(vm.records(transport_only=True))
        _render_map()

    metric_select.on("update:model-value", _on_metric_changed)
    view_select.on("update:model-value", lambda _e=None: _render_map())
    scale_input.on("update:model-value", lambda _e=None: _render_map())

    async def refresh() -> None:
        if _state.network is None:
            vm.clear()
            _set_unavailable("Load a network to see the injection map.")
            return
        try:
            data = await asyncio.to_thread(
                _extract_injection_data, _state.network,
            )
        except Exception as exc:
            vm.clear()
            _set_unavailable(f"Injection map failed: {exc}")
            return
        # Network swap → forget the previous network's per-metric scales.
        vm.clear()
        vm.set_data(data)
        if vm.data is None:
            _set_unavailable(
                "No geographical data available. The network needs a "
                "'substationPosition' extension with latitude/longitude "
                "coordinates."
            )
            return
        records = vm.records(transport_only=True)
        if not records:
            _set_unavailable(
                f"No substations with a voltage level at or above "
                f"{TRANSPORT_NOMINAL_V_THRESHOLD:g} kV in this network."
            )
            return
        status_lbl.visible = False
        controls_row.visible = True
        _seed_default_scale(records)
        _render_map()

    return refresh


def _cast_value_for_col(series, raw):
    """Best-effort cast for a user-typed cell value, matching the
    source DataFrame's column dtype."""
    import pandas as pd
    sample = None
    for v in series:
        if v is not None and not (isinstance(v, float) and pd.isna(v)):
            sample = v
            break
    if isinstance(sample, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "1", "yes", "on")
    if isinstance(sample, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if isinstance(sample, float):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float("nan")
    return raw if isinstance(raw, str) else (None if raw is None else str(raw))


def _after_revert(touched, refresh_data_grid) -> None:
    """Post-revert: invalidate diagram caches for topology-affecting
    attributes and refresh the data grid so the current view reflects
    the reverted network state.
    """
    if any(attr in TOPOLOGY_AFFECTING_ATTRIBUTES for _, attr in touched):
        _invalidate_diagram_caches()
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
        with ui.row().classes("items-center w-full no-wrap"):
            file_lbl = ui.label("No file loaded.").classes("text-caption")
            unload_btn = ui.button(icon="close") \
                .props("flat dense round size=sm") \
                .tooltip("Unload network")
            unload_btn.visible = False
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
                # Mirror Streamlit's load-network recording so the
                # Session Script reproduces the upload at replay time.
                script_recorder.record_load_network(
                    name,
                    _state.import_params or None,
                    _state.import_post_processors or None,
                )
            except Exception as exc:
                ui.notify(f"Load failed: {exc}", type="negative")
                return
            file_lbl.set_text(os.path.basename(name))
            upload_widget.reset()

        upload_widget = ui.upload(
            on_upload=handle_upload,
            auto_upload=True,
            label="Load network…",
        ).props("flat dense accept='.xiidm,.iidm,.xml,.zip,.mat,.uct'") \
         .classes("full-width q-mb-sm")

        # "Start with empty network" mirrors the Streamlit dialog —
        # prompts for an id and installs a blank pypowsybl Network so
        # the user can build a model from scratch via the Data
        # Explorer's "Create a new …" forms.
        ui.button(
            "Start with empty network",
            on_click=lambda: _open_blank_network_dialog(file_lbl),
        ).props("flat dense").classes("full-width q-mb-sm")

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

        # "Network Reduction" opens the three-mode irreversible
        # reduction modal. Disabled until a network is loaded.
        reduction_btn = ui.button(
            "Network Reduction", on_click=_open_network_reduction_dialog,
        ).props("flat dense").classes("full-width q-mb-sm")
        reduction_btn.set_enabled(False)

        def _on_unload_network() -> None:
            _state.install_network(None)
            file_lbl.set_text("No file loaded.")
            unload_btn.visible = False
            upload_widget.reset()

        unload_btn.on_click(_on_unload_network)

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
            previous = vl_select.value
            filter_text = vl_filter_input.value or ""
            if filter_text:
                # Active filter: always jump to the first match so the
                # user sees the diagram update as they type.
                current = next(iter(options)) if options else None
            else:
                # No filter: preserve the previous selection when it is
                # still present, otherwise fall back to the first entry.
                current = previous if previous in options else None
                if current is None and options:
                    current = next(iter(options))
            vl_select.options = options
            vl_select.value = current
            vl_select.update()
            # Push into the app state so the SLD / NAD / data grid follow.
            if current and current != previous:
                _state.set_selected_vl(str(current))

        def _on_vl_filter_changed(_e=None) -> None:
            _rebuild_vl_options()

        def _on_vl_filter_enter() -> None:
            """Enter in the filter confirms the current dropdown value."""
            vl_id = vl_select.value
            if vl_id:
                _state.set_selected_vl(str(vl_id))

        def _on_vl_select_changed(_e=None) -> None:
            # Skip while we're programmatically syncing the widget from
            # AppState (set_selected_vl will fire again otherwise).
            if vl_picker_state["suppress_listener"]:
                return
            vl_id = vl_select.value
            if vl_id:
                _state.set_selected_vl(str(vl_id))

        vl_filter_input.on_value_change(_on_vl_filter_changed)
        vl_filter_input.on("keydown.enter", lambda _e: _on_vl_filter_enter())
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
                # run_loadflow fires its listeners synchronously, which
                # would run _on_loadflow_completed on the worker thread
                # and crash NiceGUI ("slot stack is empty").  Run the
                # heavy pypowsybl work on the thread but call the
                # listener explicitly afterwards, back on the event loop.
                result = await asyncio.to_thread(_state.run_loadflow_no_notify)
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
            # Now safe to refresh UI — we're back on the event loop.
            _on_loadflow_completed(result)

        run_lf_btn.on_click(on_run_lf)
        view_logs_btn.on_click(
            lambda: _open_lf_report_dialog(_state.last_report_json),
        )

        # "View live Script" — auto-recorded HMI-mirror script for this
        # session. Always available; the dialog handles the empty-log
        # state. Mirrors the Streamlit + PySide6 sidebars.
        view_script_btn = ui.button("View live Script") \
            .props("flat dense").classes("full-width q-mt-sm")
        view_script_btn.on_click(_open_session_script_dialog)

    with ui.tabs().classes("w-full") as tabs:
        overview_tab = ui.tab("Overview")
        map_tab = ui.tab("Network Map")
        nad_tab = ui.tab("Network Area Diagram")
        sld_tab = ui.tab("Single Line Diagram")
        data_tab = ui.tab("Data Explorer Components")
        extensions_tab = ui.tab("Data Explorer Extensions")
        reactive_curves_tab = ui.tab("Reactive Capability Curves")
        operational_limits_tab = ui.tab("Operational Limits")
        security_analysis_tab = ui.tab("Security Analysis")
        short_circuit_tab = ui.tab("Short Circuit Analysis")
        pmax_tab = ui.tab("Pmax Visualization")
        voltage_analysis_tab = ui.tab("Voltage Analysis")
        injection_map_tab = ui.tab("Injection Map")
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
                'style="width:100%;height:calc(100vh - 160px);min-height:500px;'
                'border:none;display:block;margin:0 auto"></iframe>',
                sanitize=False,
            ).style("display:flex;justify-content:center").classes("w-full")
        with ui.tab_panel(sld_tab).classes("q-pa-none w-full"):
            with ui.row().classes("items-center q-pa-sm w-full"):
                sld_vl_label = ui.label("").classes("text-caption")
                sld_expand_btn = ui.button("Expand to substation") \
                    .props("flat dense").classes("q-ml-md")
                sld_expand_btn.visible = False
            ui.html(
                f'<iframe id="iidm-sld-iframe" src="{_SLD_URL}/index.html" '
                'style="width:100%;height:calc(100vh - 180px);min-height:500px;'
                'border:none;display:block;margin:0 auto"></iframe>',
                sanitize=False,
            ).style("display:flex;justify-content:center").classes("w-full")
        def _on_topology_changed():
            """Rebuild VL picker + flush diagram caches after a create/delete."""
            try:
                from iidm_viewer.network_loader import list_voltage_levels_for_selector
                vl_picker_state["df"] = list_voltage_levels_for_selector(_state.network)
            except Exception:
                pass
            _rebuild_vl_options()
            _invalidate_diagram_caches()
            if _state.selected_vl:
                _push_sld(_state.selected_vl)
                _push_nad(_state.selected_vl, _nad_depth)

        with ui.tab_panel(data_tab).classes("w-full"):
            refresh_data_grid = _build_data_explorer(on_topology_changed=_on_topology_changed)
        with ui.tab_panel(extensions_tab).classes("w-full"):
            refresh_extensions_tab = _build_extensions_explorer()
        with ui.tab_panel(reactive_curves_tab).classes("w-full"):
            refresh_reactive_curves = _build_reactive_curves()
        with ui.tab_panel(operational_limits_tab).classes("w-full"):
            refresh_operational_limits = _build_operational_limits()
        with ui.tab_panel(security_analysis_tab).classes("w-full"):
            refresh_security_analysis = _build_security_analysis()
        with ui.tab_panel(short_circuit_tab).classes("w-full"):
            refresh_short_circuit_analysis = _build_short_circuit_analysis()
        with ui.tab_panel(pmax_tab).classes("w-full"):
            refresh_pmax = _build_pmax_visualization()
        with ui.tab_panel(voltage_analysis_tab).classes("w-full"):
            refresh_voltage_analysis = _build_voltage_analysis()
        with ui.tab_panel(injection_map_tab).classes("w-full"):
            refresh_injection_map = _build_injection_map()
        with ui.tab_panel(overview_tab).classes("w-full"):
            refresh_overview = _build_overview()

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
        reduction_btn.set_enabled(network is not None)
        unload_btn.visible = network is not None
        lf_status_lbl.set_text("")
        # Swap-network → wipe whatever was rendered for the previous
        # network. ``_on_state_vl`` will refill if a default VL is
        # picked; an empty network has no default VL so the diagrams
        # stay blank rather than showing the previous topology.
        _clear_diagrams()
        # Clear the filter text so the new network starts unfiltered.
        vl_filter_input.value = ""
        if network is None:
            vl_picker_state["df"] = None
            vl_filter_input.visible = False
            vl_select.visible = False
            _rebuild_vl_options()
            refresh_data_grid()
            refresh_reactive_curves()
            refresh_operational_limits()
            refresh_security_analysis()
            refresh_short_circuit_analysis()
            asyncio.create_task(refresh_pmax())
            asyncio.create_task(refresh_voltage_analysis())
            asyncio.create_task(refresh_injection_map())
            asyncio.create_task(refresh_overview())
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
        refresh_reactive_curves()
        refresh_operational_limits()
        refresh_security_analysis()
        refresh_short_circuit_analysis()
        asyncio.create_task(refresh_pmax())
        asyncio.create_task(refresh_voltage_analysis())
        asyncio.create_task(refresh_injection_map())
        asyncio.create_task(refresh_overview())

    def _update_sld_header(vl_id):
        """Refresh the VL label + Expand/Collapse button above the SLD."""
        global _sld_show_substation
        if not vl_id:
            sld_vl_label.set_text("")
            sld_expand_btn.visible = False
            return
        sid, multi_vl = _get_substation_for_vl(vl_id)
        if _sld_show_substation and sid:
            sld_vl_label.set_text(f"Substation: {sid}")
        else:
            sld_vl_label.set_text(f"Voltage level: {vl_id}")
        if sid and multi_vl:
            sld_expand_btn.visible = True
            if _sld_show_substation:
                sld_expand_btn.text = "Collapse to voltage level"
            else:
                sld_expand_btn.text = "Expand to substation"
        else:
            sld_expand_btn.visible = False
            _sld_show_substation = False

    def _on_sld_expand_toggle():
        global _sld_show_substation
        _sld_show_substation = not _sld_show_substation
        vl_id = _state.selected_vl
        if vl_id:
            _update_sld_header(vl_id)
            _push_sld(vl_id)

    sld_expand_btn.on_click(_on_sld_expand_toggle)

    def _on_state_vl(vl_id):
        vl_lbl.set_text(f"VL: {vl_id}" if vl_id else "VL: —")
        _update_sld_header(vl_id)
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
        # Refresh the reactive-curves tab so its "Only generators in
        # VL <id>" checkbox label and any active narrow stay in sync.
        refresh_reactive_curves()
        # Pmax: same "Only lines connected to VL X" affordance.
        asyncio.create_task(refresh_pmax())

    def _on_loadflow_completed(result):
        """LF rewrites line P/Q/I + bus V/angle, baked into the SVGs and
        the enriched DataFrames. Flush the diagram caches and refresh
        whichever VL is active + the data grid + the extensions tab.
        """
        _invalidate_diagram_caches()
        if _state.selected_vl:
            _push_sld(_state.selected_vl)
            _push_nad(_state.selected_vl, _nad_depth)
        refresh_data_grid()
        refresh_extensions_tab()
        # Post-LF the gen ``q`` column flips PV gens from ``needs_lf`` to
        # an actionable status; re-run the classification.
        refresh_reactive_curves()
        # Post-LF the branch I and P/Q flows change → loading_pct +
        # losses + chart need to be re-rendered.
        refresh_operational_limits()
        # Pmax: needs both bus v_mag and line p1 — both come from the
        # LF — so a refresh after a run is the only way to populate it.
        asyncio.create_task(refresh_pmax())
        # Voltage Analysis: bus v_mag + shunt/SVC q come from the LF —
        # refresh so summary, drill-down + current-Q metrics update.
        asyncio.create_task(refresh_voltage_analysis())
        asyncio.create_task(refresh_injection_map())
        asyncio.create_task(refresh_overview())

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
