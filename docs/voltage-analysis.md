# Voltage Analysis tab

**File:** `iidm_viewer/voltage_analysis.py`
**Entry point:** `render_voltage_analysis(network)`

The tab bundles three sections:

1. **Bus voltages by nominal level** — `_bus_voltages` + `_render_voltage_section`
2. **Geographical voltage map** — delegated to `voltage_map.render_voltage_map` (see [below](#geographical-voltage-map---voltage_mappy))
3. **Reactive compensation** — shunt compensators (`_shunt_compensation`) and static VAR compensators (`_svc_compensation`)

All pypowsybl access goes through `NetworkProxy` methods; no raw
`pypowsybl` import is reachable from here.

## Bus voltages — `_bus_voltages(network)`

Joins `get_buses(all_attributes=True)` with
`get_voltage_levels(attributes=["nominal_v"])` and computes
`v_pu = v_mag / nominal_v`. When no load flow has run, `v_mag` and
`v_pu` are NaN and the tab shows "Voltage magnitudes are not
available — run a load flow first." — the summary table remains
populated with bus counts per nominal-voltage class.

Columns: `bus_id`, `voltage_level_id`, `nominal_v`, `v_mag`, `v_pu`.

The per-VL detail table below the summary is filtered by a nominal-voltage
selectbox and shades rows red when `v_pu` is outside the
`[lo, hi]` thresholds set by two number inputs (default `0.95` / `1.05`
pu).

## Reactive compensation

### `_shunt_compensation(network)`

One row per shunt compensator.

| Column | Meaning |
|---|---|
| `current_q_mvar`   | `q` from the load flow if present, else `b × nominal_v²` (estimate) |
| `available_q_mvar` | `(max_section_count − section_count) × (b / section_count) × nominal_v²`, plus the current contribution of disconnected units |
| `total_q_mvar`     | `max_section_count × (b / section_count) × nominal_v²` (NaN if `section_count == 0`) |

`b_per_section` is not exposed on retrieval in pypowsybl 1.14, so the
per-section susceptance is derived by dividing the total `b` by
`section_count`.

`has_q` is computed **per row** rather than network-wide, so a shunt
whose `q` is still NaN after a partial LF (e.g. newly added) is
classified correctly instead of being forced into the estimated branch.

### `_svc_compensation(network)`

One row per static VAR compensator.

| Column | Meaning |
|---|---|
| `current_q_mvar` | `q` from the load flow; forced to `0` when `regulation_mode == "OFF"`; NaN when no LF has run |
| `q_min_mvar` | `b_min × nominal_v²` |
| `q_max_mvar` | `b_max × nominal_v²` |

## Caching and load-flow invalidation

This module is stateless w.r.t. session state — every render re-reads
the network. Load-flow availability is detected per DataFrame
(`buses["v_mag"].notna().any()`, `svcs["current_q_mvar"].notna().any()`,
`shunts["q"].notna().any()`); there is no network-wide `has_lf` flag
to keep in sync.

The only cached piece is the voltage-map extraction
(`st.session_state["_voltage_map_cache"]`) — see
[voltage_map.py](#geographical-voltage-map---voltage_mappy) below.

---

# Geographical voltage map — `voltage_map.py`

**Entry point:** `voltage_map.render_voltage_map(network)`
**Called from:** `voltage_analysis.render_voltage_analysis`

## What it shows

A Leaflet map where each voltage level with a known substation
coordinate is a colored marker at that coordinate. Color encodes the
per-unit voltage deviation from nominal via a diverging
**blue-white-red** scale (blue = under-voltage, red = over-voltage,
pale yellow at nominal).

The map starts centered on France (`lat 46.6, lon 2.5, zoom 6`) —
same default as the pre-`2dac287` Leaflet renderer.

## Why Leaflet and not the main Network Map stack

The **Network Map** tab (`network_map.py`) uses
`@powsybl/network-map-layers` on top of MapLibre + deck.gl. That
library colors substations by **nominal voltage** via
`getNominalVoltageColor` — the palette is fixed and there is no hook
to color a voltage level by a scalar such as `v_pu`. Voltage-deviation
shading is not something the library offers.

The voltage map therefore falls back to the simple Leaflet-in-an-iframe
approach that used to power the main map before commit `81b459e`
("Replace Leaflet map with @powsybl/network-map-layers"). The
scaffolding was recovered from `git show 2dac287:iidm_viewer/network_map.py`
and adapted for per-VL scalar coloring.

## Controls

| Widget | Key | Purpose |
|---|---|---|
| Nominal voltage filter | `va_map_nom_select` | `"All nominal voltages"` (default) or a single class (`400 kV`, `225 kV`, …). Restricts the map to VLs of that nominal voltage so the pu scale is comparable across the displayed markers. |
| View | `va_map_mode` | Radio: `"Icons per substation"` (one colored dot per VL) vs `"Continuous gradient"` (large translucent circles blending into a heatmap, with a small dot per VL on top for the tooltip). |
| Full-scale ± pu | `va_map_vrange` | Deviation magnitude at which the red / blue channel saturates. Defaults to `0.05`. |

## Transport-network filter

```python
TRANSPORT_NOMINAL_V_THRESHOLD = 63.0  # kV
```

VLs with `nominal_v < 63 kV` are dropped before the selectbox is built.
Distribution-voltage substations would otherwise flood the map and make
the selectbox unusable. Raise or lower the constant if another deployment
needs a different cutoff.

## Data extraction — `_extract_voltage_map_data(network)`

Runs on the pypowsybl worker thread via `run()`. Returns `None` when
the network has no `substationPosition` extension or when the
extension has no valid coordinates (so the caller can render a
friendly info message).

The returned dict has two keys:

| Key | Content |
|---|---|
| `records` | list[dict], one per voltage level that has a substation coordinate |
| `has_lf`  | `True` when any bus in any VL has a finite `v_mag` (a load flow has run on this network) |

Each record:

```python
{
    "vl_id":         str,    # voltage level id
    "substation_id": str,
    "nominal_v":     float,  # kV
    "v_mag_mean":    float | None,  # kV, mean over buses in the VL
    "v_mag_min":     float | None,
    "v_mag_max":     float | None,
    "bus_count":     int,
    "lat":           float,
    "lon":           float,
}
```

Aggregation is `get_buses(all_attributes=True)` grouped by
`voltage_level_id`. `v_pu` is computed later in
`_prepare_display_records` as `v_mag_mean / nominal_v`; this keeps the
cache payload in raw-engineering units so different full-scale sliders
can be applied without re-running extraction.

`_nan_to_none` coerces pandas NaN / non-numeric cells to Python `None`
so the payload round-trips through `json.dumps` for the Leaflet iframe.

## Caching

`_get_cached_voltage_map_data` stores the extraction result in
`st.session_state["_voltage_map_cache"]`. Like the Network Map cache,
this is a single dict keyed only by session — it should be popped when
a new file is uploaded or a load flow changes bus voltages. At the
moment `state.load_network` / `state.run_loadflow` do not clear this
key; they should if the value ever becomes stale in practice. (The
Network Map has the same limitation — see
[network-map.md § Caching](network-map.md#caching).)

## Rendering

`render_voltage_map`:

1. Fetches the cached extraction.
2. Filters to `nominal_v ≥ TRANSPORT_NOMINAL_V_THRESHOLD`.
3. Emits an info message if the filter leaves nothing, or if no
   load-flow voltages are present.
4. Builds the three Streamlit controls (nominal-voltage filter,
   icons/gradient, full-scale pu).
5. Calls `_prepare_display_records(records, sel_nom, min_nominal)` to
   apply the nominal-voltage filter and compute `v_pu`.
6. Builds a `DivergingColorScale(center=1.0, range=v_range,
   mid=pale-yellow, low=blue, high=red)` and hands the records to the
   shared `leaflet_scalar_map.render_scalar_map`, which owns the JS
   template and iframe injection.

See [the shared renderer section](#shared-scalar-on-substation-renderer---leaflet_scalar_mappy)
below for the Leaflet JS contract.

---

# Shared scalar-on-substation renderer — `leaflet_scalar_map.py`

**Entry points:** `DivergingColorScale`, `get_substation_positions`,
`render_scalar_map`, `default_legend_stops`.
**Callers:** `voltage_map.py` (voltage deviation per VL, centered at
1.0 pu) and `injection_map.py` (net active/reactive power per
substation, centered at 0 MW). Both produce the same `records` shape
and differ only in color anchors, legend stops, and tooltips.

## Record shape

```python
{
    "id":      str,                # stable id used for debugging only
    "lat":     float,
    "lon":     float,
    "value":   float | None,       # None renders as a grey "no data" dot
    "tooltip": str,                # optional HTML, shown on marker hover
    "radius":  float,              # optional; overrides default in icons mode
}
```

`tooltip` is raw HTML; the caller is responsible for escaping it.
`radius` is ignored in gradient mode (the large translucent circles
have a fixed `gradient_radius_m`; the tiny dots on top use a fixed
3 px radius).

## Color model — `DivergingColorScale`

```python
@dataclass(frozen=True)
class DivergingColorScale:
    center: float                      # "neutral" value (1.0 pu / 0 MW)
    range: float                       # deviation that saturates color
    mid_rgb: tuple[int, int, int]      # color at center
    low_rgb: tuple[int, int, int]      # color at center - range
    high_rgb: tuple[int, int, int]     # color at center + range
```

Color math runs client-side in the iframe:

```js
t = clamp((value - scale.center) / scale.range, -1, 1);
target = t < 0 ? scale.lo : scale.hi;
color  = lerp(scale.mid, target, |t|);
```

`null` / `undefined` / `NaN` values render as `rgba(160,160,160,0.35)`
("no data"). The legend includes a grey swatch for that case.

## `render_scalar_map(records, *, …)`

Options:

| Kwarg | Default | Purpose |
|---|---|---|
| `mode` | — | `"icons"` (one marker per record) or `"gradient"` (wide blended circles + small dots) |
| `color_scale` | — | `DivergingColorScale` instance |
| `legend_title` | — | bold header in the legend box |
| `legend_subtitle` | `""` | smaller grey text below the title |
| `legend_stops` | `default_legend_stops(color_scale)` | list of `(value, label)` tuples; five symmetrical stops by default |
| `height` | 620 | iframe pixel height |
| `center_latlon` | `(46.6, 2.5)` | initial view center — France; same default as the pre-`2dac287` map |
| `zoom` | 6 | initial Leaflet zoom |
| `gradient_radius_m` | 25 000 | blended-circle radius in gradient mode (meters) |
| `default_icon_radius` | 7.0 | px radius when the record has no `radius` override |

## Substation-position helper — `get_substation_positions(network)`

Runs on the pypowsybl worker, returns `{substation_id: (lat, lon)}`
for every row of the `substationPosition` extension that has
in-range coordinates. Returns `{}` when the extension is missing or
empty — callers typically do `if not positions: return` and surface a
friendly info message.

Both `voltage_map._extract_voltage_map_data` and
`injection_map._extract_injection_data` call it; they do their own
per-VL / per-substation aggregation on top and route everything
through `run()`.

## How to add a new scalar-on-substation map

1. Write a `_extract_X_data(network)` that calls
   `get_substation_positions` first and returns `None` if it is empty.
   Do all pypowsybl work inside a nested `_extract()` passed to
   `run()`.
2. Cache the result in `st.session_state["_X_map_cache"]` with a
   distinct key.
3. Build the records (`{"id", "lat", "lon", "value", "tooltip"}`) and
   a `DivergingColorScale` with appropriate anchors.
4. Build `legend_stops` via `default_legend_stops(scale, unit=...,
   signed=True/False)` or a custom list if the scale is not symmetric.
5. Call `render_scalar_map`.

See `injection_map.py` for a worked example.

## Migrating to the MapLibre GL / deck.gl stack (optional)

The Leaflet renderer was chosen because it fits in a one-shot
`st.components.v1.html` iframe with no build step — see
[§ Why Leaflet and not the main Network Map stack](#why-leaflet-and-not-the-main-network-map-stack).
If a future need justifies the extra complexity (tens of thousands
of markers, a real GPU heatmap, click-driven cross-tab navigation, or
a single unified map stack across the app), here is the migration
path.

### Target stack

Same as the **Network Map** tab: MapLibre GL basemap + deck.gl
layers, packaged as a Vite-built custom Streamlit component. The
powsybl library `@powsybl/network-map-layers` is built on top of
this stack but **does not expose a per-element scalar-color hook**
(it colors by fixed nominal-voltage palette via
`getNominalVoltageColor`). Two options:

1. **Direct deck.gl** — drop `@powsybl/network-map-layers` and render
   a `ScatterplotLayer` (+ optional `HeatmapLayer`) on top of a bare
   MapLibre GL basemap. Smaller dependency surface, full control over
   color. Recommended.
2. **Fork / extend `@powsybl/network-map-layers`** — add a
   `getSubstationColor` prop and upstream it. Keeps the stack aligned
   with Network Map but depends on a library change.

### Phased plan

**Phase 1 — new custom component, same Python API.**

- Scaffold `iidm_viewer/frontend/scalar_map_component/` by copying
  `map_component/` (Vite + TypeScript + the `dist/` output convention).
- In `src/main.ts`, initialise a `maplibre-gl` map with an OSM raster
  style (cheapest migration) or a vector style later; overlay a deck.gl
  `ScatterplotLayer` fed by the incoming `records` prop.
- Translate `DivergingColorScale` into a `getFillColor` callback. The
  math is the same `lerp(mid, target, |t|)` as today's JS template — move
  it into `src/color.ts` and keep it tested.
- Create `iidm_viewer/scalar_map_component.py` as a
  `declare_component("scalar_map", path=".../dist")` wrapper, mirroring
  `map_component.py`.
- Rewrite `leaflet_scalar_map.render_scalar_map` to call that
  component instead of formatting `_LEAFLET_HTML`. Keep the Python
  signature byte-for-byte identical so `voltage_map.py` and
  `injection_map.py` don't need to change.
- Rename the module to `scalar_map.py` once the Leaflet version is
  gone; keep a deprecation re-export during the transition.

**Phase 2 — genuine gradient mode.**

- Replace the overlapping translucent `L.circle` approximation with a
  deck.gl `HeatmapLayer` (GPU-accelerated, density-weighted) — use
  `getWeight = r.value` for signed weighting and a diverging
  `colorRange`. This is the single biggest visual/perf win and the
  main reason to migrate.

**Phase 3 — two-way interaction.**

- Wire `onClick` / `onHover` to `setComponentValue` with a payload
  like `{"type": "scalar-map-click", "id": substation_id, "ts": …}`
  — same protocol NAD / SLD use (see
  [tabs.md § NAD](tabs.md#network-area-diagram--diagramsrender_nad_tab)).
- On the Python side, `voltage_map.py` / `injection_map.py` can then
  write `selected_vl` or switch tabs, enabling e.g. "click a red
  substation → jump to the Single Line Diagram".

**Phase 4 — unify with Network Map.**

- Once the direct-deck.gl path is solid, consider folding the scalar
  layer into `map_component` so a single iframe shows the whole grid
  *and* a scalar overlay toggle. Requires the
  `@powsybl/network-map-layers` fork from option 2 above, or a two-layer
  composition inside `map_component/src/main.ts`.

### Costs to weigh before starting

- **Build system:** a second Vite/TS frontend in the repo, another
  `dist/` bundle shipped in the wheel, another `npm run build` step
  before PRs touch this code.
- **Cold start:** MapLibre GL + deck.gl bundles are ~1 MB gzipped; the
  iframe takes longer to first paint than the 50 KB Leaflet script.
- **Feature regressions to avoid:** today's Leaflet renderer serves
  HTML tooltips out of the box (`bindTooltip`). In deck.gl you wire
  `getTooltip` on the `Deck` instance and render Popups via MapLibre —
  make sure the HTML in `_build_tooltip` still renders correctly
  (tables, `<b>`, `<i>`).
- **Scale today:** the transport-network views show ≤ a few thousand
  substations. Leaflet is fine at that size — migrate only when a
  concrete need (perf, interaction, or a real heatmap) emerges.

Until then the simpler Leaflet path wins on maintenance cost.

## Tests

`tests/test_voltage_map.py`:

- `_nan_to_none` coercion (number / NaN / None / garbage)
- `_prepare_display_records` filtering (below-threshold drop,
  nominal-voltage selection, `v_pu` computation, `v_pu is None` when
  no LF voltage)
- `_extract_voltage_map_data` returns `None` for a blank network and
  for the four-substations factory (neither has `substationPosition`)
- IEEE14 end-to-end: records populate, geo coordinates are in range,
  payload is JSON-serializable, `has_lf` is a bool
- AppTest smoke: the Voltage Analysis tab renders without exception
  after uploading IEEE14

`tests/test_leaflet_scalar_map.py`:

- `DivergingColorScale` is a frozen dataclass
- `default_legend_stops` returns five symmetrical stops around the
  center, and respects `signed=True` (adds `+` / `-`)
- `get_substation_positions` returns `{}` for networks without the
  extension (blank, four-substations) and a populated dict for IEEE14
