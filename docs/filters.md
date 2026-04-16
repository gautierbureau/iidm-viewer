# Filter infrastructure

**File:** `iidm_viewer/filters.py`

## `FILTERS` — column whitelist per component

Each component type declares which columns get filter widgets. Columns not in
the whitelist are never exposed as filters.

```python
FILTERS = {
    "Generators": ["nominal_v", "country", "energy_source", "min_p", "max_p",
                   "target_p", "voltage_regulator_on", "connected"],
    "Lines":      ["nominal_v1", "nominal_v2", "p1", "connected1", "connected2"],
    # ...
}
```

`nominal_v`, `country`, `nominal_v1/2`, `country1/2` are not native columns on
most component DataFrames — they are added by `enrich_with_joins`.

## `build_vl_lookup(network)` → DataFrame

Fetches `get_voltage_levels` and `get_substations`, merges on `substation_id`,
and caches the result in `st.session_state["_vl_lookup_cache"]` keyed by
`id(network)`. Cleared by `run_loadflow` and `update_components`.

Returns a DataFrame with columns: `id`, `substation_id`, `nominal_v`, `country`.

## `enrich_with_joins(df, vl_lookup)` → DataFrame

Left-joins voltage-level and substation derived columns onto any component
DataFrame. Handles three join patterns:

| Source column | Adds columns |
|---|---|
| `substation_id` | `country` |
| `voltage_level_id` | `nominal_v`, `country` |
| `voltage_level1_id` / `voltage_level2_id` | `nominal_v1`/`nominal_v2`, `country1`/`country2` |

Preserves the original index name.

## `render_filters(df, columns, key_prefix)` → DataFrame

For each column in the whitelist that is present in `df`, renders a widget
inside an `st.expander("Filters")`:

| Column dtype | Widget |
|---|---|
| bool | `st.selectbox` with Any / True / False |
| numeric | `st.slider` range; skipped if constant or all NaN |
| other (low-cardinality, ≤ 30 unique values) | `st.multiselect` |
| other (high-cardinality, > 30 unique values) | skipped |

Returns the filtered DataFrame. Does not mutate the input.
