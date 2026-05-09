# N vs N-K state comparison via pypowsybl variant manager

Park for later: design doc for adding side-by-side N (base) vs N-K
(post-contingency) comparison to the viewer using pypowsybl's variant manager.
Status: planning only, no code yet.

## Context

Today the viewer holds exactly one network state. After a load flow there is
one set of `(p, q, i, v_mag, v_angle)` columns in every getter. We want to add
a second "N-K" state derived from the base "N" by:

1. Cloning the working variant via
   `network.get_variant_manager().clone_variant("InitialState", "N-K")`.
2. Disconnecting a user-picked set of branches/transformers/generators on the
   N-K variant only.
3. Running an independent AC load flow on N-K so the two variants carry
   different flow/voltage results.

Four tabs need to surface the comparison: **Data Explorer** (DataFrames),
**SLD** (two SVGs), **Reactive Capability Curves** (PQ overlay), **Operational
Limits** (dual loading %). Out of scope for the first step: NAD, Network Map,
Voltage Analysis, Pmax, Injection Map, Extensions, Network Reduction.

The codebase explicitly avoided variants until now
([docs/data-explorer.md](data-explorer.md) §"Restore button"), because variants
share topology and so cannot serve as a structural-undo mechanism. For N-K
that limitation is fine: we mutate connection state, never topology.

## Decisions taken

- **Contingency picker**: reuse the existing manual picker in
  `iidm_viewer/security_analysis.py` (extract it into a reusable function).
- **View-mode toggle**: per-tab radio at the top of each affected tab
  (`N | N-K | Side-by-side`).
- **N-K is read-only**: Apply / Apply+LF / Remove buttons disabled in N-K and
  Side-by-side modes in the Data Explorer.
- **LF triggering**: explicit "Run N-K Load Flow" button in the sidebar,
  greyed out until an N-K variant has been built.

## The one rule (carry-over from [AGENTS.md §1](../AGENTS.md))

`set_working_variant` mutates the network's "current variant" globally. Doing
`set_working_variant("N-K")` and `get_lines(...)` as **separate** worker
round-trips is unsafe — an interleaved Streamlit rerun can swap the variant
between them. **Every variant-scoped fetch must do switch+work+restore inside a
single `run(...)` call.** Between worker calls the working variant is always
`"InitialState"`.

## Architecture additions

### Session state keys (new)

| Key | Type | Lifecycle |
|---|---|---|
| `_nk_contingency` | `dict` `{id, element_ids}` or `None` | set by Build N-K; cleared on network replace, on any topology edit, on Clear N-K |
| `_nk_variant_id` | `"N-K"` or `None` | set after a successful clone |
| `_nk_lf_status` | `"NEVER" \| "CONVERGED" \| "FAILED" \| "DIVERGED"` | updated by Run N-K LF |
| `_nk_lf_report_json` | `str` | mirrors `_lf_report_json` |
| `_lf_gen` | becomes `dict[str, int]` keyed by variant id | per-variant LF generation counter |
| `_de_view_mode`, `_sld_view_mode`, `_rcc_view_mode`, `_oplim_view_mode` | `"N" \| "N-K" \| "Side-by-side"` | one per affected tab; defaults to `"N"` |
| `_nk_picker_payload` | `dict[str, list[str]]` | transient output of the reused picker |

A new `_NK_CACHE_KEYS` tuple in `caches.py` lists these so
`invalidate_on_network_replace` pops them all.

### Worker helpers (new in `iidm_viewer/state.py`)

All three follow the same pattern: switch+work+restore inside one `run(...)`.

```python
def build_contingency_variant(network, contingency, target_variant="N-K"):
    """Clone InitialState into target_variant and disconnect picked elements."""

def run_loadflow_on_variant(network, variant_id):
    """Run AC LF on variant_id, restore working variant. Calls
    invalidate_on_load_flow(variant_id=...) on return."""

def drop_nk_variant(network):
    """Restore working variant to InitialState, remove N-K from variant manager."""
```

Disconnection uses `update_lines` / `update_2_windings_transformers` /
`update_3_windings_transformers` with `connected1=False, connected2=False`,
and `update_generators` with `connected=False`. Element ids are split by type
using the existing `_get_ids(network)` helper from `security_analysis.py:79`.

### Generic "fetch on a variant" primitive (new in `caches.py`)

```python
def fetch_for_variant(network, fn_name, variant_id, *args, **kwargs):
    """Atomic switch + getattr(raw, fn_name)(...) + restore inside one run()."""
```

Plus thin wrappers used by the four tabs:
`get_lines_all_for_variant`, `get_2wt_all_for_variant`,
`get_buses_all_for_variant`, `get_generators_all_for_variant`,
`get_component_df_for_variant`, `get_enriched_component_for_variant`.
When `variant_id == "InitialState"`, they short-circuit to the existing cached
getter so the base path takes no extra round-trip.

### Cache key extension (`iidm_viewer/caches.py`)

- `_lf_gen` becomes a dict keyed by variant id;
  `_lf_gen(variant_id="InitialState")` reads it.
- `_cache_key(network, variant_id="InitialState")` returns
  `(net_key, _lf_gen(variant_id), variant_id)`.
- Single-entry caches in `_LOAD_FLOW_CACHE_KEYS` (`_lines_all_cache`,
  `_2wt_all_cache`, `_buses_all`, `_generators_all_cache`, `_3wt_all_cache`,
  `_bus_voltages_cache`, `_loading_cache`, …) become dicts keyed by
  `variant_id`. The dict-shaped caches (`_de_component_cache`,
  `_enriched_component_cache`, `_ext_df_cache`) just take the longer key.
- `invalidate_on_load_flow(variant_id="InitialState")` bumps only that
  variant's generation counter and pops only that variant's slot.
- `invalidate_on_topology_change` also calls `drop_nk_variant(network)` and
  clears `_nk_contingency` / `_nk_variant_id` (topology change ⇒ contingency
  element ids may dangle). Implemented as a new
  `state._invalidate_topology(network)` wrapper — every existing call site in
  `state.py` is rerouted through it.
- `invalidate_on_network_replace` pops `_NK_CACHE_KEYS` and resets
  `_lf_gen = {"InitialState": 0}`. Does **not** call `drop_nk_variant` because
  the underlying raw network is being released anyway.

## Sidebar (`iidm_viewer/app.py:269-302`)

Add a collapsed expander after the existing "Run AC Load Flow" block:

```
[N-K Variant]
  ── render_contingency_picker(network, key_prefix="nk_pick")  ← see Picker refactor
  [Build N-K]    [Run N-K Load Flow]   ← second disabled until variant exists
  N-K LF status pill
  [Clear N-K]
```

Build N-K calls `state.build_contingency_variant(network, contingency)` and
sets `_nk_variant_id = "N-K"`. Run N-K LF calls
`state.run_loadflow_on_variant(network, "N-K")` and updates `_nk_lf_status`.
Clear N-K calls `state.drop_nk_variant(network)` and pops the `_nk_*` keys.

## Picker refactor (`iidm_viewer/security_analysis.py`)

Extract the manual-contingency form into:

```python
def render_contingency_picker(network, key_prefix: str, *, grouping="single") -> dict | None:
    """Self-contained widget. Returns one {id, element_ids} dict on submit."""
```

What moves: the element-type selectbox + `_MANUAL_TYPES` lookup
(`security_analysis.py:30-44`), the filter rendering, the multiselect form,
the preview/remove list. What stays in `_render_contingencies_subtab`: the
auto N-1/N-2 builder, JSON-import, `_sa_contingencies` aggregation. The
Security Analysis sub-tab uses a thin shim that loops returned dicts into
`_sa_manual_contingencies`; the sidebar uses the dict directly.

## Per-tab changes

A small helper in `iidm_viewer/components.py`:

```python
def render_view_mode_radio(key: str) -> Literal["N", "N-K", "Side-by-side"]:
    """Radio at the top of an affected tab. Greyed out (returns "N") until
    st.session_state["_nk_variant_id"] is set."""
```

### Data Explorer (`iidm_viewer/data_explorer.py:1172-1369`)

- Insert `view_mode = render_view_mode_radio("_de_view_mode")` after the
  component-type selectbox (around line 1180).
- Replace `df = get_enriched_component(network, method_name)` (line 1235) with
  a dispatch on `view_mode`. Side-by-side renders two `st.data_editor`s in
  `st.columns(2)`; N-K column uses `disabled=True`.
- Apply / Apply+LF / Remove (lines 1326–1359) and creation forms
  (lines 1196–1220) gated on `view_mode == "N"`. Caption: "N-K data is
  read-only in the first step".

### SLD (`iidm_viewer/diagrams.py:313-410`)

- Insert `view_mode = render_view_mode_radio("_sld_view_mode")` after the
  early-return on missing VL (line 318).
- Cache key changes from `container_id` (line 348) to
  `(container_id, variant_id)`. Add
  `_sld_for_variant(network, container_id, variant_id)` that wraps
  `set_working_variant` + `get_single_line_diagram(...).svg` + `.metadata` +
  restore inside one `run(...)`.
- Side-by-side renders two columns, each with its own `render_interactive_sld`
  using key `f"sld_{container_id}_{variant_id}"`.
- Click handlers (lines 377–410) only mutate the network when the click came
  from the N-side viewer; N-K clicks are ignored.

### Reactive Capability Curves (`iidm_viewer/reactive_curves.py:14-146`)

- Insert `view_mode = render_view_mode_radio("_rcc_view_mode")` at the top.
- Generators DF fetched per variant:
  `get_generators_all_for_variant(network, "InitialState")` and (if
  `view_mode != "N"`) `get_generators_all_for_variant(network, "N-K")`. The
  capability curve itself (`get_reactive_curve_points`, line 15) is
  topology-only and shared.
- Polygon trace (lines 100–107) stays single. Operating-point trace
  (lines 110–120): in N-K and Side-by-side modes plot two markers — N as red
  `x`, N-K as blue `cross`. Same for the target diamond (lines 122–130).
- Metric row (lines 71–75) becomes a 2-column comparison `[N | N-K | Δ]` in
  side-by-side.

### Operational Limits (`iidm_viewer/operational_limits.py:222-327`)

- Insert `view_mode = render_view_mode_radio("_oplim_view_mode")` at the top.
- `_compute_loading(network, limits_reset)` (line 128) gains a `variant_id`
  parameter; cache key (line 136) extends with it. `_branch_dataframes(network)`
  (line 22) becomes `_branch_dataframes_for_variant(network, variant_id)`.
- The "Most loaded" table (line 234) in side-by-side shows columns
  `[Element, Type, Side, I_N, I_NK, Limit, Load%_N, Load%_NK, ΔLoad%]` sorted
  by `ΔLoad%` desc (worst contingency impact first).
- `_build_element_chart` (line 77) takes an optional `current_flow_nk`; emits
  a second `add_hline` per side in a different colour/dash. Title:
  `f"Current limits — {element_id} (N vs N-K)"`.

## Critical files to modify

- `iidm_viewer/state.py` — add `build_contingency_variant`,
  `run_loadflow_on_variant`, `drop_nk_variant`, `_disconnect_elements`,
  `_invalidate_topology` wrapper.
- `iidm_viewer/caches.py` — variant-keyed `_lf_gen` and `_cache_key`;
  `fetch_for_variant`; per-variant getters; updated invalidation entry points;
  `_NK_CACHE_KEYS`.
- `iidm_viewer/security_analysis.py` — extract `render_contingency_picker`.
- `iidm_viewer/app.py` — sidebar "N-K Variant" expander.
- `iidm_viewer/components.py` — `render_view_mode_radio`.
- `iidm_viewer/data_explorer.py` — view-mode radio, side-by-side editors,
  gating of mutating actions.
- `iidm_viewer/diagrams.py` — view-mode radio, per-variant SLD cache.
- `iidm_viewer/reactive_curves.py` — view-mode radio, per-variant operating
  points + targets.
- `iidm_viewer/operational_limits.py` — view-mode radio, per-variant loading,
  dual current line in chart, dual-loading-% table.

## Out-of-scope tabs (must keep working unchanged)

Overview, Network Area Diagram, Network Map, Pmax, Voltage Analysis, Injection
Map, Extensions Explorer, Short Circuit Analysis, Network Reduction. All read
their data via the default-`InitialState` path (no variant_id), which is the
existing path renamed.

## Risks and mitigations

- **Variant cleanup on network replace.** N-K variant lives on the OLD raw
  network; that handle is released by `load_network`.
  `invalidate_on_network_replace` pops `_NK_CACHE_KEYS` and resets `_lf_gen`;
  do **not** call `drop_nk_variant` on the dying network.
- **Topology edits dangle the contingency.** Every site that calls
  `invalidate_on_topology_change()` (24 hits in `state.py`) is rerouted
  through `state._invalidate_topology(network)` which also calls
  `drop_nk_variant` and clears the contingency keys. Show a one-time toast
  "N-K variant cleared because the base network changed."
- **Thread safety of `set_working_variant`.** Enforced by code review and a
  unit test `test_variant_state_is_restored_after_fetch`: every variant
  operation must be atomic inside a single `run(...)`. Between worker calls
  the working variant is always `"InitialState"` (invariant).
- **Cache memory growth.** Per-variant dicts cap at 2 entries
  (`InitialState`, `N-K`). `drop_nk_variant` pops the `"N-K"` slot from every
  variant-keyed cache.

## Verification plan

### Unit tests (new / extended in `tests/`)

- `test_state.py`: `test_build_contingency_variant_creates_variant`,
  `test_run_loadflow_on_variant_only_affects_target` (assert N-side `p`/`q`
  are unchanged after N-K LF), `test_topology_change_drops_nk_variant`,
  `test_variant_state_is_restored_after_fetch`.
- `test_caches.py` (new): variant-keyed `_lf_gen`, dict-shaped caches, slot
  popping on `drop_nk_variant`.
- `test_data_explorer.py`, `test_reactive_curves.py`,
  `test_operational_limits.py`, `test_diagrams.py`: assert `view_mode="N"`
  produces identical output to today; `view_mode="Side-by-side"` requires a
  built variant.
- `test_security_analysis.py`: standalone `render_contingency_picker` returns
  the same dicts as the embedded path.

### End-to-end (per [AGENTS.md §2](../AGENTS.md) Playwright recipe)

```
- upload test_ieee14.xiidm
- select a different VL  (forces post-upload thread switch — segfault canary)
- click "Run AC Load Flow"
- expand sidebar "N-K Variant"
- pick element type "Lines", select line "L1-2-1", id "single_line_outage"
- click "Build N-K"
- click "Run N-K Load Flow"
- for tab in [Data Explorer, Single Line Diagram,
              Reactive Capability Curves, Operational Limits]:
    click tab; select "Side-by-side"; wait 3s
- assert ps -p $SPID alive AND no "Segmentation fault" in /tmp/streamlit.log
```

### Manual smoke

After E2E, also: edit a generator's `target_p` in N (Apply+LF), verify N-K is
auto-cleared (toast + `_nk_variant_id` is None). Re-build N-K, then upload a
new file — verify all `_nk_*` keys are cleared and the sidebar expander
resets.

## Suggested implementation order

Each step is independently testable — land them as separate commits so any
regression is bisectable.

1. **Plumbing** (no UI change yet): variant-keyed `_lf_gen` and `_cache_key`
   in `caches.py`; `build_contingency_variant` /
   `run_loadflow_on_variant` / `drop_nk_variant` in `state.py`. Unit tests
   for the atomic switch+restore invariant.
2. **Picker refactor**: extract `render_contingency_picker` from
   `security_analysis.py`; prove the SA tab behaves identically.
3. **Sidebar wiring**: N-K Variant expander with Build / Run N-K LF / Clear
   buttons.
4. **Per-tab rollout**, one tab per commit: Reactive Curves → Operational
   Limits → Data Explorer → SLD.
5. **End-to-end Playwright run** from AGENTS.md §2 with the N-K scenario.
