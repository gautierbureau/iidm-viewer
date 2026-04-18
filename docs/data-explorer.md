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
VSC Converter Stations, LCC Converter Stations, Shunt Compensators. Each
maps to its matching `create_*_bay` helper in pypowsybl 1.14.

Shunt compensators are a special case because pypowsybl's
`create_shunt_compensator_bay` takes **three** dataframes (main shunt,
linear model, non-linear model) rather than the single dataframe every
other injection uses. `_dispatch_shunt_bay` in `state.py` splits the flat
field dict into the shunt row + a linear-model row (columns
`g_per_section`, `b_per_section`, `max_section_count`). Only the
**LINEAR** model is exposed in the UI for v1.

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

## Tap changer creation — `CREATABLE_TAP_CHANGERS`

Ratio and phase tap changers attach to an *existing* 2-winding transformer
rather than to a busbar, so they don't use a `_bay` helper: they call
`raw.create_ratio_tap_changers(rtc_df, steps_df)` or
`raw.create_phase_tap_changers(ptc_df, steps_df)` on the network via the
worker thread.

The registry (`CREATABLE_TAP_CHANGERS` in `state.py`) lists the two kinds:

```python
"Ratio": {"create_method": "create_ratio_tap_changers",
          "main_fields": [tap, low_tap, oltc, regulating, target_v,
                          target_deadband, regulated_side],
          "step_columns": ["r", "x", "g", "b", "rho"]}
"Phase": {"create_method": "create_phase_tap_changers",
          "main_fields": [tap, low_tap, regulation_mode, regulating,
                          target_deadband, regulated_side],
          "step_columns": ["r", "x", "g", "b", "rho", "alpha"]}
```

Form (`_render_create_tap_changer_form`) appears alongside the
2-Winding Transformer creation form and renders only when at least one
2WT already exists. It shows a 2WT picker, a kind picker, a generic field
grid for the main tap-changer attributes, and an `st.data_editor` with
`num_rows="dynamic"` for the per-tap steps. `create_tap_changer(network,
kind, transformer_id, main_fields, steps)` builds the two dataframes and
dispatches them through the worker.

`validate_create_tap_changer_fields` enforces required main fields,
at-least-one step, and that the current `tap` sits within
`[low_tap, low_tap + len(steps))`. Ratio tap changers additionally need
`oltc=True` + `target_v > 0` when `regulating=True`.

## Coupling device — `create_coupling_device`

Switches tying two busbar sections inside the same voltage level are
created via `pn.create_coupling_device(network, bus_or_busbar_section_id_1,
bus_or_busbar_section_id_2, switch_prefix_id=...)`. In node-breaker VLs
pypowsybl inserts a closed breaker plus closed disconnectors on each
busbar section automatically; in bus-breaker VLs it inserts a plain
breaker.

Form (`_render_create_coupling_device_form`) appears when the component
type is **Switches** (coupling devices create switches under the hood,
so this is where the form logically belongs). It picks a node-breaker
VL, then two distinct busbar sections inside it, and accepts an optional
switch id prefix.

`create_coupling_device(network, bbs1, bbs2, switch_prefix)` validates
that both busbar sections exist, differ, and share the same voltage
level before dispatching to pypowsybl via the worker thread.

## HVDC line creation — `CREATABLE_HVDC_LINES`

HVDC lines connect two *existing* converter stations (VSC or LCC) rather
than two busbars, so they don't use a `_bay` helper. The form
(`_render_create_hvdc_line_form`, shown on the **HVDC Lines** component
view) picks the two endpoints from `list_converter_stations(network)`
and dispatches `network.create_hvdc_lines(df)` via the worker.

Fields: id, name, r, nominal_v, max_p, target_p, converters_mode
(`SIDE_1_RECTIFIER_SIDE_2_INVERTER` or `SIDE_1_INVERTER_SIDE_2_RECTIFIER`).

`validate_create_hvdc_line_fields` enforces required fields, distinct
endpoints, and `|target_p| <= max_p`. pypowsybl handles remaining errors
(unknown station id, station already owning an HVDC line, …).

## Reactive limits — `create_reactive_limits`

Attaches reactive limits to an *existing* generator, battery, or VSC
station (anything with reactive capability). Two modes:

- **min/max**: single `(min_q, max_q)` pair → `create_minmax_reactive_limits`.
- **curve**: ≥2 rows of `(p, min_q, max_q)` → `create_curve_reactive_limits`.
  At least two distinct `p` values are required.

pypowsybl replaces any existing reactive limits on the target.

The form (`_render_create_reactive_limits_form`) appears in the
Generators / Batteries / VSC Converter Stations views and shows a
target picker, an `st.radio` for the kind, and either a min/max pair or
a dynamic-row `st.data_editor` depending on the kind.

`REACTIVE_LIMITS_TARGETS` maps component labels to the network getter
used to enumerate valid targets. `create_reactive_limits(network,
element_id, mode, payload)` runs all validation on the main thread then
dispatches the right `create_*_reactive_limits` call through the worker.

## Operational limits — `create_operational_limits`

Attaches current / apparent-power / active-power limits to an existing
line, 2-winding transformer, or dangling line. Limits are always
submitted as a *group* — pypowsybl replaces the target group on write.

The form (`_render_create_operational_limits_form`) appears on the
Lines / 2-Winding Transformers / Dangling Lines views and takes:

- **Target** element id
- **Side** (`ONE` or `TWO`)
- **Type** (`CURRENT`, `APPARENT_POWER`, `ACTIVE_POWER`)
- **Group name** (defaults to `DEFAULT`)
- Dynamic-row editor for the limit rows (name, value,
  acceptable_duration, fictitious). `acceptable_duration = -1` denotes
  the permanent limit.

`create_operational_limits` enforces:

- Exactly one permanent limit per call.
- Non-negative values; `acceptable_duration` must be `-1` or `>= 0`.
- The underlying dataframe uses `element_id` as the index (required by
  pypowsybl's `create_operational_limits`).

`OPERATIONAL_LIMITS_TARGETS` lists supported component types and the
getter used to enumerate candidate element ids.

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

## Extension creation — `CREATABLE_EXTENSIONS`

Attaches an extension row to an existing element (first-phase set: 12 of
pypowsybl's 27 extensions). Read-only browsing of every extension lives
in `extensions_explorer.py`; this form adds write support for the
extensions that pair with equipment the app can already create.

The form (`_render_create_extension_form`) is rendered for every
component whose label appears in at least one extension's ``targets``
mapping. It shows a dropdown of applicable extensions, a target-id
picker (from the component's getter), and a dynamic field grid driven
by the extension's schema.

Registered extensions and their targets:

| Extension | Component |
| --- | --- |
| `substationPosition` | Substations |
| `entsoeArea` | Substations |
| `busbarSectionPosition` | Busbar Sections |
| `position` | Generators, Loads, Batteries, Shunt/Static VAR Compensators, VSC/LCC Stations |
| `slackTerminal` | Voltage Levels |
| `activePowerControl` | Generators, Batteries |
| `voltageRegulation` | Batteries |
| `voltagePerReactivePowerControl` | Static VAR Compensators |
| `standbyAutomaton` | Static VAR Compensators |
| `hvdcAngleDroopActivePowerControl` | HVDC Lines |
| `hvdcOperatorActivePowerRange` | HVDC Lines |
| `entsoeCategory` | Generators |

Each registry entry carries a ``label``, ``detail`` caption, the
``index`` column used to build the single-row DataFrame (usually
``id``; ``voltage_level_id`` for `slackTerminal`), a ``targets`` map of
component-label to getter, and a ``fields`` list. Each field declares
``kind`` (``float``/``int``/``bool``/``str``/``choice``), ``required``,
``default``, ``help``, and optionally ``options`` (for ``choice``) or
``optional_fill`` — the latter drops the column from the dispatched
DataFrame when left blank.

`create_extension(network, extension_name, target_id, fields)`
validates against the registry, coerces types, drops ``optional_fill``
blanks, and routes `network.create_extensions(name, df)` through the
worker thread. `validate_create_extension_fields` is extracted so the
form can preview errors without a dispatch, and carries per-extension
invariants (e.g. ``slackTerminal`` requires exactly one of ``bus_id``
or ``element_id``; ``activePowerControl`` requires
``max_target_p >= min_target_p``).

`linePosition` is intentionally absent from the first phase: pypowsybl
1.14's writer raises an internal error when populating its multi-row
``(id, num)`` layout. The read-only viewer continues to expose it.

## Column priority — `PRIORITY_COLUMNS`

For Generators and Loads, certain columns are moved to sit right after `name`
in the displayed table so they're visible without scrolling:

```python
"Generators": ["target_p", "target_q", "target_v", "connected",
               "voltage_regulator_on", "p", "q", "regulated_element_id"]
"Loads":      ["p0", "q0", "connected", "p", "q"]
```
