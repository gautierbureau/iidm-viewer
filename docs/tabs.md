# UI Tabs

## Layout — `app.py`

```
sidebar:  file uploader → vl_selector → Run LF button + ⚙ params button
tabs:     Overview | Network Map | Network Area Diagram | Single Line Diagram
          | Data Explorer Components | Data Explorer Extensions
          | Reactive Capability Curves | Operational Limits
```

Each tab has a dedicated render function. `app.py` passes `(network, selected_vl)`
to most of them; `network` is always a `NetworkProxy`.

## Tab inventory

### Overview — `network_info.render_overview`

Displays `st.metric` tiles for network metadata (id, name, format, case_date) and
element counts from `COMPONENT_TYPES`. Counts are fetched with `network.<method>()`.

`COMPONENT_TYPES` is the canonical label→method registry shared with `data_explorer.py`.

### Network Map — `network_map.render_network_map`

Renders a Leaflet map inside `st.components.v1.html`. Only works when the network
has a `substationPosition` extension; otherwise shows an info message.

See [network-map.md](network-map.md) for details.

### Network Area Diagram — `diagrams.render_nad_tab`

Controls: depth slider (0–10), "Enable click-to-select" checkbox.

Calls `network.get_network_area_diagram(voltage_level_ids=[selected_vl], depth=depth, ...)`.
The result is a `NadResult`-like proxy with `.svg` and `.metadata` attributes.

When interactive mode is on, `nad_interactive.make_interactive_nad_svg` injects
`<style>` + `<script>` into the SVG before rendering. Clicking a VL node rewrites
`?selected_vl=VLx` on the top-level URL, which Streamlit picks up via
`st.query_params` on the next rerun (`app.py` lines 20-22).

Note: accessing `.svg` and `.metadata` from the proxy issues two separate `run()`
calls. Do not wrap these inside another `run()` — that deadlocks the executor.

### Single Line Diagram — `diagrams.render_sld_tab`

Calls `network.get_single_line_diagram(selected_vl, parameters=SldParameters(...))`.
Result `.svg` is rendered via `render_svg`.

### Data Explorer Components — `data_explorer.render_data_explorer`

See [data-explorer.md](data-explorer.md).

### Data Explorer Extensions — `extensions_explorer.render_extensions_explorer`

Selectbox of all extension names (from `pn.get_extensions_names()`, cached via
`@st.cache_data`). Shows `network.get_extensions(extension)` as a dataframe.
Download-as-CSV button included.

### Reactive Capability Curves — `reactive_curves.render_reactive_curves`

Loads `network.get_reactive_capability_curve_points()` and
`network.get_generators(all_attributes=True)`. Keeps generators with either
curve points or finite min/max reactive limits. Renders a Plotly filled polygon
for the capability boundary plus:
- red X marker for the operating point `(−p, −q)` (sign convention: pypowsybl
  stores generation as negative)
- green diamond for `(target_p, target_q)`

VL filter and generic Generator filters from `filters.py` are available.

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

## Session-state keys set by the sidebar / app.py

| Key | Content |
|---|---|
| `network` | `NetworkProxy` wrapping the loaded network |
| `selected_vl` | Currently selected voltage level id (str or None) |
| `_last_file` | Filename of last uploaded file (prevents reload on rerun) |
| `nad_depth` | NAD depth (set by the depth slider, key `nad_depth_slider`) |
