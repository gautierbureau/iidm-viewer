# Short Circuit Analysis

## Entry point

| Location | Trigger |
|---|---|
| `app.py` tab "Short Circuit Analysis" | "Run Short Circuit Analysis" button in the Configuration sub-tab |

## Execution — `state.run_short_circuit_analysis(network, faults, sc_params)`

```python
def run_short_circuit_analysis(network, faults, sc_params):
    raw = object.__getattribute__(network, "_obj")   # unwrap proxy
    # sc_params is read on the main thread before entering run()

    def _run_sc():
        import pypowsybl.shortcircuit as sc
        analysis = sc.create_analysis()
        for f in faults:
            ft = sc.FaultType[f["fault_type"]]
            analysis.add_fault(f["id"], f["element_id"], fault_type=ft)
        params = sc.Parameters(
            study_type=sc.StudyType[sc_params["study_type"]],
            with_feeder_result=sc_params["with_feeder_result"],
            with_limit_violations=sc_params["with_limit_violations"],
            min_voltage_drop_proportional_threshold=...,
        )
        result = sc.run(raw, analysis, parameters=params)
        # Serialize inside worker before results escape
        return _serialize(result, faults)

    return run(_run_sc)
```

All pypowsybl calls happen inside `_run_sc` on the worker thread. All
DataFrames and status strings are serialized to plain Python objects before
returning so they are safe to store in `st.session_state`.

Unlike `run_loadflow` / `run_security_analysis`, short circuit does not
inherit the current load-flow parameters — it has its own parameter set
read from the widget state before entering `run()`.

## Fault building — `state.build_bus_faults(network, nominal_v_set, fault_type)`

```python
def build_bus_faults(network, nominal_v_set=None, fault_type="THREE_PHASE"):
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        buses = raw.get_buses(attributes=["voltage_level_id"])
        vl_df = raw.get_voltage_levels(attributes=["nominal_v"]) if nominal_v_set else None
        return buses, vl_df

    buses, vl_df = run(_gather)
    # filter by nominal_v_set, then return
    # [{"id": "SC_<bus_id>", "element_id": bus_id, "fault_type": fault_type}, ...]
```

Buses and VL data are fetched in a single worker call. The resulting list
drives both the preview count and `run_short_circuit_analysis`.

## pypowsybl API notes

| Symbol | Value |
|---|---|
| Run function | `sc.run(raw, analysis, parameters=params)` |
| Fault types | `sc.FaultType.THREE_PHASE`, `sc.FaultType.SINGLE_PHASE_TO_GROUND` |
| Study types | `sc.StudyType.SUB_TRANSIENT` (default), `sc.StudyType.TRANSIENT` |
| Fault target | bus-view bus IDs (from `network.get_buses()`) |

## UI structure — `short_circuit_analysis.py`

```
render_short_circuit_analysis(network)
├── tab "Configuration"
│   ├── selectbox: fault type (THREE_PHASE / SINGLE_PHASE_TO_GROUND)
│   ├── multiselect: nominal voltage filter (defaults to ≥ 380 kV)
│   ├── caption: N bus faults to be simulated
│   ├── expander: preview fault table
│   ├── subheader: Analysis parameters
│   │   ├── selectbox: study type (SUB_TRANSIENT / TRANSIENT)
│   │   ├── checkbox: with feeder contributions
│   │   ├── checkbox: with limit violations
│   │   └── number_input: min voltage drop threshold (%)
│   └── button: "Run Short Circuit Analysis"
└── tab "Results"
    ├── metrics: total faults / failed / with violations
    ├── slider: show faults with fault power ≥ N MVA
    ├── styled summary table (status + violation count color-coded)
    └── subheader: Fault detail
        ├── text_input: filter by fault ID
        ├── selectbox: select one fault
        ├── metrics: fault power (MVA) + fault current (kA)
        ├── dataframe: feeder contributions (if available)
        └── dataframe: limit violations (if any)
```

## Fault result DataFrame columns (pypowsybl)

### `feeder_results`

| Column | Description |
|---|---|
| `feeder_id` | Feeder (branch / generator) contributing to the fault |
| `side` | Terminal side (ONE / TWO) |
| `current` | Contribution current magnitude (A) |

### `limit_violations`

Same schema as security analysis; see `docs/security-analysis.md`.

## Session-state keys summary

| Key | Set by | Read by |
|---|---|---|
| `_sc_results` | `short_circuit_analysis._render_config_tab` (after successful run) | `short_circuit_analysis._render_results_tab` |

## Extending to branch faults

`build_bus_faults` returns a list-of-dicts. A `build_branch_faults` builder
could emit the same shape using `analysis.add_branch_fault(id, element_id, ...)`
and feed the same `run_short_circuit_analysis` without modifying the results
rendering.
