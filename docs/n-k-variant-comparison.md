# N vs N-K state comparison via pypowsybl variant manager

Park for later: design doc for adding side-by-side N (base) vs N-K
(post-contingency) comparison to the viewer using pypowsybl's variant manager.
Status: planning only, no code yet. Rewritten after the host-agnostic backbone
landed (Streamlit + PySide6 `qt/` + NiceGUI `web/` now share
`component_registry.py`, `diagram_services.py`, `network_loader.py`,
`change_log.py`, `loadflow.py`, `data_view.py`, plus per-tab cores).

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

Four tabs surface the comparison: **Data Explorer** (DataFrames), **SLD** (two
SVGs), **Reactive Capability Curves** (PQ overlay), **Operational Limits**
(dual loading %). Out of scope for the first step: NAD, Network Map, Voltage
Analysis, Pmax, Injection Map, Extensions, Network Reduction.

Note: variants share topology (`docs/data-explorer.md` §"Restore button" still
applies — variants are not a structural-undo mechanism). For N-K that's fine:
we mutate connection state, never topology.

## Decisions taken

- **Contingency picker**: reuse the existing manual picker (currently embedded
  in `iidm_viewer/security_analysis_tab.py:255-356`) by extracting a small
  view-model into the host-agnostic core.
- **View-mode toggle**: per-tab radio at the top of each affected tab
  (`N | N-K | Side-by-side`).
- **N-K is read-only**: Apply / Apply+LF / Remove buttons disabled in N-K and
  Side-by-side modes in the Data Explorer.
- **LF triggering**: explicit "Run N-K Load Flow" button in the sidebar,
  greyed out until an N-K variant has been built.
- **Host scope, first step**: Streamlit only. The backbone is extended to take
  `variant_id` so PySide6 (`qt/`) and NiceGUI (`web/`) can follow later with
  minimal extra work — but their tabs keep behaving as today until then.

## The one rule (carry-over from [AGENTS.md §1](../AGENTS.md))

`set_working_variant` mutates the network's "current variant" globally. Doing
`set_working_variant("N-K")` and `get_lines(...)` as **separate** worker
round-trips is unsafe — an interleaved Streamlit rerun (or another host's
event loop) can swap the variant between them. **Every variant-scoped fetch
must do switch+work+restore inside a single `run(...)` call.** Between worker
calls the working variant is always `"InitialState"`.

## Architecture additions

### New host-agnostic module: `iidm_viewer/variants.py`

The variant manager surface is host-agnostic; it sits alongside
`network_loader.py` / `loadflow.py` so all three hosts can import it.

```python
NK_VARIANT_ID = "N-K"
INITIAL_VARIANT_ID = "InitialState"

def build_contingency_variant(network, contingency, target_variant=NK_VARIANT_ID) -> None:
    """Clone the working variant into target_variant and disconnect the
    contingency's element_ids. One run(): clone + set + update_* + restore."""

def run_loadflow_on_variant(
    network, variant_id, *, generic_params=None, provider_params=None
) -> LoadFlowResult:
    """Set working variant, call loadflow.run_ac, restore. Returns a
    LoadFlowResult exactly like loadflow.run_ac."""

def drop_variant(network, variant_id=NK_VARIANT_ID) -> None:
    """If working variant is variant_id, restore InitialState first; then
    remove variant_id from the variant manager."""

def list_variants(network) -> list[str]:
    """Return the variant ids known to the network."""

def fetch_for_variant(network, fn_name: str, variant_id: str, *args, **kwargs):
    """Atomic switch + getattr(raw, fn_name)(...) + restore inside one run().
    The single primitive every variant-aware getter delegates to."""
```

Disconnection inside `build_contingency_variant` uses
`update_lines` / `update_2_windings_transformers` /
`update_3_windings_transformers` with `connected1=False, connected2=False`,
and `update_generators` with `connected=False`. Element ids are split by type
using the type lookups in `security_analysis.py` (`_MANUAL_TYPE_IDS_KEY` and
the `_get_ids` helper).

### Variant-aware backbone extensions (signature growth only)

Each accepts an optional `variant_id` keyword. When `variant_id is None` or
`"InitialState"`, the existing fast path runs unchanged (no extra worker
round-trip, no behaviour change for anything not yet variant-aware).

| Module | Function | Where today's call lives |
|---|---|---|
| `component_registry.py` | `get_dataframe(network, component, *, variant_id=None) -> DataFrame` | currently single-variant |
| `data_view.py` | `get_enriched_dataframe(network, component, *, variant_id=None) -> DataFrame` | `data_view.py:297` |
| `diagram_services.py` | `generate_sld(network, container_id, *, variant_id=None, …) -> (svg, meta)` | `diagram_services.py:31-55` |
| `reactive_curves.py` | `build_reactive_curves_view_model(network, …, *, variant_id=None) -> ReactiveCurvesViewModel` | `reactive_curves.py:506-579` |
| `operational_limits.py` | `build_operational_limits_view_model(network, …, *, variant_id=None) -> OperationalLimitsViewModel` | `operational_limits.py:255-303` |
| `operational_limits.py` | `get_current_flows(network, *, variant_id=None)` | `operational_limits.py:85-93` |
| `operational_limits.py` | `compute_loading(network, limits_reset, *, variant_id=None)` | `operational_limits.py:119-171` |

Internally these all delegate to `variants.fetch_for_variant` (or its result),
so the atomic switch+restore lives in exactly one place.

### Streamlit cache changes (`iidm_viewer/caches.py`)

The Streamlit cache layer becomes variant-keyed. PySide6 and NiceGUI have
their own caching strategies; the backbone primitives above don't know about
session state.

- `_lf_gen` becomes `dict[str, int]` keyed by variant id;
  `_lf_gen(variant_id="InitialState")` reads it.
- `_cache_key(network, variant_id="InitialState")` returns
  `(net_key, _lf_gen(variant_id), variant_id)`.
- Single-entry caches in `_LOAD_FLOW_CACHE_KEYS` (`_lines_all_cache`,
  `_2wt_all_cache`, `_buses_all`, `_generators_all_cache`, `_3wt_all_cache`,
  `_bus_voltages_cache`, `_loading_cache`, …) become dicts keyed by
  `variant_id`. The dict-shaped caches (`_de_component_cache`,
  `_enriched_component_cache`, `_ext_df_cache`) just take a longer key.
- New thin wrappers: `get_lines_all_for_variant`, `get_2wt_all_for_variant`,
  `get_buses_all_for_variant`, `get_generators_all_for_variant`,
  `get_component_df_for_variant`, `get_enriched_component_for_variant`. They
  short-circuit to today's getter when `variant_id == "InitialState"` so the
  base path takes no extra round-trip.
- `invalidate_on_load_flow(variant_id="InitialState")` bumps only that
  variant's generation counter and pops only that variant's slot.
- `invalidate_on_topology_change` also calls
  `state._invalidate_topology(network)` (new wrapper) that drops the N-K
  variant via `variants.drop_variant(network)` and clears
  `_nk_contingency` / `_nk_variant_id`. Every existing call site in `state.py`
  is rerouted through this wrapper. Topology change ⇒ contingency element ids
  may dangle.
- `invalidate_on_network_replace` pops `_NK_CACHE_KEYS` and resets
  `_lf_gen = {"InitialState": 0}`. Does **not** call `drop_variant` because
  the underlying raw network is being released anyway.

### Session state keys (Streamlit, new)

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

## Sidebar (`iidm_viewer/app.py`)

Add a collapsed expander after the existing "Run AC Load Flow" block:

```
[N-K Variant]
  ── render_manual_contingency_picker(network, key_prefix="nk_pick")
  [Build N-K]    [Run N-K Load Flow]   ← second disabled until variant exists
  N-K LF status pill
  [Clear N-K]
```

Build N-K calls `variants.build_contingency_variant(network, contingency)` and
sets `_nk_variant_id = "N-K"`. Run N-K LF calls
`variants.run_loadflow_on_variant(network, "N-K", generic_params=…, provider_params=…)`
using the same `get_lf_parameters()` dialog values as the base LF button, then
calls `caches.invalidate_on_load_flow(variant_id="N-K")` and updates
`_nk_lf_status`. Clear N-K calls `variants.drop_variant(network)` and pops the
`_nk_*` keys.

## Picker refactor

The manual contingency picker today lives entirely in
`security_analysis_tab.py:255-356` and writes into the session-state list
`_sa_manual_contingencies`. Extract a small host-agnostic view-model into
`security_analysis.py` so the Streamlit sidebar and the SA tab both consume
the same data shape:

```python
# security_analysis.py (new)
def normalize_manual_contingency(
    element_type: str,
    element_ids: list[str],
    grouping: str,           # "single" | "per_element"
    group_id: str | None,
) -> list[dict]:
    """Return [{id, element_ids}, …] in the canonical contingency shape."""
```

What stays in `security_analysis_tab.py`: the Streamlit form (selectbox,
filters, multiselect, submit, preview/remove list, append into
`_sa_manual_contingencies`).

What's added: a thin Streamlit helper
`render_manual_contingency_picker(network, key_prefix) -> dict | None` that
reuses the same widget bodies but emits a single contingency dict (not a list)
on submit. The N-K sidebar uses this directly. The SA tab keeps its current
list-accumulation behaviour by appending whatever the helper returns.

PySide6 / NiceGUI hosts get their own picker widgets later but call the same
`normalize_manual_contingency()` core, so the contingency dict shape stays
consistent across hosts.

## Per-tab changes (Streamlit, first step)

A small helper in `iidm_viewer/components.py`:

```python
def render_view_mode_radio(key: str) -> Literal["N", "N-K", "Side-by-side"]:
    """Radio at the top of an affected tab. Greyed out (returns "N") until
    st.session_state["_nk_variant_id"] is set."""
```

### Data Explorer (`iidm_viewer/data_explorer.py`)

- Insert `view_mode = render_view_mode_radio("_de_view_mode")` after the
  component-type selectbox (around `data_explorer.py:1180`).
- Replace `df = get_enriched_component(network, method_name)` (currently
  `data_explorer.py:1205`) with a dispatch on `view_mode`. Internally each
  branch calls `caches.get_enriched_component_for_variant(network, method_name, variant_id)`.
  Side-by-side renders two `st.data_editor`s in `st.columns(2)`; the N-K
  column uses `disabled=True`.
- Apply / Apply+LF / Remove (around `data_explorer.py:1307-1308` for apply,
  the creation/delete forms around `1196-1220`) gated on `view_mode == "N"`.
  Caption when hidden: "N-K data is read-only in the first step".

### SLD (`iidm_viewer/diagrams.py`)

- Insert `view_mode = render_view_mode_radio("_sld_view_mode")` after the
  early-return on missing VL (around `diagrams.py:309`).
- Cache key changes from `container_id` (current `diagrams.py:343-355`) to
  `(container_id, variant_id)`. The fetch goes through
  `diagram_services.generate_sld(network, container_id, variant_id=...)`;
  internally that wraps `set_working_variant` + `get_single_line_diagram` +
  restore inside one `run(...)`.
- Side-by-side renders two columns, each with its own
  `render_interactive_sld(svg, metadata, key=f"sld_{container_id}_{variant_id}")`.
- Click handlers only mutate the network when the click came from the N-side
  viewer; N-K clicks are ignored.

### Reactive Capability Curves (`iidm_viewer/reactive_curves_tab.py`)

- Insert `view_mode = render_view_mode_radio("_rcc_view_mode")` at the top.
- Build a view-model per active variant: today's single
  `build_reactive_curves_view_model(network, …)` call becomes one or two
  calls with `variant_id="InitialState"` and (if `view_mode != "N"`)
  `variant_id="N-K"`. The capability polygon (drawn from
  `caches.get_reactive_curve_points`) is topology-only and shared.
- Plotly traces (`reactive_curves_tab.py:183-226`):
  - polygon (lines 186-192) — single trace shared across variants
  - operating-point marker (lines 196-201) — two markers in N-K and
    Side-by-side modes: N as red `x`, N-K as blue `cross`
  - target marker (lines 205-217) — two diamonds with the existing status
    colour, distinguishable by legend (`"Target N (...)"`, `"Target N-K (...)"`)
- Containment-summary tables (`reactive_curves.py:703-773`) gain a `variant`
  column or are rendered twice side-by-side.

### Operational Limits (`iidm_viewer/operational_limits_tab.py`)

- Insert `view_mode = render_view_mode_radio("_oplim_view_mode")` at the top.
- `compute_loading(network, limits_reset)`
  (`operational_limits.py:119-171`) gains `variant_id`; cache key extends with
  it. Internally `get_current_flows(network)` (`operational_limits.py:85-93`)
  also gains `variant_id` and delegates to
  `caches.get_lines_all_for_variant` / `get_2wt_all_for_variant`.
- "Most loaded" table (`operational_limits_tab.py:124-144`) in side-by-side
  shows columns `[Element, Type, Side, I_N, I_NK, Limit, Load%_N, Load%_NK, ΔLoad%]`
  sorted by `ΔLoad%` desc (worst contingency impact first).
- `build_element_chart(element_id, elem_df, current_flow)`
  (`operational_limits.py:177-232`) takes an optional `current_flow_nk`;
  emits a second `add_hline` per side in a different colour/dash. Title:
  `f"Current limits — {element_id} (N vs N-K)"`.

## Critical files to modify

- `iidm_viewer/variants.py` *(new)* — `build_contingency_variant`,
  `run_loadflow_on_variant`, `drop_variant`, `list_variants`,
  `fetch_for_variant`, constants `INITIAL_VARIANT_ID`, `NK_VARIANT_ID`.
- `iidm_viewer/component_registry.py` — `get_dataframe` gains `variant_id`.
- `iidm_viewer/data_view.py` — `get_enriched_dataframe` gains `variant_id`.
- `iidm_viewer/diagram_services.py` — `generate_sld` gains `variant_id`.
- `iidm_viewer/reactive_curves.py` — `build_reactive_curves_view_model` (and
  the inner helpers it calls) gain `variant_id`.
- `iidm_viewer/operational_limits.py` — `get_current_flows`,
  `compute_loading`, `build_operational_limits_view_model`,
  `build_element_chart` gain `variant_id` / `current_flow_nk`.
- `iidm_viewer/caches.py` — variant-keyed `_lf_gen` and `_cache_key`;
  per-variant getter wrappers; updated invalidation; `_NK_CACHE_KEYS`.
- `iidm_viewer/state.py` — new `_invalidate_topology(network)` wrapper that
  also calls `variants.drop_variant`; reroute every existing
  `invalidate_on_topology_change()` call through it.
- `iidm_viewer/security_analysis.py` — `normalize_manual_contingency` core.
- `iidm_viewer/security_analysis_tab.py` — extract
  `render_manual_contingency_picker` from the embedded form
  (`security_analysis_tab.py:255-356`).
- `iidm_viewer/app.py` — sidebar "N-K Variant" expander.
- `iidm_viewer/components.py` — `render_view_mode_radio`.
- `iidm_viewer/data_explorer.py` — view-mode radio, side-by-side editors,
  gating of mutating actions.
- `iidm_viewer/diagrams.py` — view-mode radio, per-variant SLD cache.
- `iidm_viewer/reactive_curves_tab.py` — view-mode radio, per-variant
  operating points + targets.
- `iidm_viewer/operational_limits_tab.py` — view-mode radio, per-variant
  loading, dual current line in chart, dual-loading-% table.

PySide6 (`iidm_viewer/qt/*`) and NiceGUI (`iidm_viewer/web/app.py`) are
**unchanged in the first step**. Their tabs continue to call the backbone
without `variant_id`, which defaults to `None` / `"InitialState"`.

## Out-of-scope tabs (must keep working unchanged)

Overview, Network Area Diagram, Network Map, Pmax, Voltage Analysis, Injection
Map, Extensions Explorer, Short Circuit Analysis, Network Reduction. All read
their data via the default-`InitialState` path (no `variant_id`), which is
the existing path unchanged.

## Risks and mitigations

- **Variant cleanup on network replace.** N-K variant lives on the OLD raw
  network; that handle is released by `network_loader.load_from_*`.
  `invalidate_on_network_replace` pops `_NK_CACHE_KEYS` and resets `_lf_gen`;
  do **not** call `variants.drop_variant` on the dying network.
- **Topology edits dangle the contingency.** Every site that calls
  `invalidate_on_topology_change()` (24 hits in `state.py`) is rerouted
  through `state._invalidate_topology(network)` which also calls
  `variants.drop_variant` and clears the contingency keys. Show a one-time
  toast "N-K variant cleared because the base network changed."
- **Thread safety of `set_working_variant`.** Enforced by code review and a
  unit test `test_variant_state_is_restored_after_fetch`: every variant
  operation must be atomic inside a single `run(...)`. The
  `variants.fetch_for_variant` primitive is the canonical place where this
  happens; every backbone variant-aware getter delegates to it. Between
  worker calls the working variant is always `"InitialState"` (invariant).
- **Cache memory growth.** Per-variant dicts cap at 2 entries
  (`InitialState`, `N-K`). `variants.drop_variant` pops the `"N-K"` slot
  from every variant-keyed cache.
- **Cross-host drift.** Adding `variant_id` to the backbone with a default of
  `None` keeps PySide6 / NiceGUI untouched. Their cores already call the
  backbone with positional args; the new param is keyword-only.

## Verification plan

### Unit tests (new / extended in `tests/`)

- `tests/test_variants.py` *(new)*:
  `test_build_contingency_variant_creates_variant`,
  `test_run_loadflow_on_variant_only_affects_target` (assert N-side `p`/`q`
  are unchanged after N-K LF), `test_variant_state_is_restored_after_fetch`,
  `test_drop_variant_clears_nk`.
- `tests/test_state.py`: `test_topology_change_drops_nk_variant`.
- `tests/test_caches.py`: variant-keyed `_lf_gen`, dict-shaped caches, slot
  popping on `drop_variant`.
- `tests/test_data_view.py`, `tests/test_reactive_curves.py`,
  `tests/test_operational_limits.py`, `tests/test_diagrams.py`: assert
  default-path behaviour is identical to today; assert
  `variant_id="InitialState"` matches `variant_id=None`; add a
  `variant_id="N-K"` case that requires a built variant.
- `tests/test_security_analysis.py`: standalone
  `render_manual_contingency_picker` returns the same dicts as the embedded
  path.

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

1. **Backbone plumbing** (no UI change yet): add `iidm_viewer/variants.py`
   with `fetch_for_variant`, `build_contingency_variant`,
   `run_loadflow_on_variant`, `drop_variant`. Unit tests for the atomic
   switch+restore invariant. No callers yet.
2. **Variant-aware backbone signatures**: extend
   `component_registry.get_dataframe`, `data_view.get_enriched_dataframe`,
   `diagram_services.generate_sld`,
   `reactive_curves.build_reactive_curves_view_model`,
   `operational_limits.build_operational_limits_view_model` (and the inner
   helpers) with a keyword-only `variant_id=None`. No behaviour change for
   callers passing nothing. Tests assert parity.
3. **Streamlit cache layer**: turn `_lf_gen` and the affected caches in
   `caches.py` into variant-keyed dicts; add the
   `*_for_variant` wrappers; update invalidation entry points; reroute
   topology invalidation through `state._invalidate_topology`.
4. **Picker refactor**: pull the Streamlit manual picker out of
   `security_analysis_tab.py` into a small reusable
   `render_manual_contingency_picker` and a host-agnostic
   `normalize_manual_contingency` core. Prove the SA tab behaves identically.
5. **Sidebar wiring**: N-K Variant expander with Build / Run N-K LF / Clear
   buttons.
6. **Per-tab rollout**, one tab per commit: Reactive Curves → Operational
   Limits → Data Explorer → SLD.
7. **End-to-end Playwright run** from AGENTS.md §2 with the N-K scenario.

PySide6 and NiceGUI parity is deliberately deferred. Once the backbone is
variant-aware, each host needs only its own view-mode toggle + a way to
trigger build / run / clear N-K — the data path is already in place.
