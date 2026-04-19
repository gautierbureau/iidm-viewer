# Network Map tab

**File:** `iidm_viewer/network_map.py`
**Entry point:** `render_network_map(network, selected_vl)`

## What it shows

An interactive geographical map rendered by a custom Streamlit
component (`iidm_viewer/frontend/map_component/`) built on
[MapLibre GL JS](https://maplibre.org/) + [deck.gl](https://deck.gl/)
+ [`@powsybl/network-map-layers`](https://www.npmjs.com/package/@powsybl/network-map-layers).

The library ships two deck.gl layers purpose-built for Powsybl:

- **`SubstationLayer`** — one concentric ring per voltage level,
  coloured by nominal voltage via
  `getNominalVoltageColor(nominalVoltage)`. Rings size-sorted so the
  highest-voltage ring sits on the outside.
- **`LineLayer`** — lines and 2-winding transformers as polylines
  between the substations they connect, coloured by the higher of the
  two end voltages. Parallel lines are automatically fanned out;
  disconnected lines render in the supplied `disconnectedLineColor`.

Hover tooltips (id, name, P1, I1 for lines; id + VL breakdown for
substations) are added by `src/main.ts` on top of the layers — the
library itself is rendering-only.

## Basemap

Raw **OpenStreetMap raster tiles** via MapLibre's minimal style
(`tile.openstreetmap.org/{z}/{x}/{y}.png` across the three round-robin
subdomains). No API key. Same tile source as the previous Leaflet
implementation — only the renderer changed.

## Data extraction — `_extract_map_data(network)`

Runs on the pypowsybl worker thread via `run()`. Requires the
`substationPosition` extension; returns `None` otherwise.

Returns a 3-tuple shaped to match `@powsybl/network-map-layers`' typed
models:

| Name | Matches | Fields |
|---|---|---|
| `substations` | `MapSubstation[]` | `id`, `name`, `voltageLevels: [{id, substationId, substationName, nominalV}]` |
| `substation_positions` | `GeoDataSubstation[]` | `id`, `coordinate: {lon, lat}` |
| `lines` | `MapLine[]` | `id`, `name`, `voltageLevelId1/2`, `terminal1/2Connected`, `p1`, `p2`, `i1`, `i2` (lines **and** 2-winding transformers) |

Invalid lat/lon are filtered out; substations without coordinates are
dropped (the layer can't place them anyway). Pandas cells are coerced
to native Python `bool`/`float` so the values survive Streamlit's JSON
serialisation.

## Caching

`_get_cached_map_data` stores the extraction result in
`st.session_state["_map_data_cache"]`. This is cleared when a new
file is uploaded (`state.load_network` pops it). It is **not**
cleared by `run_loadflow` — the map displays `p1`/`i1` from the
network, but that data comes from the cache produced at upload time.
If post-LF flows must be reflected on the map, clear the cache key or
re-extract.

## Frontend — `render_interactive_map(substations, substation_positions, lines, height, key)`

See
[`iidm_viewer/frontend/map_component/README.md`](../iidm_viewer/frontend/map_component/README.md)
for the build workflow. The bundle (~1.9 MB raw, ~520 KB gzip) is
committed under `dist/` so `pip install` works without Node; CI
rebuilds it on every push.

`src/main.ts` (~220 lines):

1. Builds `MapEquipments` and `GeoData` from the three lists.
2. Augments each line with `equipmentType: EQUIPMENT_TYPES.LINE` to
   match `MapLineWithType[]` (what `LineLayer` expects in its `data`
   prop).
3. Creates a `maplibregl.Map` with the OSM raster style, computes a
   bounding box from the substation positions, and calls `fitBounds`.
4. Attaches a `@deck.gl/mapbox` `MapboxOverlay` containing
   `SubstationLayer` + `LineLayer` to the map as a MapLibre control.
5. Builds a DOM legend (`.map-legend`) from
   `network.getNominalVoltages()` — cheaper than a deck.gl layer and
   matches the visual style of the previous Leaflet implementation.

No clicks are currently forwarded back to Python; tooltips and
pan/zoom/drag are entirely browser-side. The parked plan to add
substation click-to-navigate (and have it land on the **Single Line
Diagram** tab rather than just updating the sidebar) is in
[future-interactive-viewer.md § "Follow-up: cross-tab navigation"](future-interactive-viewer.md#follow-up-cross-tab-navigation-nad--map-click--sld-tab)
— the wiring mirrors NAD / SLD: `pickable: true` + `onClick` on
`SubstationLayer`, posting `{type: "map-vl-click", vl, ts}` via
`setComponentValue`.

## Nominal-voltage colour scheme

Delegated to `getNominalVoltageColor` from
`@powsybl/network-map-layers`. The library owns the palette so the
map, NAD, and SLD stay visually consistent.
