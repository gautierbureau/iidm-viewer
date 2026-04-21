# Security Analysis

## Entry point

| Location | Trigger |
|---|---|
| `app.py` tab "Security Analysis" | "Run Security Analysis" button in the Configuration sub-tab |

## Execution — `state.run_security_analysis(network, contingencies)`

```python
def run_security_analysis(network, contingencies):
    raw = object.__getattribute__(network, "_obj")   # unwrap proxy
    generic, provider = get_lf_parameters()           # read session state on main thread

    def _run_sa():
        import pypowsybl.security as sa
        import pypowsybl.loadflow as lf
        analysis = sa.create_analysis()
        for c in contingencies:
            analysis.add_single_element_contingency(c["id"], c["element_id"])
        lf_params = lf.Parameters(**generic)
        if provider:
            lf_params.provider_parameters = {k: str(v) for k, v in provider.items()}
        params = sa.Parameters(load_flow_parameters=lf_params)
        result = sa.run_ac(raw, analysis, parameters=params)
        # Serialize inside worker before results escape
        return _serialize(result, contingencies)

    return run(_run_sa)
```

All pypowsybl calls happen inside `_run_sa` on the worker thread. Results
(pre/post DataFrames and status strings) are serialized to plain Python
objects before returning so they are safe to store in `st.session_state`.

LF parameters are read **before** entering `run()` because `st.session_state`
is not safe to access from the worker.

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
│   ├── selectbox: element type (Lines / 2-Winding Transformers)
│   ├── multiselect: nominal voltage filter (defaults to ≥ 380 kV)
│   ├── caption: N contingencies to be simulated
│   ├── expander: preview contingency table
│   └── button: "Run Security Analysis"
└── tab "Results"
    ├── subheader: Pre-contingency state
    │   ├── metric: base case status
    │   ├── metric: pre-contingency violation count
    │   └── dataframe: pre-contingency limit violations (if any)
    ├── subheader: Post-contingency results
    │   ├── metrics: total / failed / with violations
    │   ├── slider: show only contingencies with violations ≥ N
    │   └── styled summary dataframe (status color-coded)
    └── subheader: Contingency detail
        ├── text_input: filter by contingency ID
        ├── selectbox: select one contingency
        └── dataframe: limit violations for selected contingency
```

Results are stored in `_sa_results` and survive reruns within the session.

## Session-state keys summary

| Key | Set by | Read by |
|---|---|---|
| `_sa_results` | `security_analysis._render_config_tab` (after successful run) | `security_analysis._render_results_tab` |

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
