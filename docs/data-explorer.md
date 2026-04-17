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

## Component creation — `CREATABLE_COMPONENTS`

Defined in `state.py`. Maps component label → creation spec. Each spec
has:

- `bay_function` — the `pypowsybl.network.create_*_bay` entry point.
- `fields` — a list of typed field dicts driving the form
  (`name`, `label`, `kind` in {`text`, `float`, `int`, `bool`, `select`},
  `required`, `default`, optional `options`, `help`, `min_value`, `step`).
- `validate` (optional) — key into `_VALIDATORS` for extra business-rule
  checks (e.g. `max_p >= min_p`, `target_v > 0` when regulating).

Currently creatable: Generators, Loads, Batteries, Static VAR Compensators,
VSC Converter Stations, LCC Converter Stations. Each maps to its
matching `create_*_bay` helper in pypowsybl 1.14.

Shared locator fields (`position_order`, `direction`) live in
`LOCATOR_FIELDS` and are appended to every form. The `bus_or_busbar_section_id`
comes from a VL + busbar picker rendered above the `st.form`.

The Data Explorer inserts a collapsible "Create a new ..." expander at the
top of any creatable component's view
(`_render_create_component_form(network, component)`). Render flow:

1. Pick a **node-breaker voltage level** from
   `list_node_breaker_voltage_levels` (empty → info message, bus-breaker
   VLs skipped for v1).
2. Pick a **busbar section** within that VL (`list_busbar_sections`).
3. Fill component fields (rendered generically from the spec) + the
   locator fields.
4. `create_component_bay(network, component, fields)` runs
   `validate_create_fields` on the main thread, then dispatches
   `pypowsybl.network.<bay_function>(raw, df)` through the worker thread.
   In node-breaker VLs the helper allocates nodes and inserts a
   **disconnector + breaker** between the busbar section and the new
   injection, so the user never sees node numbers.
5. pypowsybl errors (unknown busbar, duplicate id, enum mismatches)
   propagate and surface via `st.error(...)`.

Adding another creatable injection type is one registry entry — no UI
change required. The generic form renderer (`_render_generic_field_grid`)
handles text/number/bool/select widgets from the spec; `create_component_bay`
already dispatches to any `create_*_bay` function.

## Branch creation — `CREATABLE_BRANCHES`

Lines and 2-Winding Transformers connect two voltage levels, so they use
a dedicated two-sided form (`_render_create_branch_form`) instead of the
single-side injection form.

The `CREATABLE_BRANCHES` registry (in `state.py`) mirrors
`CREATABLE_COMPONENTS` but adds a `same_substation` flag:

```python
"Lines":                  {"bay_function": "create_line_bays", ...,
                            "same_substation": False}
"2-Winding Transformers": {"bay_function": "create_2_windings_transformer_bays",
                            ..., "same_substation": True}
```

Side-specific locator fields (`bus_or_busbar_section_id_1/2`,
`position_order_1/2`, `direction_1/2`) are generated at render time via
`branch_side_locator_fields(side)` by suffixing the shared
`_BRANCH_SIDE_LOCATOR` template.

Render flow (for a node-breaker network):

1. Two **voltage level + busbar section** pickers side by side
   (`_render_side_picker` for sides 1 and 2). Bus-breaker VLs are
   skipped for v1, same as for injections.
2. Electrical fields (e.g. `r`, `x`, `g1/b1/g2/b2` for lines;
   `r`, `x`, `g`, `b`, `rated_u1`, `rated_u2`, optional `rated_s` for
   2WTs) rendered from the spec.
3. Per-side `position_order` + `direction` locator fields rendered
   under **Side 1** / **Side 2** headings.
4. `create_branch_bay(network, component, fields)` runs
   `validate_create_branch_fields` on the main thread, then dispatches
   `pypowsybl.network.<bay_function>(raw, df)` through the worker
   thread.

`validate_create_branch_fields` enforces required fields on both sides
and — for components with `same_substation=True` (i.e. 2WTs) — verifies
both picked busbar sections belong to the same substation (via
`_substations_of_bbs`), producing a friendly error before pypowsybl is
involved.

## Blank-network bootstrap

The sidebar ("Or start from a blank network" expander) calls
``state.create_empty_network(network_id)`` which routes through the worker
thread to ``pypowsybl.network.create_empty`` and installs the fresh
``NetworkProxy`` as ``session_state["network"]``. From there the user can
drive the full node-breaker build-up — Substation → Voltage Level (with
``topology_kind="NODE_BREAKER"``) → Busbar Section → injections / branches
— using the Data Explorer's creation forms without ever uploading a file.
Bus-breaker creation is deliberately not wired in yet; the forms still
restrict injection + branch creation to node-breaker VLs.

## Container creation — `CREATABLE_CONTAINERS`

Substations, Voltage Levels, and Busbar Sections don't have a ``_bay`` helper:
they are created directly via the plain ``create_<type>s`` methods on
``pypowsybl.network.Network``. Their registry (in `state.py`) mirrors
`CREATABLE_COMPONENTS` but each spec has a ``create_function`` key instead of
``bay_function``:

```python
"Substations":     {"create_function": "create_substations",     ...}
"Voltage Levels":  {"create_function": "create_voltage_levels",  ...}
"Busbar Sections": {"create_function": "create_busbar_sections", ...}
```

Form behaviour (`_render_create_container_form`):

- **Substations**: plain field grid — id, name, country (ISO code), TSO. No
  context fields.
- **Voltage Levels**: optional `substation_id` picker rendered above the
  form (empty network ⇒ the VL is created unattached). Field grid covers id,
  name, topology_kind, nominal_v, low/high voltage limits. ``0`` is the
  "unset" sentinel for the voltage limits (dropped before dispatch).
- **Busbar Sections**: `voltage_level_id` picker restricted to node-breaker
  VLs (same helper as the injection form). The `node` field's default is
  set to `next_free_node(network, vl_id)` — the first integer not already
  used by a busbar section or switch in that VL.

All dispatch runs through `create_container(network, component, fields)` on
the worker thread, with `_vl_lookup_cache` + `_map_data_cache` invalidated
afterwards so follow-up tabs reload with the new container.

## Column priority — `PRIORITY_COLUMNS`

For Generators and Loads, certain columns are moved to sit right after `name`
in the displayed table so they're visible without scrolling:

```python
"Generators": ["target_p", "target_q", "target_v", "connected",
               "voltage_regulator_on", "p", "q", "regulated_element_id"]
"Loads":      ["p0", "q0", "connected", "p", "q"]
```
