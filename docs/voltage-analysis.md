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
4. Builds the three Streamlit controls.
5. Calls `_prepare_display_records(records, sel_nom, min_nominal)` to
   apply the nominal-voltage filter and compute `v_pu`.
6. Formats `_LEAFLET_HTML` with `records`, `mode`, `v_range`, and
   `gradient_radius_m`, and hands it to `st.components.v1.html`.

### Leaflet JS contract

The iframe script reads four template variables:

| JS var | Python side | Role |
|---|---|---|
| `records` | `display` list (after filters) | one marker per entry |
| `mode`    | `"icons"` or `"gradient"` | selects the rendering branch |
| `vRange`  | `v_range` slider value | color-saturation range (±pu) |
| `gradientRadiusMeters` | heuristic on `sel_nom` | radius of the blended circles in gradient mode (25 km for ≥200 kV / all-voltages, 12 km otherwise) |

Color computation is entirely client-side:

```js
function divergingColor(v_pu) {
  var t = clamp((v_pu - 1) / vRange, -1, 1);
  var mid = [255, 255, 224];  // pale yellow at 1.0 pu
  var lo  = [27,  74, 199];   // blue  at 1 - vRange
  var hi  = [199, 27,  27];   // red   at 1 + vRange
  var target = t < 0 ? lo : hi;
  return lerp(mid, target, Math.abs(t));
}
```

The legend is a five-stop sample of `divergingColor` at
`{-vRange, -vRange/2, 0, +vRange/2, +vRange}` plus a grey "no data"
swatch for VLs whose `v_pu` is `null`.

## Reusing the Leaflet scaffolding for other scalar-on-substation maps

`voltage_map.py` is effectively a small generic **"scalar value per
substation on top of Leaflet"** renderer with voltage-specific
controls bolted on. To use it for another scalar (injection,
short-circuit power, loading %, etc.) the refactoring path is:

1. **Split the JS template.** `_LEAFLET_HTML` is parameterised only on
   `records`, `mode`, `vRange`, `gradientRadiusMeters`. Move it into a
   shared module (e.g. `iidm_viewer/leaflet_scalar_map.py`) that
   exposes a function like:

   ```python
   def render_scalar_map(
       records,            # list of {id, lat, lon, value, label, ...}
       *, mode="icons",
       color_fn="diverging",  # "diverging" | "sequential"
       center=(46.6, 2.5), zoom=6,
       legend_title="Value",
       unit="",
       value_range,           # (center, span) for diverging, (min, max) for sequential
       tooltip_builder=None,  # optional lambda record -> str
   ):
       ...
   ```

2. **Parameterise the color function.** Today there's one hard-coded
   `divergingColor` in JS. A shared renderer should accept a
   color-map name (diverging blue-white-red, sequential viridis,
   categorical) and the corresponding color stops, and pick the JS
   branch accordingly.

3. **Keep the cache shape generic.** The current cache key is
   `_voltage_map_cache`; another tab should use its own key
   (`_loading_map_cache`, `_sc_power_map_cache`) so they don't collide.
   Extraction must still run on the worker thread via `run()`.

4. **Per-tab controls stay with the tab.** Keep the nominal-voltage
   and full-scale widgets in `voltage_map.py`; a loading-percentage
   map would have its own nominal-voltage filter + color-range widget
   and would pre-compute values before calling the shared renderer.

5. **Substation positions are the only common extraction.** The
   `substationPosition` extension lookup and `get_substations()` merge
   in `_extract_voltage_map_data` is reusable verbatim. Extract it
   into `iidm_viewer/geo.py` (`get_substation_positions(network) ->
   dict[str, (lat, lon)]`) when a second caller appears.

Until a second scalar-on-substation view exists, the inlined approach
here is the simplest thing that works — the refactor should be driven
by a concrete second use-case.

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
