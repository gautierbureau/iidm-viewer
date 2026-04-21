# Security Analysis

## Entry point

| Location | Trigger |
|---|---|
| `app.py` tab "Security Analysis" | "Run Security Analysis" button in the Configuration sub-tab |

## Execution — `state.run_security_analysis(network, contingencies, monitored_elements=None, limit_reductions=None)`

All pypowsybl calls happen inside `_run_sa` on the worker thread. Results
(pre/post DataFrames, monitored-element DataFrames and status strings) are
serialized to plain Python objects before returning so they are safe to store
in `st.session_state`.

LF parameters are read **before** entering `run()` because `st.session_state`
is not safe to access from the worker.

Inside the worker, the analysis is composed in this order:

1. `add_single_element_contingency` for each contingency
2. `add_monitored_elements(...)` for each monitored-element rule
3. `add_limit_reductions(pd.DataFrame(...).set_index("limit_type"))` if any are defined
4. `_apply_action(...)` for each action — see the dispatcher below
5. `add_operator_strategy(...)` for each operator strategy
6. `run_ac(raw, parameters=params)`

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

## Contingency building — `state.build_n1_contingencies(network, element_type, nominal_v_set)`

```python
def build_n1_contingencies(network, element_type, nominal_v_set=None):
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        elem_df = getattr(raw, getter)(attributes=vl_cols)
        vl_df = raw.get_voltage_levels(attributes=["nominal_v"]) if nominal_v_set else None
        return elem_df, vl_df

    elem_df, vl_df = run(_gather)
    # filter by nominal_v_set, then return [{"id": "N1_<id>", "element_id": id}, ...]
```

Both the element table and VL table are fetched in a single `run()` call to
avoid two round-trips to the worker. The resulting list is a plain Python
structure that drives both the preview count and the `run_security_analysis`
call.

## UI structure — `security_analysis.py`

```
render_security_analysis(network)
├── tab "Configuration"
│   ├── sub-tab "Contingencies"
│   │   ├── selectbox: element type (Lines / 2-Winding Transformers)
│   │   ├── multiselect: nominal voltage filter (defaults to ≥ 380 kV)
│   │   ├── caption: N contingencies to be simulated
│   │   └── expander: preview contingency table
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
│   │   └── list of actions with per-row Remove (Remove also drops the action
│   │       from any operator strategy that referenced it)
│   ├── sub-tab "Operator strategies"
│   │   ├── form: strategy_id + contingency selector + action multiselect +
│   │   │       condition selector
│   │   └── list of strategies with per-row Remove button
│   └── footer row: metrics (contingencies / monitored / reductions /
│                 actions / strategies) + button "Run Security Analysis"
└── tab "Results"
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
| `_sa_contingencies` | `_render_contingencies_subtab` (rebuilt each render) | `_render_config_tab`, `_render_monitored_subtab`, `_render_operator_strategies_subtab` |
| `_sa_monitored` | `_render_monitored_subtab` | `_render_config_tab` → `run_security_analysis` |
| `_sa_limit_reductions` | `_render_limit_reductions_subtab` | `_render_config_tab` → `run_security_analysis` |
| `_sa_actions` | `_render_actions_subtab` | `_render_config_tab` → `run_security_analysis`; `_render_operator_strategies_subtab` |
| `_sa_operator_strategies` | `_render_operator_strategies_subtab` | `_render_config_tab` → `run_security_analysis` |
| `_sa_id_cache` | `_get_ids` (one worker call per session) | `_render_monitored_subtab`, `_render_actions_subtab` |
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

## Extending to N-2 / custom contingencies

The architecture is contingency-list-driven: `build_n1_contingencies` is a
builder that returns `list[dict]`. New builders (N-2, manual definition form,
filtered by substation, etc.) can produce the same shape and feed the same
`run_security_analysis` without changing the results rendering.
