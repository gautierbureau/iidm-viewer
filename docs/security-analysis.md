# Security Analysis

## Entry point

| Location | Trigger |
|---|---|
| `app.py` tab "Security Analysis" | "Run Security Analysis" button in the Configuration sub-tab |

## Execution — `state.run_security_analysis(network, contingencies, monitored_elements=None, limit_reductions=None, actions=None, operator_strategies=None, contingencies_json_paths=None, actions_json_paths=None, operator_strategies_json_paths=None)`

All pypowsybl calls happen inside `_run_sa` on the worker thread. Results
(pre/post DataFrames, monitored-element DataFrames and status strings) are
serialized to plain Python objects before returning so they are safe to store
in `st.session_state`.

LF parameters are read **before** entering `run()` because `st.session_state`
is not safe to access from the worker.

Inside the worker, the analysis is composed in this order:

1. `add_single_element_contingency` / `add_multiple_elements_contingency` for each dict contingency
2. `add_contingencies_from_json_file(path)` for each path in `contingencies_json_paths`
3. `add_monitored_elements(...)` for each monitored-element rule
4. `add_limit_reductions(pd.DataFrame(...).set_index("limit_type"))` if any are defined
5. `_apply_action(...)` for each action — see the dispatcher below
6. `add_actions_from_json_file(path)` for each path in `actions_json_paths`
7. `add_operator_strategy(...)` for each operator strategy
8. `add_operator_strategies_from_json_file(path)` for each path in `operator_strategies_json_paths`
9. `run_ac(raw, parameters=params)`
10. `result.export_to_json(tempfile)` → bytes stashed in `result["json_export"]`

The result is split per contingency: monitored `branch_results`, `bus_results`
and `three_windings_transformer_results` come back as multi-indexed DataFrames
whose level-0 key is the contingency id (empty string for pre-contingency);
`_select(...)` slices them into per-contingency frames stored alongside each
`PostContingencyResult`.

### Monitored-element rule shape

Each entry in `monitored_elements` is a dict:

| Key | Type | Notes |
|---|---|---|
| `contingency_context_type` | `"ALL"` / `"NONE"` / `"SPECIFIC"` | Mapped to `ContingencyContextType` inside the worker |
| `contingency_ids` | `list[str]` or `None` | Required when context is `SPECIFIC` |
| `branch_ids` | `list[str]` or `None` | Lines + 2-winding transformers |
| `voltage_level_ids` | `list[str]` or `None` | |
| `three_windings_transformer_ids` | `list[str]` or `None` | |

### Limit-reduction row shape

Each entry in `limit_reductions` is a dict flattened into a one-row DataFrame
passed to `add_limit_reductions`:

| Key | Type | Notes |
|---|---|---|
| `limit_type` | `"CURRENT"` | Only value supported by OpenLoadFlow |
| `permanent` | `bool` | Apply to permanent limits |
| `temporary` | `bool` | Apply to temporary limits |
| `value` | `float ∈ [0, 1]` | Reduction factor |
| `contingency_context` | `"ALL"` | Only `ALL` supported by OpenLoadFlow |
| `min_temporary_duration` / `max_temporary_duration` | `int` (s) | Optional, only when `temporary=True` |
| `country` | `str` | Optional, 2-letter code |
| `min_voltage` / `max_voltage` | `float` (kV) | Optional range filter |

### Action dict shape (dispatched by `_apply_action`)

All entries share `{"action_id": str, "type": <ACTION_TYPE>}`. Type-specific
fields are:

| `type` | Required fields | Optional fields |
|---|---|---|
| `SWITCH` | `switch_id` (str), `open` (bool) | — |
| `TERMINALS_CONNECTION` | `element_id` (str), `opening` (bool) | `side` (`"NONE"` / `"ONE"` / `"TWO"`) |
| `GENERATOR_ACTIVE_POWER` | `generator_id` (str), `is_relative` (bool), `active_power` (float, MW) | — |
| `LOAD_ACTIVE_POWER` | `load_id` (str), `is_relative` (bool), `active_power` (float, MW) | — |
| `PHASE_TAP_CHANGER_POSITION` | `transformer_id` (str), `is_relative` (bool), `tap_position` (int) | `side` |
| `RATIO_TAP_CHANGER_POSITION` | `transformer_id` (str), `is_relative` (bool), `tap_position` (int) | `side` |
| `SHUNT_COMPENSATOR_POSITION` | `shunt_id` (str), `section` (int) | — |

Adding a new action type means: extend `_ACTION_TYPES` in
`security_analysis.py`, add a branch in `_render_action_form_fields` and in
`_action_summary`, and add a matching branch in `state._apply_action`.

### Operator strategy shape

| Key | Type | Notes |
|---|---|---|
| `operator_strategy_id` | str | Unique |
| `contingency_id` | str | Must match an entry in `contingencies` |
| `action_ids` | list[str] | Applied in list order |
| `condition_type` | str | `TRUE_CONDITION` (default) or one of the violation conditions |
| `violation_subject_ids` | list[str] | Empty/missing → any element (only used when condition is violation-based) |
| `violation_types` | list[str] | Subset of `CURRENT` / `ACTIVE_POWER` / `APPARENT_POWER` / `LOW_VOLTAGE` / `HIGH_VOLTAGE`; empty → any type |

## Contingency building

### Contingency dict shape

Every contingency has a unique `id` and a list of `element_ids`. The
Contingencies sub-tab composes the list from an automatic builder and an
optional manual list:

| Key | Type | Notes |
|---|---|---|
| `id` | str | Unique within the run |
| `element_ids` | list[str] | One entry = N-1; two or more = N-k |
| `element_id` | str | Back-compat alias for N-1 entries (equals `element_ids[0]`) |

Inside the worker, `state.run_security_analysis` dispatches on list length:
`len(element_ids) == 1` → `add_single_element_contingency`, otherwise
`add_multiple_elements_contingency`.

### `state.build_n1_contingencies(network, element_type, nominal_v_set=None)`

Returns one N-1 entry per element of `element_type` whose terminals touch
`nominal_v_set`:

```python
[{"id": f"N1_{eid}", "element_id": eid, "element_ids": [eid]} for eid in ...]
```

Both the element table and VL table are fetched in a single `run()` call to
avoid two round-trips to the worker.

### `state.build_n2_contingencies(network, element_type, nominal_v_set=None)`

Calls `build_n1_contingencies` to enumerate eligible elements, then returns
every unique unordered pair `(a, b)` with `a < b`:

```python
[{"id": f"N2_{a}_{b}", "element_ids": [a, b]} for (a, b) in combinations(ids, 2)]
```

The combinatorics can grow fast — pair count is `n * (n - 1) / 2` where `n`
is the number of filtered elements — so narrow the voltage filter before
running.

### Manual contingencies

The sub-tab also exposes a form to pick any subset of elements of a given
type (Lines / 2WTs / 3WTs / Generators) and add them either as one N-1 per
element or as a single grouped N-k contingency. Manual entries live in
`_sa_manual_contingencies` and are concatenated with the auto list to form
the authoritative `_sa_contingencies`.

On large networks the raw id list per type can be unwieldy, so the form
reuses the Components explorer filter stack (`iidm_viewer.filters` —
`FILTERS`, `build_vl_lookup`, `enrich_with_joins`, `render_filters`) via a
`_get_filterable_df(network, manual_type)` helper. The filter expander sits
outside the form so adjustments immediately narrow the multiselect options;
the enriched DataFrame is cached in `_sa_manual_df_cache` per
`(id(network), manual_type)`.

## JSON import / export

The Configuration sub-tabs for Contingencies, Actions and Operator strategies
each expose an "Upload JSON file(s)" expander that delegates to
`_render_json_upload_section(label, state_key, help_text, uploader_gen_key)`.
Uploaded `UploadedFile` objects are persisted to tempfiles by
`_persist_uploaded_json(files, state_key)` and their paths are recorded in
session state as `[{"name": str, "path": str}, ...]`. The file uploader
widget is keyed with an incrementing generation counter so it resets after
a successful load, matching the pattern used by the top-level network
uploader in `app.py`.

At run time, `_render_config_tab` extracts the persisted paths via
`_json_paths(state_key)` and forwards them to `run_security_analysis` as
`contingencies_json_paths` / `actions_json_paths` /
`operator_strategies_json_paths`. The worker calls the matching
`add_*_from_json_file(path)` method so pypowsybl parses the files natively —
we never inspect or reshape their contents.

The run button stays enabled as long as **any** contingency source is
present (either a dict contingency or at least one JSON file).

After `run_ac`, the worker writes `result.export_to_json(tempfile)`, reads
the bytes, deletes the tempfile, and returns the bytes under
`results["json_export"]`. The Results tab exposes a `st.download_button`
for those bytes (only when the key is present).

JSON-imported contingencies that don't appear in the form list are still
rendered in the Results summary DataFrame (marked `(from JSON)`) and in the
drill-down selectbox.

## UI structure — `security_analysis.py`

```
render_security_analysis(network)
├── tab "Configuration"
│   ├── sub-tab "Contingencies"
│   │   ├── automatic builder
│   │   │   ├── radio: N-1 / N-2
│   │   │   ├── selectbox: element type (Lines / 2-Winding Transformers)
│   │   │   └── multiselect: nominal voltage filter (defaults to ≥ 380 kV)
│   │   ├── manual form (type selector + FILTERS expander + element multiselect
│   │   │       + grouping) and list of manual entries with per-row Remove
│   │   ├── expander: "Upload contingency JSON file(s)"
│   │   ├── caption: <auto> + <manual> + <json> counts
│   │   └── expander: preview composed contingency table
│   ├── sub-tab "Monitored elements"
│   │   ├── form: context (ALL/NONE/SPECIFIC) + contingency picker + id multiselects
│   │   │       (branches, voltage levels, 3WTs)
│   │   └── list of rules with per-row Remove button
│   ├── sub-tab "Limit reductions"
│   │   ├── form: value, permanent/temporary flags, duration window,
│   │   │       country, voltage window
│   │   └── list of reductions (+ expander with preview DataFrame)
│   ├── sub-tab "Actions"
│   │   ├── selectbox: action type (SWITCH / TERMINALS_CONNECTION /
│   │   │       GENERATOR_ACTIVE_POWER / PHASE_TAP_CHANGER_POSITION)
│   │   ├── form: action_id + type-specific fields
│   │   ├── expander: "Upload action JSON file(s)"
│   │   └── list of actions with per-row Remove (Remove also drops the action
│   │       from any operator strategy that referenced it)
│   ├── sub-tab "Operator strategies"
│   │   ├── form: strategy_id + contingency selector + action multiselect +
│   │   │       condition selector
│   │   ├── expander: "Upload operator-strategy JSON file(s)"
│   │   └── list of strategies with per-row Remove button
│   └── footer row: metrics (contingencies / monitored / reductions /
│                 actions / strategies) + button "Run Security Analysis"
│                 (enabled as long as any contingency source — form dicts
│                  or uploaded JSON — is present)
└── tab "Results"
    ├── download_button: "Download results (JSON)" (when json_export is set)
    ├── subheader: Pre-contingency state
    │   ├── metric: base case status
    │   ├── metric: pre-contingency violation count
    │   ├── dataframe: pre-contingency limit violations (if any)
    │   └── expander: pre-contingency monitored results (branches/buses/3WTs)
    ├── subheader: Post-contingency results
    │   ├── metrics: total / failed / with violations
    │   ├── slider: show only contingencies with violations ≥ N
    │   └── styled summary dataframe (status color-coded)
    └── subheader: Contingency detail
        ├── text_input: filter by contingency ID
        ├── selectbox: select one contingency
        ├── dataframe: limit violations for selected contingency
        ├── dataframes: monitored branches/buses/3WTs for this contingency
        └── operator-strategy blocks (status + violations + monitored)
                for any strategy targeting the selected contingency
```

Results are stored in `_sa_results` and survive reruns within the session.

## Session-state keys summary

| Key | Set by | Read by |
|---|---|---|
| `_sa_contingencies` | `_render_contingencies_subtab` (rebuilt each render = auto + manual) | `_render_config_tab`, `_render_monitored_subtab`, `_render_operator_strategies_subtab` |
| `_sa_manual_contingencies` | `_render_contingencies_subtab` (manual form) | `_render_contingencies_subtab` |
| `_sa_manual_df_cache` | `_get_filterable_df` (one pypowsybl+join call per `(network, type)`) | `_render_contingencies_subtab` |
| `_sa_monitored` | `_render_monitored_subtab` | `_render_config_tab` → `run_security_analysis` |
| `_sa_limit_reductions` | `_render_limit_reductions_subtab` | `_render_config_tab` → `run_security_analysis` |
| `_sa_actions` | `_render_actions_subtab` | `_render_config_tab` → `run_security_analysis`; `_render_operator_strategies_subtab` |
| `_sa_operator_strategies` | `_render_operator_strategies_subtab` | `_render_config_tab` → `run_security_analysis` |
| `_sa_id_cache` | `_get_ids` (one worker call per session) | `_render_monitored_subtab`, `_render_actions_subtab` |
| `_sa_contingencies_json_files` | `_render_json_upload_section` (Contingencies) | `_render_config_tab` → `run_security_analysis` |
| `_sa_actions_json_files` | `_render_json_upload_section` (Actions) | `_render_config_tab` → `run_security_analysis` |
| `_sa_operator_strategies_json_files` | `_render_json_upload_section` (Operator strategies) | `_render_config_tab` → `run_security_analysis` |
| `_sa_*_json_uploader_gen` | `_render_json_upload_section` (incremented after each successful load to reset the uploader) | `_render_json_upload_section` |
| `_sa_results` | `_render_config_tab` (after successful run) | `_render_results_tab` |

## Limit violations DataFrame columns (pypowsybl)

| Column | Description |
|---|---|
| `subject_id` | Network element with the violation |
| `subject_name` | Element name |
| `limit_type` | `CURRENT` / `APPARENT_POWER` / `ACTIVE_POWER` |
| `limit_name` | Name of the violated limit |
| `limit` | Limit threshold value |
| `acceptable_duration` | Duration class (−1 = permanent) |
| `limit_reduction` | Applied reduction factor (1.0 = no reduction) |
| `value` | Actual flow/current |
| `side` | `ONE` or `TWO` |

## Extending to custom contingency builders

The architecture is contingency-list-driven: every builder returns
`list[dict]` with the shape documented above. Existing builders cover N-1
(`build_n1_contingencies`), N-2 pairs (`build_n2_contingencies`), and the
Contingencies sub-tab's manual form (grouped N-k from a picked subset). New
builders (filtered by substation, common-mode outages, imported from a CSV,
etc.) produce the same shape and feed the same `run_security_analysis`
without changing the results rendering.
