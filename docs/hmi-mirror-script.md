# HMI Mirror Script (Session Script tab)

The "Session Script" tab turns the user's HMI session into a runnable
Python script.  Every time the app mutates the network (load, edit,
remove, create, run a load flow, run security analysis), one op is
appended to a per-session log; the user downloads a self-contained
`session_<timestamp>.py` that calls into pypowsybl directly to replay
those ops against any compatible network.

The generator does not depend on Streamlit or pypowsybl, so its tests
run without bringing the JVM online.

## Modules

| File | Role |
|---|---|
| `iidm_viewer/script_recorder.py` | Session-state op log; `record_*` helpers called from `state.py` mutators |
| `iidm_viewer/script_generator.py` | Pure-Python op log → runnable script string |
| `iidm_viewer/session_script.py` | "Session Script" tab UI: live preview, download, clear, include-reverted toggle |

## Op log

`st.session_state["_op_log"]` is an ordered list of dicts.  The log is
reset on every successful `load_network` / `create_empty` and the new
op is appended in its place — there is no "before-load" garbage.

Op kinds (all phases):

| Kind | Source | Generator path |
|---|---|---|
| `load_network` | `state.load_network` | `pn.load(args.network_path, …)` in `main()` |
| `create_empty` | `state.create_empty_network` | `pn.create_empty(network_id=…)` in `main()` |
| `run_loadflow` | `state.run_loadflow` | `lf.Parameters(...)` + `lf.run_ac(...)` |
| `update_components` | `data_explorer` Apply, `diagrams` switch toggle | batched `network.update_<method>(df)` |
| `revert_update_components` | `data_explorer` single revert + revert-all | batched `network.update_<method>(df)` (full transcript only) |
| `remove_components` | `data_explorer` Remove | inline `_remove(network, …)` helper |
| `update_extension` / `revert_update_extension` | `extensions_explorer` Apply / Revert | batched `network.update_extensions(name, df)` |
| `remove_extension` | `extensions_explorer` Remove | `network.remove_extensions(name, ids)` |
| `create_component_bay` | `state.create_component_bay` | `pn.<bay_function>(network, _bay_df(…))` (or `_create_shunt_bay` for shunts) |
| `create_branch_bay` | `state.create_branch_bay` | `pn.create_line_bays / create_2_windings_transformer_bays` |
| `create_container` | `state.create_container` | `_create_container(network, create_function, fields)` |
| `create_tap_changer` | `state.create_tap_changer` | `_create_tap_changer(network, method, …)` |
| `create_coupling_device` | `state.create_coupling_device` | `pn.create_coupling_device(...)` |
| `create_hvdc_line` | `state.create_hvdc_line` | `network.create_hvdc_lines(_bay_df(…))` |
| `create_reactive_limits` | `state.create_reactive_limits` | `_create_reactive_limits(network, …)` |
| `create_operational_limits` | `state.create_operational_limits` | `_create_operational_limits(network, …, group_name=…)` |
| `create_extension` | `state.create_extension` | `_create_extension(network, name, target, row, index_col)` |
| `create_secondary_voltage_control` | `state.create_secondary_voltage_control` | `_create_secondary_voltage_control(network, zones, units)` |
| `run_security_analysis` | `state.run_security_analysis` | `_run_security_analysis(network, …)` |
| `run_short_circuit_analysis` | `state.run_short_circuit_analysis` | `_run_short_circuit_analysis(network, faults=…, sc_params=…)` |

## Revert semantics

Revert never deletes log entries.  When a user reverts a property edit:

1. The latest non-reverted matching op (matched on
   `(component, element_id, property)`) is mutated in-place to
   `reverted=True`.
2. A separate `revert_update_components` op is appended that captures
   the value being restored.

The "Include reverted edits" toggle on the Session Script tab decides
how to render this:

- **Off** (default — net state): drop every op with `reverted=True`
  and every `revert_*` op.  The script reproduces the *final* HMI
  state without the cancelled detour.
- **On** (full transcript): emit everything in chronological order.
  An edit followed by a revert appears as two distinct
  `network.update_<method>(...)` calls, mirroring what actually
  happened in the HMI.

Removals are recorded with `reverted=False` for schema uniformity but
the HMI does not yet expose a removal-revert action — see the
`_revert_all_changes` comment in `data_explorer.py`.

## Generator emission

The generator only includes the helpers it needs.  A session that
just runs a load flow imports nothing beyond `argparse`,
`pypowsybl.network`, and `pypowsybl.loadflow`; `pandas` is added when
any update / create op is present, and `pypowsybl.security` is added
inside the SA helper block when an SA op is present.  Each helper
function mirrors its `state.py` counterpart line-for-line.

Adjacent `update_components` / `update_extension` ops with the same
method name are batched into a single
`pd.DataFrame.from_dict({...}, orient='index')` call so that a
revert-all of fifty edits emits one update, not fifty.

The first `load_network` / `create_empty` op drives `main()`:

- `load_network` → an argparse path argument so the user can replay
  against a different file (the source filename is in the docstring
  for provenance).
- `create_empty` → `pn.create_empty(network_id=…)` directly, no CLI arg.

## Adding a new op kind

1. Add `record_<new_kind>(...)` to `script_recorder.py`.  Snapshot the
   payload (deep-copy lists / dicts so subsequent session-state edits
   don't mutate the recorded op).
2. Append `record_<new_kind>(...)` at the *end* of the corresponding
   `state.py` function — after `run(...)` and `invalidate_*()`.
   Recording only after success means failed pypowsybl calls never
   pollute the log.
3. Add an emitter in `script_generator.py` and (if needed) a script-
   side helper to `_HELPERS_REGISTRY`, then register it in
   `_KIND_HELPER_DEPS` so it appears only when the op is present.
4. Extend `tests/test_script_generator.py` with a fixture op-log and
   a `_compile()` smoke check.

## Caveats

- **JSON file paths in security analysis** (`contingencies_json_paths`
  / `actions_json_paths` / `operator_strategies_json_paths`) are
  recorded verbatim.  Replaying needs the same files at the same
  paths.
- **Shunt compensators**: the helper only handles the LINEAR model,
  matching `state._dispatch_shunt_bay`.  Non-linear shunts are not
  yet exposed by the HMI.
- **Out-of-scope tabs**: Network Map, NAD, SLD (except the breaker
  switch toggle, which goes through `update_components`), Voltage
  Analysis, Injection Map, Pmax, Reactive Curves, and the read-only
  Operational Limits chart are pure views — they do not mutate the
  network and are intentionally not recorded.  The analytical-run
  tabs (Security Analysis, Short Circuit Analysis) and every
  network-mutating tab are fully recorded.

## Testing

- `tests/test_script_recorder.py` — pure session-state assertions.
- `tests/test_script_generator.py` — feeds fixture op-logs to
  `generate_script` and checks the output strings + that every
  emitted script `compile()`s.
- `tests/test_session_script_e2e.py` — records a small session,
  writes the script to a tmp file, runs it under
  `subprocess.run([sys.executable, ...])`, asserts a clean exit and
  the expected stdout marker.  Skipped when pypowsybl is not
  installed.
