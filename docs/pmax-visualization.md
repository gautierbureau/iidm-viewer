# Pmax Visualization tab

**File:** `iidm_viewer/pmax_visualization.py`
**Entry point:** `render_pmax_visualization(network, selected_vl)`

## What it shows

Per-line steady-state **transmissible power limit** and how close each
line is operating to it.

The underlying relation is the classical lossless-line power–angle
characteristic:

```
Pmax  = V₁ · V₂ / X            (V in kV, X in Ω → Pmax in MW)
P     = Pmax · sin(δ)          δ = voltage angle difference across the line
```

Two views stacked in the tab:

1. **Summary table** — every line that has a non-zero reactance and
   valid bus voltages on both ends, sorted by ascending margin so the
   most-loaded lines float to the top. Cells for `P/Pmax` and
   `Margin (%)` are shaded using the same thresholds as the chart
   below.
2. **Per-line chart** — Plotly `P = Pmax · sin(δ)` curve from 0° to
   90°, with three translucent safety bands (green / orange / red),
   a dotted `Pmax` reference line, and a colored marker at the
   operating point `(δ, P)`.

## Data computation — `_compute_pmax_data(network)`

Runs through the `NetworkProxy` (no direct pypowsybl calls). Steps:

1. `network.get_lines(all_attributes=True)` — returns early if empty.
2. `network.get_buses(all_attributes=True)` — returns early if empty.
3. `build_vl_lookup(network)` + `enrich_with_joins(lines, vl_lookup)`
   from `filters.py` adds `bus1_id` / `bus2_id` / `voltage_level*_id` to
   every line row.
4. For each line, drop rows where:
   - `|x| < 1e-6 Ω` — division by zero (or near-zero X).
   - `bus1_id` or `bus2_id` is missing or absent from the bus index —
     e.g. a disconnected end.
   - either `v_mag` is non-positive or NaN.
5. Compute:
   - `pmax_mw = v1 · v2 / |x|`
   - `p_actual_mw = |p1|` (falls back to `0.0` when `p1` is NaN)
   - `p_pmax_ratio = p_actual / pmax`
   - `delta_deg = arcsin(ratio)` only when `0 ≤ ratio ≤ 1`, else NaN.
     The clamp matters: a spurious `p_actual > pmax` (e.g. caused by
     stale network data mixed with fresh LF results) would otherwise
     crash `np.arcsin`.
   - `margin_pct = (1 − ratio) · 100`

Returns a DataFrame indexed by `line_id`, sorted by `margin_pct`
ascending. Columns: `name, x_ohm, v1_kv, v2_kv, pmax_mw, p_actual_mw,
p_pmax_ratio, delta_deg, margin_pct, voltage_level1_id,
voltage_level2_id`.

### Without a load flow

IEEE14 (and most XIIDM fixtures) ship with stored bus voltages
(`v_mag > 0`) but **no line flows**, so the table populates — every
row has a finite `Pmax` — but every `p_actual_mw` is `0` and every
`p_pmax_ratio` is `0`. Running the AC load flow populates `p1` and
fills in the ratio / angle columns.

If both voltages and flows are missing (e.g. a freshly built blank
network), `_compute_pmax_data` returns an empty DataFrame and the tab
shows "No data available. Make sure a load flow has been run and the
network contains transmission lines."

## VL filter

When the sidebar `selected_vl` matches at least one end of at least
one line in the data set, the tab renders a checkbox "Only lines
connected to VL *id*". Default is unchecked (show all lines); checking
it narrows the summary table and the line selectbox to lines where
`voltage_level1_id == selected_vl` or `voltage_level2_id ==
selected_vl`. The checkbox is hidden when the VL is not involved in
any line, so there is never a "the filter emptied the table" dead-end.

## Color thresholds

Used identically in the table and the chart bands:

| Band | Range | Cell color | Chart band |
|---|---|---|---|
| Safe    | `P/Pmax < 60%`   (margin > 40%) | default | green   `rgba(0,180,0,0.08)` |
| Caution | `P/Pmax 60–80%`  (margin 20–40%) | orange | orange  `rgba(255,165,0,0.12)` |
| Warning | `P/Pmax ≥ 80%`   (margin ≤ 20%) | red    | red     `rgba(220,0,0,0.12)` |

The chart band positions are `arcsin(0)`, `arcsin(0.6)`, `arcsin(0.8)`,
`arcsin(1.0)` in degrees — the same thresholds expressed on the angle
axis.

Operating-point marker color follows the same rule — green / orange /
red depending on which band contains the ratio.

## Per-line chart — `_build_pangle_chart(line_id, row)`

Returns a `plotly.graph_objects.Figure` laid out as:

- `add_vrect` × 3 for the safe / caution / warning background bands.
- One `Scatter` trace for the `P = Pmax · sin(δ)` curve, sampled at
  270 points across `[0°, 90°]`.
- `add_hline` at `y = pmax` with a `Pmax = … MW` annotation.
- One `Scatter` marker at `(delta_deg, p_actual_mw)` when both are
  finite and `p_actual > 0`, plus a dashed `add_vline` at the same
  `delta_deg`. The marker is suppressed when `p_actual == 0` because
  `(0°, 0 MW)` is always on the curve and would imply "no-LF" and
  "truly zero flow" render identically.

Axes are pinned to `x ∈ [0, 90]` and `y ∈ [0, 1.15 · Pmax]` so the
visual room above Pmax is constant across lines.

## Metrics strip

Four `st.metric` tiles above the chart:

| Metric | Value | Notes |
|---|---|---|
| Pmax | `pmax_mw` (MW) | — |
| P actual | `p_actual_mw` (MW) | — |
| P/Pmax | percentage | `delta` shows the margin; `delta_color` inverted when `ratio ≥ 0.8` so red arrow == bad |
| δ operating | degrees | `"N/A"` when `delta_deg` is NaN |

## Caching

None. Every rerun recomputes the DataFrame; the work is a single
`get_lines` + `get_buses` round-trip through the worker thread and a
pure-pandas loop — measured well below a second on IEEE14 and
comfortable on RTE-sized networks.

If this ever becomes a bottleneck, cache on
`st.session_state["_pmax_cache"]` keyed by the `id(network._obj)` +
load-flow counter, mirroring what `voltage_map.py` does. Invalidation
would then need a hook in `state.run_loadflow`.

## Tests — `tests/test_pmax_visualization.py`

Covers:

- table populates without a load flow (voltages from the XIIDM file
  are enough for `Pmax`) but `p_actual == 0` everywhere;
- table populates after `run_loadflow`;
- required columns are present;
- `pmax_mw > 0` on every row;
- `p_pmax_ratio ∈ [0, 1]`, `delta_deg ∈ [0°, 90°]`;
- `margin_pct == (1 − ratio) × 100` to 1e-6;
- rows are sorted by `margin_pct` ascending (most-loaded first);
- `sin(delta_deg) ≈ p_pmax_ratio` to 1e-6 — the angle and ratio are
  consistent;
- IEEE14 produces at most 20 rows (its line count);
- `_build_pangle_chart` returns a Plotly figure with at least one
  trace.
