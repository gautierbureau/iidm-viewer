# Network Map tab

**File:** `iidm_viewer/network_map.py`  
**Entry point:** `render_network_map(network, selected_vl)`

## What it shows

An interactive Leaflet map (embedded via `st.components.v1.html`) displaying:
- Substations as concentric circle markers, one ring per voltage level, colored by
  nominal voltage (matches pypowsybl conventions: 380 kV red, 225 kV green, etc.)
- Lines and 2-winding transformers as polylines colored by the higher nominal voltage
  of their two ends; dashed and faded when disconnected
- Tooltip on each line/transformer: id, name, P1 (MW), I1 (A)
- Tooltip on each substation: id, name, list of VLs with colors and nominal voltages
- Selected VL highlighted with a black border on its marker ring
- Legend of voltage levels present in the network (top-right)

## Data source — `_extract_map_data(network)`

Runs entirely on the worker thread via `run()`. Requires the `substationPosition`
extension to be present in the network; returns empty lists otherwise.

Extracts:
- `spos` — list of `{id, coordinate: {lat, lon}}` for each substation
- `smap` — list of `{id, name, voltageLevels: [{id, name, substationId, nominalV}]}` 
- `lmap` — records from `get_lines` + `get_2_windings_transformers` with fields:
  `id, name, voltageLevelId1, voltageLevelId2, terminal1Connected, terminal2Connected,
  p1, p2, i1, i2`
- `vl_coords` — `{vl_id: {lat, lon}}` derived from the substation positions
- `vl_nv` — `{vl_id: nominal_v}`

## Caching

`_get_cached_map_data` stores the extraction result in
`st.session_state["_map_data_cache"]`. This is cleared when a new file is
uploaded (`state.load_network` pops it). It is **not** cleared by
`run_loadflow` — the map does display p1/i1 from the network but that data
comes from the `lmap` extracted at upload time, not re-extracted after LF.
If post-LF line flows need to be reflected on the map, clear the cache key
or re-extract.

## Leaflet HTML template — `_LEAFLET_HTML`

A self-contained HTML page with inline JS. Template variables are JSON-encoded
Python values injected via `.format()`. Leaflet and its CSS are loaded from
`unpkg.com` at runtime — requires internet access in the browser.

A `setTimeout(map.invalidateSize, 200)` call is needed because Streamlit renders
the component in an iframe whose size stabilises after the initial render.

## Nominal voltage color scheme

```
≥ 380 kV → #ff0000  (red)
≥ 225 kV → #228b22  (forest green)
≥ 150 kV → #6495ed  (cornflower blue)
≥  90 kV → #ff8c00  (dark orange)
≥  63 kV → #a020f0  (purple)
≥  42 kV → #ff69b4  (hot pink)
<  42 kV → #6b8e23  (olive)
```
