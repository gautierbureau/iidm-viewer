# UI Tabs

## Layout — `app.py`

```
sidebar:  file uploader → vl_selector → Run LF button + ⚙ params button
tabs:     Overview | Network Map | Network Area Diagram | Single Line Diagram
          | Data Explorer Components | Data Explorer Extensions
          | Reactive Capability Curves | Operational Limits
          | Pmax Visualization | Voltage Analysis
```

Each tab has a dedicated render function. `app.py` passes `(network, selected_vl)`
to most of them; `network` is always a `NetworkProxy`.

## Tab inventory

### Overview — `network_info.render_overview`

Displays `st.metric` tiles for network metadata (id, name, format, case_date) and
element counts from `COMPONENT_TYPES`. Counts are fetched with `network.<method>()`.

`COMPONENT_TYPES` is the canonical label→method registry shared with `data_explorer.py`.

### Network Map — `network_map.render_network_map`

Renders an interactive geographical map through the
`map_component` custom Streamlit component (MapLibre + deck.gl +
`@powsybl/network-map-layers`). Only works when the network has a
`substationPosition` extension; otherwise shows an info message.

See [network-map.md](network-map.md) for details.

### Network Area Diagram — `diagrams.render_nad_tab`

Controls: depth slider (0–10).

Calls `network.get_network_area_diagram(voltage_level_ids=[selected_vl], depth=depth, ...)`.
The result is a `NadResult`-like proxy with `.svg` and `.metadata` attributes.

Rendering goes through `nad_component.render_interactive_nad(svg, metadata,
height, key)` — a custom Streamlit component declared from
`iidm_viewer/frontend/nad_component/dist/`. The frontend is a Vite-built
TypeScript wrapper around
[`@powsybl/network-viewer-core`](https://www.npmjs.com/package/@powsybl/network-viewer-core):
the library supplies pan, zoom, drag, hover, and hit-testing; our
`src/main.ts` (~60 lines) speaks the Streamlit wire protocol directly
(no `streamlit-component-lib` dep) and translates
`onSelectNodeCallback(equipmentId, ...)` into
`{"type": "nad-vl-click", "vl": "<equipmentId>", "ts": ...}` via
`setComponentValue`.

`render_nad_tab` writes `vl` into `st.session_state.selected_vl` and calls
`st.rerun()`; session state survives so the uploaded network and NetworkProxy
stay intact. See [future-interactive-viewer.md](future-interactive-viewer.md)
for the full upgrade history and
[`iidm_viewer/frontend/nad_component/README.md`](../iidm_viewer/frontend/nad_component/README.md)
for the build workflow. The parked plan to make a NAD click jump
straight to the **Single Line Diagram** tab is in
[future-interactive-viewer.md § "Follow-up: cross-tab navigation"](future-interactive-viewer.md#follow-up-cross-tab-navigation-nad--map-click--sld-tab).

Note: accessing `.svg` and `.metadata` from the proxy issues two separate `run()`
calls. Do not wrap these inside another `run()` — that deadlocks the executor.

### Single Line Diagram — `diagrams.render_sld_tab`

Calls `network.get_single_line_diagram(selected_vl, parameters=SldParameters(...))`.
Returns an SLD result with `.svg` and `.metadata` attributes.

Rendering goes through `sld_component.render_interactive_sld(svg, metadata,
height, key)` — a custom Streamlit component declared from
`iidm_viewer/frontend/sld_component/dist/`. The frontend is a Vite-built
TypeScript wrapper around
[`@powsybl/network-viewer-core`](https://www.npmjs.com/package/@powsybl/network-viewer-core)'s
`SingleLineDiagramViewer`: the library renders the SLG, places the
"next voltage level" navigation arrows on feeders whose `nextVId`
points to another VL, and hit-tests them; our `src/main.ts` (~90 lines)
speaks the Streamlit wire protocol directly and forwards
`onNextVoltageCallback(nextVId)` into
`{"type": "sld-vl-click", "vl": "<nextVId>", "ts": ...}` via
`setComponentValue`. The constructor is invoked with `svgType =
"voltage-level"` so the library applies VL-scoped zoom limits.

`render_sld_tab` writes `vl` into `st.session_state.selected_vl` and
calls `st.rerun()`; session state survives so the uploaded network and
NetworkProxy stay intact. Same invariant as NAD: accessing `.svg` and
`.metadata` issues two separate `run()` calls — don't wrap them inside
another `run()`.

Below the diagram, `_render_bus_legend(network, selected_vl, svg)` shows
one row per bus in the VL with columns: colored dot, bus id, V (kV),
angle (°). Data comes from `network.get_buses(all_attributes=True)`;
`v_mag` / `v_angle` show `—` until a load flow has run. The dot
colors are parsed out of the SLD SVG itself so they match pypowsybl's
SLG rendering exactly. The resolver (`_resolve_bus_colors`) joins three
pieces:

1. the `--sld-vl-color` palette from the SVG's `<style>` block
   (`(voltage_band, bus_index) → hex`),
2. the `sld-bus-N` class on each `<g class="sld-busbar-section …">`
   element (`busbar_id → (band, index)`), and
3. network topology — `get_busbar_sections()` for node-breaker VLs,
   `get_bus_breaker_topology(vl).buses` for bus-breaker VLs —
   (`busbar_id → calculated bus_id`).

Buses the SVG doesn't tag fall back to `_BUS_LEGEND_PALETTE`. Moving
the legend fully inside the iframe (so it picks up theme changes and
hover highlights automatically) is still parked as "Option B" in
[future-interactive-viewer.md § "Bus-voltage legend — Option B"](future-interactive-viewer.md#bus-voltage-legend--option-b-in-iframe-legend).

### Data Explorer Components — `data_explorer.render_data_explorer`

See [data-explorer.md](data-explorer.md).

### Data Explorer Extensions — `extensions_explorer.render_extensions_explorer`

Selectbox of all extension names (from `pn.get_extensions_names()`, cached via
`@st.cache_data`). Shows `network.get_extensions(extension)`. When the selected
extension is listed in `state.EDITABLE_EXTENSIONS`, the rows are rendered in a
`st.data_editor` with the editable columns unlocked and the index / index-like
columns read-only; an **Apply N changes** button calls `update_extension`,
which groups rows by their non-null column set and dispatches one
`raw.update_extensions(name, subset)` per group through the worker thread.
Extensions omitted from `EDITABLE_EXTENSIONS` stay in the read-only
`st.dataframe` view. Download-as-CSV button included in both cases.

Non-editable extensions and why:

| Extension | Reason |
|---|---|
| `substationPosition` | `latitude` and `longitude` are not modifiable on the Java side — `update_extensions` raises "Series 'latitude' is not modifiable". Remove + recreate via the Data Explorer Components tab instead. |
| `position` | `order`, `feeder_name`, and `direction` are all not modifiable. Same workaround (remove + recreate). |
| `slackTerminal` | `update_extensions` rejects it with "id is missing": its index is `voltage_level_id` but the updater expects a differently-shaped key. Use remove + recreate. |
| `secondaryVoltageControl` | Network-level, two-dataframe (zones + units) shape that pypowsybl only supports as a full replace via `create_extensions`; pypowsybl 1.14 also has no read-back adapter, so there is nothing to show in the table view. Create / replace via the dedicated form in the Data Explorer Components tab (Voltage Levels). |

### Reactive Capability Curves — `reactive_curves.render_reactive_curves`

Loads `network.get_reactive_capability_curve_points()` and
`network.get_generators(all_attributes=True)`. Keeps generators with either
curve points or finite min/max reactive limits. Renders a Plotly filled polygon
for the capability boundary plus:
- red X marker for the operating point `(−p, −q)` (sign convention: pypowsybl
  stores generation as negative)
- green diamond for `(target_p, target_q)`

VL filter and generic Generator filters from `filters.py` are available.

### Pmax Visualization — `pmax_visualization.render_pmax_visualization`

Per-line steady-state transmission limit `Pmax = V₁·V₂/X` plus the
operating ratio `P/Pmax = sin(δ)` and its margin. Shows a sortable
summary table (colored by safe / caution / warning bands at 60 % /
80 % of Pmax) and a Plotly P-δ characteristic for the selected line.
Works as soon as bus voltages are available (loaded from the XIIDM
file or solved by a load flow); `p_actual` stays `0` until an AC load
flow has run.

See [pmax-visualization.md](pmax-visualization.md) for the formulas
and full column reference.

### Voltage Analysis — `voltage_analysis.render_voltage_analysis`

Bus voltages grouped by nominal level, a **geographical voltage-deviation
map** (Leaflet — separate renderer from the main Network Map, see
[voltage-analysis.md](voltage-analysis.md)), and reactive-compensation
tables (shunts, SVCs). No `selected_vl` input — the voltage map has
its own nominal-voltage filter because the pu color scale only makes
sense within one voltage class.

### Operational Limits — `operational_limits.render_operational_limits`

Two sections:
1. **Most loaded elements** — `_compute_loading` joins permanent current limits
   against actual `i1`/`i2` from `get_lines` / `get_2_windings_transformers`.
   Color-codes rows: red ≥ 100%, orange ≥ 80%. Shows "No loading data available
   (run a load flow first)" when currents are all zero/NaN.
2. **Element detail** — bar chart of limit values by acceptable_duration per side,
   with horizontal dashed lines for actual current. Raw limits table below.

## Sidebar components — `components.py`

`vl_selector(network)` — text filter + selectbox over all voltage levels. Writes
`st.session_state.selected_vl`. Returns the selected VL id.

`render_svg(svg_string, height)` — thin wrapper around `st.components.v1.html`.
(Kept for ad-hoc use; the NAD and SLD tabs now use their dedicated
custom components instead.)

## Session-state keys set by the sidebar / app.py

| Key | Content |
|---|---|
| `network` | `NetworkProxy` wrapping the loaded network |
| `selected_vl` | Currently selected voltage level id (str or None) |
| `_last_file` | Filename of last uploaded file (prevents reload on rerun) |
| `nad_depth` | NAD depth (set by the depth slider, key `nad_depth_slider`) |
