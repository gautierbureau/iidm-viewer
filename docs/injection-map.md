# Injection Map tab

**File:** `iidm_viewer/injection_map.py`
**Entry point:** `render_injection_map(network)`
**Shared renderer:** [`leaflet_scalar_map`](voltage-analysis.md#shared-scalar-on-substation-renderer---leaflet_scalar_mappy)

## What it shows

One marker per transport-network substation, colored by **net
power injection into the grid**:

- **green** → net exporter (generation > load)
- **red** → net importer (load > generation)
- **pale yellow** → balanced (near 0)

Active power (MW) or reactive power (MVAr) is toggled with a radio
button. Marker size scales with the absolute net injection (square
root of `|value| / full_scale`, clamped to `[4 px, 18 px]`) so big
stations stand out without overwhelming the map.

## Sign convention

pypowsybl exposes every terminal in **load convention**: positive =
flowing *from bus into equipment*. So a running generator has
`terminal.p < 0` and an energised load has `terminal.p > 0`.

The UI uses **grid-injection convention** (positive = into the grid),
which is `-terminal.p` / `-terminal.q`. That is the sign applied in
`_grid_inj_series`:

| Source | Fallback when `p`/`q` is NaN | Flip fallback? |
|---|---|---|
| Generators (`p`, `q`)     | `target_p`, `target_q` | No — already injection-signed |
| Loads (`p`, `q`)          | `p0`, `q0`             | Yes — load-convention scheduled |

Disconnected terminals contribute 0 regardless of the realized /
scheduled value.

This lets the map show something useful even before a load flow has
run (using setpoints), and it shows realized flows afterwards.

## Transport-network filter

```python
TRANSPORT_NOMINAL_V_THRESHOLD = 63.0  # kV
```

Substations whose **highest** voltage level is below 63 kV are
dropped. Same threshold as the voltage map — distribution stations
would otherwise flood the transport view.

## Data extraction — `_extract_injection_data(network)`

Runs on the pypowsybl worker via `run()`. Calls
`leaflet_scalar_map.get_substation_positions` first and returns
`None` when the network has no `substationPosition` extension (the
tab then shows a friendly info message).

The returned dict:

| Key | Content |
|---|---|
| `records` | list[dict], one per substation with a known position |
| `has_lf_p` | `True` when any gen/load terminal `p` is populated |
| `has_lf_q` | `True` when any gen/load terminal `q` is populated |

Each record:

```python
{
    "substation_id":   str,
    "substation_name": str,              # falls back to id when empty
    "max_nominal_v":   float,            # kV; highest VL at this substation
    "nominal_v_set":   list[float],      # sorted desc, for tooltip
    "gen_p_mw":        float,            # sum of grid-injected P from generators
    "load_p_mw":       float,            # sum of grid-injected P from loads (≤ 0)
    "inj_p_mw":        float,            # gen_p_mw + load_p_mw (net)
    "gen_q_mvar":      float,
    "load_q_mvar":     float,
    "inj_q_mvar":      float,
    "gen_count":       int,
    "load_count":      int,
    "lat":             float,
    "lon":             float,
}
```

Generator / load tables are joined to voltage levels via
`voltage_level_id → substation_id`, then aggregated with
`pandas.DataFrame.groupby("substation_id").sum()`. Switched
compensation (shunts, SVCs) is **not** counted in this tab — it has
its own treatment in the Voltage Analysis tab.

## Caching

`_get_cached_injection_data` stores the result in
`st.session_state["_injection_map_cache"]`. Like the voltage-map and
network-map caches, it is not invalidated when a new file is uploaded
or a load flow completes — rerun / reload is enough today. The three
caches should eventually share a common invalidation hook (see
[network-map.md § Caching](network-map.md#caching)).

## Controls

| Widget | Key | Purpose |
|---|---|---|
| Metric | `im_metric` | Active power `P` (MW) or Reactive power `Q` (MVAr) |
| View | `im_mode` | `Icons per substation` vs `Continuous gradient` |
| Full-scale ± {unit} | `im_range_P` / `im_range_Q` | Value at which color saturates and marker hits max radius. Default picked from the 90th percentile of `|inj|` rounded to `1 / 2 / 5 × 10ⁿ`. |

When `has_lf_{p,q}` is false, a caption notes that the map reflects
scheduled setpoints, not realized flows.

## Tooltip layout

```
<b>SUB_ID</b> (Substation Name)
Nominal: 400 kV, 225 kV
<b>Net injection:</b> +420.0 MW  (Net exporter)
Generation: +500.0 MW (3 gen)
Load: -80.0 MW (2 load)
```

## Rendering

`render_injection_map`:

1. Fetches the cached extraction.
2. Filters substations with no VL ≥ 63 kV.
3. Builds the three controls.
4. Constructs a `DivergingColorScale(center=0.0, range=full_scale,
   mid=pale-yellow, low=red, high=green)`.
5. Builds the legend stops via `_inj_legend_stops` (`±full`,
   `±full/2`, `0` with explicit `+` / `-` signs).
6. Hands records, color scale, and legend to
   `leaflet_scalar_map.render_scalar_map`.

Below the map, a caption summarises `len(exporters)`,
`len(importers)`, total export, total import, and the net balance.

## Tests

`tests/test_injection_map.py`:

- `_grid_inj_series`: generator realized (`p → -p`) and fallback
  (`target_p` straight through); load realized (`p → -p`) and
  fallback (`p0 → -p0`); disconnected terminal → 0; empty DataFrame;
  missing columns
- `_filter_transport` drops substations with
  `max_nominal_v < TRANSPORT_NOMINAL_V_THRESHOLD`
- `_radius_for`: zero → `min_r`, full scale → `max_r`, half scale
  uses sqrt, negative equals positive, out-of-scale clamped, `None` /
  `NaN` → `min_r`
- `_suggest_full_scale`: empty → 500, rounded to `1/2/5 × 10ⁿ`,
  scales up with magnitude
- `_extract_injection_data` returns `None` for networks without the
  `substationPosition` extension (blank, four-substations)
- IEEE14 end-to-end: records populate, `inj_p = gen_p + load_p`,
  `has_lf_{p,q}` are bool
- AppTest smoke: the app with the Injection Map tab renders without
  exception after uploading IEEE14
