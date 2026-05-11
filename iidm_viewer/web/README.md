# `iidm_viewer.web` — NiceGUI preview

A third front-end alongside the Streamlit app (`iidm-viewer`) and
the PySide6 prototype (`iidm-viewer-pyside`). Four tabs —
**Network Map**, **Network Area Diagram**, **Single Line Diagram**,
and **Data Explorer Components** — same scope as the PySide6 spike,
so the two can be compared head-to-head on:

* responsiveness of the killer interaction (click a substation on
  the map → land on its SLD);
* effort to reuse the existing JS bundles;
* install footprint and packaging story.

## Run it

```bash
pip install 'iidm-viewer[nicegui]'             # adds nicegui + pywebview
iidm-viewer-nicegui                            # native desktop window
iidm-viewer-nicegui test_ieee14.xiidm          # load on startup
iidm-viewer-nicegui --no-native --port 8669    # localhost web server
# or: python -m iidm_viewer.web
```

The Streamlit and PySide6 paths are unaffected — the `nicegui` extra
is opt-in.

## Architecture

```
   ┌─────────────────────────── NiceGUI page ───────────────────────────┐
   │ header: load · file label · VL label                               │
   │ ┌─ Network Map (tab) ───────────────────────────────────────────┐ │
   │ │ <iframe id="iidm-map-iframe"                                  │ │
   │ │   src="/_iidm/map_component/index.html">                      │ │
   │ │ ←  postMessage  →                                             │ │
   │ └───────────────────────────────────────────────────────────────┘ │
   │ ┌─ Network Area Diagram (tab) ──────────────────────────────────┐ │
   │ │ depth: <ui.number>                                            │ │
   │ │ <iframe id="iidm-nad-iframe"                                  │ │
   │ │   src="/_iidm/nad_component/index.html">                      │ │
   │ └───────────────────────────────────────────────────────────────┘ │
   │ ┌─ Single Line Diagram (tab) ───────────────────────────────────┐ │
   │ │ <iframe id="iidm-sld-iframe"                                  │ │
   │ │   src="/_iidm/sld_component/index.html">                      │ │
   │ └───────────────────────────────────────────────────────────────┘ │
   │ ┌─ Data Explorer Components (tab) ──────────────────────────────┐ │
   │ │ <ui.select> Component:  <ui.aggrid> rows × cols               │ │
   │ └───────────────────────────────────────────────────────────────┘ │
   │ <script> bridge.js: postMessage  ←→  emitEvent  </script>          │
   └────────────────────────────────────────────────────────────────────┘
                            │
                  ui.on('iidm-component-value', …)
                  ui.on('iidm-component-ready', …)
                            │
                       AppState (plain Python)
                  on_network_changed / on_selected_vl_changed
                            │
                  iidm_viewer.powsybl_worker.run(…)
                  (same worker as the Streamlit + PySide6 paths
                  — AGENTS.md §1 thread-affinity rule unchanged)
```

### The JS reuse trick

The existing `frontend/{map,sld}_component/dist/index.html` bundles
speak Streamlit's iframe wire-protocol
(`window.parent.postMessage({isStreamlitMessage, type:'streamlit:…'})`).
Here we re-host them as plain `<iframe src=…>` inside a NiceGUI page.
`window.parent` becomes the NiceGUI top page itself, so the bundles'
outgoing messages land there. A page-level `<script>`
(see `_BRIDGE_JS` in `app.py`) catches them, identifies the source
iframe via `event.source === iframe.contentWindow`, and forwards to
NiceGUI's `emitEvent` bus:

* `streamlit:componentReady`  →  `emitEvent('iidm-component-ready', {component})`
* `streamlit:setComponentValue` →  `emitEvent('iidm-component-value', {component, value})`

On the Python side, `ui.on('iidm-component-value', …)` receives the
events. Outgoing renders use `window.iidmRenderTo(component, args)`,
posted into the iframe via `iframe.contentWindow.postMessage(...)`.

The bundles are **byte-for-byte identical** to what the Streamlit and
PySide6 paths ship. No fork, no second build.

### Map → SLD and NAD → SLD wiring

```
   Map: deck.gl onClick on a substation                     (map main.ts)
       │
       ▼
   setComponentValue({type:'map-substation-click', vlIds, …})
       │  ↳ window.parent === NiceGUI page
       ▼
   page <script> bridge → emitEvent('iidm-component-value', {component:'map', value})
       │
       ▼
   ui.on('iidm-component-value', _on_component_value)
       │
       ├─ tabs.set_value(sld_tab)
       └─ _state.set_selected_vl(vlIds[0])
                   │
                   ▼  (state listener)
            _push_sld(vl); _push_nad(vl, depth)
                   │
                   ▼   cached? no → run(get_single_line_diagram / get_network_area_diagram)
            ui.run_javascript("window.iidmRenderTo(component, {...})")
                   │
                   ▼
            iframe.contentWindow.postMessage({type:'streamlit:render', args})
                   │
                   ▼
            bundle's main.ts renders the SVG, no script rerun


   NAD: NetworkAreaDiagramViewer.onSelectNodeCallback        (nad main.ts)
       │
       ▼
   setComponentValue({type:'nad-vl-click', vl, ts})
       │  ↳ same path as Map → SLD from this point on
       ▼
   tabs.set_value(sld_tab) + _state.set_selected_vl(vl)
```

No full-page reload, no Streamlit-style "rerun the script". Only the
iframe that needs a new payload gets one.

### Data Explorer Components tab

Pure NiceGUI — no iframe. A `ui.select` lists 18 pypowsybl component
types (Substations, Voltage Levels, Buses, Generators, Lines, …);
`ui.aggrid` renders the corresponding DataFrame. Selecting a
different component fires `select.on_value_change`, which fetches
the new DataFrame on the worker thread via `_fetch_dataframe`,
converts it to ag-Grid `{columnDefs, rowData}` via
`_dataframe_to_aggrid_options` (NaN → em-dash, numeric columns
right-aligned), and pushes it into the grid with `grid.options =
... ; grid.update()`. ag-Grid handles sort + column resize for free.
Filtering and editing are left for the next iteration.

## pypowsybl thread-affinity rule

Unchanged. `powsybl_worker.run(…)` and `NetworkProxy` are reused
as-is. All pypowsybl calls (`pn.load`, `get_voltage_levels`,
`get_single_line_diagram`, `NetworkMapWidget.extract_map_data`,
…) run on the single worker thread the GraalVM isolate is bound to.
NiceGUI's event handlers off-load to that thread via the same
helpers as the Streamlit and PySide6 paths.

## Test it

```bash
pytest tests/test_nicegui_prototype.py -q
```

Seven cases. They cover the framework-agnostic surface:

* `AppState` listener semantics — single-source-of-truth + dedup.
* End-to-end pypowsybl helpers against IEEE14 (`_extract_map_data`,
  `_generate_sld`) — proves the worker integration.
* Bridge JS contains every hook the iframe wire-protocol expects.
* Page route and the two static-mount routes register cleanly.

Anything inside the iframes (deck.gl picking, SLD switch animation,
SLD navigation arrows) needs a real browser to exercise. A
Playwright-driven end-to-end is straightforward when a Chromium
download is available; in this sandbox it's been verified by hand
via `curl` against a running server.
