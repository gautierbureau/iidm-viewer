# Data Explorer Components tab

**File:** `iidm_viewer/data_explorer.py`  
**Entry point:** `render_data_explorer(network, selected_vl)`

## Render flow

1. Pop `_lf_status_message` from session state and display if present (see [loadflow.md](loadflow.md)).
2. Selectbox of component types from `COMPONENT_TYPES` (defined in `network_info.py`).
3. Optional VL filter checkbox (only for `VL_FILTERABLE` component types when a VL is selected).
4. Optional ID substring filter text input.
5. Fetch the dataframe via `network.<method>(all_attributes=True)`.
6. Enrich with joined columns (`nominal_v`, `country`, etc.) via `enrich_with_joins`.
7. Reorder priority columns for Generators and Loads (`PRIORITY_COLUMNS`).
8. Apply whitelist filters via `render_filters` (see [filters.md](filters.md)).
9. Apply ID substring filter.
10. If the component is editable: render `st.data_editor`, detect changes, show
    Apply and Apply+LF buttons.
11. If not editable: render `st.dataframe`.
12. Download-as-CSV button.

## Editable components — `EDITABLE_COMPONENTS`

Defined in `state.py`. Maps component label → `(update_method_name, [editable_cols])`.

```python
"Generators": ("update_generators", ["target_p", "target_v", "target_q",
                                      "voltage_regulator_on", "connected"])
"Loads":      ("update_loads",      ["p0", "q0", "connected"])
# ... and 8 more
```

Non-editable columns are disabled via `st.column_config.Column(disabled=True)`.

## Change detection — `_compute_changes`

Compares original and edited DataFrames on the editable columns only.
Returns a sparse DataFrame (rows = changed elements, columns = changed fields).
Cells that didn't change for a given row are set to `None` so the update
call doesn't overwrite them.

## Applying changes — `update_components`

In `state.py`. Groups changed rows by their non-null column set (one
`update_*` call per group). Runs on the worker thread via `run()`. Clears
`_vl_lookup_cache` afterward.

## Apply + Load Flow pattern

When "Apply N changes & Run Load Flow" is clicked:
1. `update_components` is called.
2. `run_loadflow` is called (see [loadflow.md](loadflow.md)).
3. The status `(text, is_success)` is stored in `st.session_state["_lf_status_message"]`.
4. `st.rerun()` is called to refresh the table with updated values.
5. On the next render, step 1 of the render flow above pops and displays the message.

This pattern is necessary because `st.rerun()` discards any `st.success/warning`
rendered in the same pass.

## Column priority — `PRIORITY_COLUMNS`

For Generators and Loads, certain columns are moved to sit right after `name`
in the displayed table so they're visible without scrolling:

```python
"Generators": ["target_p", "target_q", "target_v", "connected",
               "voltage_regulator_on", "p", "q", "regulated_element_id"]
"Loads":      ["p0", "q0", "connected", "p", "q"]
```
