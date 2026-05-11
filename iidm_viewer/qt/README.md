# `iidm_viewer.qt` — PySide6 desktop preview

A second front-end that explores moving away from Streamlit's
rerun-the-whole-script model. Ships four tabs — **Network Map**,
**Network Area Diagram**, **Single Line Diagram** and
**Data Explorer Components** — covering both the diagram interactions
and a first read-only DataFrame viewer:

* clicking a substation on the map navigates to its SLD;
* clicking a node on the NAD navigates to its SLD;
* the data tab renders any pypowsybl component DataFrame in a native
  `QTableView` (no filtering / editing yet — that's next iteration).

Both jumps activate the SLD tab and render the target VL instantly,
with no script rerun and no websocket round-trip.

## Run it

```bash
pip install 'iidm-viewer[pyside]'    # adds PySide6 (~250 MB)
iidm-viewer-pyside                    # opens an empty window — load via sidebar
iidm-viewer-pyside path/to/net.xiidm  # opens directly on a network
# or: python -m iidm_viewer.qt
```

The Streamlit front-end (`iidm-viewer`) is unchanged and ships in the
same wheel; the PySide6 path is opt-in via the `pyside` extra.

## Architecture

```
   ┌─────────────────────────── QMainWindow ───────────────────────────┐
   │ Sidebar     │ QTabWidget                                          │
   │ • Load…     │  ┌─ Network Map ──────────────────────────────────┐ │
   │ • file lbl  │  │ PowsyblWebView → frontend/map_component/dist   │ │
   │ • VL lbl    │  │   ▲ render_component(substations=…)            │ │
   │             │  │   ▼ value_received: {type:'map-substation-click'}│
   │             │  └────────────────────────────────────────────────┘ │
   │             │  ┌─ Network Area Diagram ─────────────────────────┐ │
   │             │  │ PowsyblWebView → frontend/nad_component/dist   │ │
   │             │  │   ▲ render_component(svg=…, metadata=…)        │ │
   │             │  │   ▼ value_received: {type:'nad-vl-click'}       │ │
   │             │  └────────────────────────────────────────────────┘ │
   │             │  ┌─ Single Line Diagram ──────────────────────────┐ │
   │             │  │ PowsyblWebView → frontend/sld_component/dist   │ │
   │             │  │   ▲ render_component(svg=…, metadata=…)        │ │
   │             │  └────────────────────────────────────────────────┘ │
   └────────────────────────────────────────────────────────────────────┘
                                  │
                          AppState (QObject)
            network_changed / selected_vl_changed  (Qt signals)
                                  │
                  iidm_viewer.powsybl_worker.run(…)
                  (the same single-threaded executor the
                   Streamlit app uses — AGENTS.md §1)
```

### The JS reuse trick

The existing `frontend/{map,sld}_component/dist/index.html` bundles
speak the Streamlit iframe wire-protocol
(`window.parent.postMessage({isStreamlitMessage, type: 'streamlit:…'})`).
Inside `QWebEngineView` there is no parent iframe, so those messages
land back in the bundle's own window. `bridge.js` (injected at
`DocumentCreation` via `QWebEngineScript`) adapts the protocol to a
`QWebChannel`-exposed Python object named `iidm_bridge`:

* JS → Py: every `streamlit:setComponentValue` is JSON-stringified and
  forwarded as `iidm_bridge.onComponentValue(json)`.
* Py → JS: `window.iidmRender(args)` is exposed by the shim;
  `PowsyblWebView.render_component(**args)` is the Python entry point.
* `streamlit:setFrameHeight` and `streamlit:componentReady` are
  swallowed (the iframe-height protocol is meaningless when the view
  fills its host widget).

This means the bundles are **byte-for-byte identical** to what the
Streamlit app ships. No fork, no second build.

### Map → SLD and NAD → SLD wiring

```
   Map: deck.gl onClick on a substation
       │
       ▼
   setComponentValue({type:'map-substation-click', vlIds, …})    (map main.ts)
       │
       ▼   bridge.js → QWebChannel
   MapTab.substation_clicked(vlIds)
       │
       ▼
   MainWindow._on_map_substation_clicked
       │   tabs.setCurrentWidget(sld_tab)
       │   AppState.set_selected_vl(vlIds[0])
       │
       ▼            (signal: selected_vl_changed)
   SldTab.show_voltage_level(vl)
       │
       ▼   cached? no → run(get_single_line_diagram)  (worker thread)
   PowsyblWebView.render_component(svg, metadata, …)


   NAD: NetworkAreaDiagramViewer.onSelectNodeCallback
       │
       ▼
   setComponentValue({type:'nad-vl-click', vl, ts})              (nad main.ts)
       │
       ▼   bridge.js → QWebChannel
   NadTab.node_clicked(vl)
       │
       ▼
   MainWindow._on_nad_node_clicked  →  same path as map → SLD
```

No `st.rerun()`, no full-script execution, no recomputation of the
other 11 tabs that don't exist here.

## pypowsybl thread-affinity rule

Unchanged. `powsybl_worker.run(…)` and `NetworkProxy` are reused as-is.
The pypowsybl isolate binds to the worker thread on first call and
stays there for the lifetime of the process — exactly the same
guarantee the Streamlit path makes (AGENTS.md §1). Qt's GUI thread
never touches pypowsybl directly.

## Test it offscreen

```bash
QT_QPA_PLATFORM=offscreen pytest tests/test_qt_prototype.py -q
```

The smoke test boots the main window, loads `test_ieee14.xiidm`,
synthesises a substation-click signal, and asserts the tab switch +
SLD-cache population. Runs in ~2 s, no display required.
