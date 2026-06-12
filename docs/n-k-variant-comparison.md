# N vs N-K state comparison via pypowsybl variant manager

Status: **fully delivered** as of mid-2026 across all three hosts
(Streamlit, PySide6, NiceGUI). Side-by-side view available on every
affected tab (Reactive Curves, Operational Limits, Data Explorer, SLD).

For what's **left** to do — first-paint latency on large networks,
end-to-end Playwright tests, script-recorder hooks for the N-K
lifecycle, cache adoption in the prototype hosts, and other
follow-ups — see [`n-k-followups.md`](n-k-followups.md).

The body of this document is the **original design plan** that the
implementation was built against. Use it as background for the design
decisions behind the shipped code (the atomic switch+restore rule,
the per-variant `_lf_gen` dict, the `NK_CACHE_KEYS` set, etc.).

---

Park for later: design doc for adding side-by-side N (base) vs N-K
(post-contingency) comparison to the viewer using pypowsybl's variant manager.
Status: planning only, no code yet. Targets all three hosts:
Streamlit (`app.py` + the legacy tab modules), PySide6 (`iidm_viewer/qt/`)
and NiceGUI (`iidm_viewer/web/`). Cache layers differ across hosts — see
"Hosts and their cache layers" below.

**Related**: [`host-sharing.md`](host-sharing.md) is the broader cross-host
unification plan. Steps 1–3 of that plan (CacheBackend, AppState base,
change-log unification) are recommended prerequisites — they collapse the
per-host bookkeeping work this doc describes into single backbone changes.

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

Note: variants share topology — they are not a structural-undo mechanism.
For N-K that's fine: we mutate connection state, never topology.

## Decisions taken

- **Contingency picker**: reuse the existing manual picker (currently embedded
  in `iidm_viewer/security_analysis_tab.py:255-356`). Extract a host-agnostic
  normalizer into `security_analysis.py` and per-host picker widgets.
- **View-mode toggle**: per-tab radio at the top of each affected tab
  (`N | N-K | Side-by-side`), implemented in each host with its native widget.
- **N-K is read-only**: Apply / Apply+LF / Remove buttons disabled in N-K and
  Side-by-side modes in the Data Explorer.
- **LF triggering**: explicit "Run N-K Load Flow" button (greyed out until an
  N-K variant has been built), placed wherever the host already has the base
  "Run AC Load Flow" button.
- **Host scope**: all three hosts. Backbone wiring done once; per-host UI +
  cache adaptation done per host.

## The one rule (carry-over from [AGENTS.md §1](../AGENTS.md))

`set_working_variant` mutates the network's "current variant" globally. Doing
`set_working_variant("N-K")` and `get_lines(...)` as **separate** worker
round-trips is unsafe — an interleaved Streamlit rerun, a Qt signal handler,
or a NiceGUI event-loop tick can swap the variant between them. **Every
variant-scoped fetch must do switch+work+restore inside a single `run(...)`
call.** Between worker calls the working variant is always `"InitialState"`.

## Hosts and their cache layers

Verified by audit of caches.py + qt/state.py + web/state.py + per-tab cores:

| Layer | Streamlit | PySide6 (`qt/`) | NiceGUI (`web/`) |
|---|---|---|---|
| Per-tab cores (`component_registry`, `data_view`, `diagram_services`, `reactive_curves`, `operational_limits`) | **stateless**, worker-routed | same | same |
| Raw DataFrame cache (lines, gens, buses, …) | `caches.py` keyed by `(net_key, _lf_gen)` in `st.session_state` | **none** — re-fetched each refresh | **none** — re-fetched each refresh |
| SVG cache (SLD / NAD) | `st.session_state["_sld_cache"]` keyed by `container_id` | tab-local dict `sld_tab._cache: {container_id: …}` | module-level `_sld_cache: {container_id: …}` in `web/app.py` |
| Generation counter (`_lf_gen`) | yes, `st.session_state["_lf_gen"]: int` | none | none |
| Session-state object | `st.session_state` (per-tab strings) | `qt.state.AppState(QObject)` with signals | `web.state.AppState` with observer callbacks |

Implications:

- The host-agnostic backbone primitives go in **one** new module
  (`iidm_viewer/variants.py`) and the per-tab cores grow a keyword-only
  `variant_id=None`. No host-specific code in either place.
- `caches.py` keeps its current role — variant-keying lives there for
  Streamlit only.
- Qt and NiceGUI need only re-key their **SVG caches** by
  `(container_id, variant_id)`. They have no DataFrame cache to extend, so
  every variant DF fetch is one atomic worker round-trip — that matches their
  existing cache-light philosophy.
- Each host extends its own `AppState` / `session_state` with the N-K keys.

## Backbone (host-agnostic)

### New module: `iidm_viewer/variants.py`

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

No streamlit / PySide6 / NiceGUI imports in this module.

### Variant-aware core signatures (signature growth only)

Each accepts an optional keyword `variant_id=None`. When `variant_id is None`
or `"InitialState"`, the existing fast path runs unchanged — no extra worker
round-trip, no behaviour change for any host until it starts passing the new
keyword.

| Module | Function | Where today's call lives |
|---|---|---|
| `component_registry.py` | `get_dataframe(network, component, *, variant_id=None) -> DataFrame` | `component_registry.py:174-198` |
| `data_view.py` | `get_enriched_dataframe(network, component, *, variant_id=None) -> DataFrame` | `data_view.py:297-311` |
| `diagram_services.py` | `generate_sld(network, container_id, *, variant_id=None, …) -> (svg, meta)` | `diagram_services.py:31-55` |
| `reactive_curves.py` | `build_reactive_curves_view_model(network, …, *, variant_id=None) -> ReactiveCurvesViewModel` | `reactive_curves.py:506-579` |
| `reactive_curves.py` | `augment_gens_with_step_up_transformer(network, *, variant_id=None)`, `augment_gens_with_bus_voltage(network, *, variant_id=None)` | inner helpers used by the view-model builder |
| `operational_limits.py` | `get_current_flows(network, *, variant_id=None)` | `operational_limits.py:85-93` |
| `operational_limits.py` | `compute_loading(network, limits_reset, *, variant_id=None)` | `operational_limits.py:119-171` |
| `operational_limits.py` | `build_operational_limits_view_model(network, …, *, variant_id=None)` | `operational_limits.py:255-303` |

Internally these delegate to `variants.fetch_for_variant` (or to a private
helper that does). The atomic switch+restore lives in exactly one place.

### Picker normalizer (host-agnostic)

Add to `iidm_viewer/security_analysis.py`:

```python
def normalize_manual_contingency(
    element_type: str,
    element_ids: list[str],
    grouping: str,           # "single" | "per_element"
    group_id: str | None,
) -> list[dict]:
    """Return [{id, element_ids}, …] in the canonical contingency shape."""
```

Each host renders its own widget tree but delegates the
selection-to-contingency-dict translation here. The dict shape is identical
across hosts so `variants.build_contingency_variant` consumes it uniformly.

## Streamlit host

### Session state keys (new in `st.session_state`)

| Key | Type | Lifecycle |
|---|---|---|
| `_nk_contingency` | `dict` `{id, element_ids}` or `None` | set by Build N-K; cleared on network replace, on any topology edit, on Clear N-K |
| `_nk_variant_id` | `"N-K"` or `None` | set after a successful clone |
| `_nk_lf_status` | `"NEVER" \| "CONVERGED" \| "FAILED" \| "DIVERGED"` | updated by Run N-K LF |
| `_nk_lf_report_json` | `str` | mirrors `_lf_report_json` |
| `_lf_gen` | becomes `dict[str, int]` keyed by variant id | per-variant LF generation counter |
| `_de_view_mode`, `_sld_view_mode`, `_rcc_view_mode`, `_oplim_view_mode` | `"N" \| "N-K" \| "Side-by-side"` | one per affected tab; defaults to `"N"` |

A new `_NK_CACHE_KEYS` tuple in `caches.py` lists these so
`invalidate_on_network_replace` pops them all.

### `caches.py` extension

- `_lf_gen` becomes `dict[str, int]` keyed by variant id;
  `_lf_gen(variant_id="InitialState")` reads it.
- `_cache_key(network, variant_id="InitialState")` returns
  `(net_key, _lf_gen(variant_id), variant_id)`.
- Single-entry caches in `_LOAD_FLOW_CACHE_KEYS` (`_lines_all_cache`,
  `_2wt_all_cache`, `_buses_all`, `_generators_all_cache`, `_3wt_all_cache`,
  `_bus_voltages_cache`, `_loading_cache`, …) become dicts keyed by
  `variant_id`. Dict-shaped caches (`_de_component_cache`,
  `_enriched_component_cache`, `_ext_df_cache`) just take a longer key.
- New thin wrappers: `get_lines_all_for_variant`, `get_2wt_all_for_variant`,
  `get_buses_all_for_variant`, `get_generators_all_for_variant`,
  `get_component_df_for_variant`, `get_enriched_component_for_variant`. They
  short-circuit to today's getter when `variant_id == "InitialState"`.
- `invalidate_on_load_flow(variant_id="InitialState")` bumps only that
  variant's generation counter and pops only that variant's slot.
- `invalidate_on_topology_change` calls a new `state._invalidate_topology(network)`
  wrapper that also calls `variants.drop_variant(network)` and clears the
  `_nk_*` keys.
- `invalidate_on_network_replace` pops `_NK_CACHE_KEYS` and resets
  `_lf_gen = {"InitialState": 0}`. Does **not** call `drop_variant` (the raw
  handle is being released anyway).

### Sidebar (`iidm_viewer/app.py`)

Add a collapsed expander after the existing "Run AC Load Flow" block:

```
[N-K Variant]
  ── render_manual_contingency_picker_streamlit(network, key_prefix="nk_pick")
  [Build N-K]    [Run N-K Load Flow]   ← second disabled until variant exists
  N-K LF status pill
  [Clear N-K]
```

`render_manual_contingency_picker_streamlit` is a thin wrapper extracted from
`security_analysis_tab.py:255-356` that reuses the same widgets but emits a
single contingency dict (via `security_analysis.normalize_manual_contingency`)
instead of appending to `_sa_manual_contingencies`. The SA tab keeps its
list-accumulation behaviour by appending the dict the helper returns.

### Per-tab changes

Small helper in `iidm_viewer/components.py`:

```python
def render_view_mode_radio(key: str) -> Literal["N", "N-K", "Side-by-side"]:
    """Radio at the top of an affected tab. Greyed out (returns "N") until
    st.session_state["_nk_variant_id"] is set."""
```

- **Data Explorer** (`data_explorer.py`): radio after the component-type
  selectbox (~line 1180); dispatch `get_enriched_component(...)` (line 1205)
  to `get_enriched_component_for_variant` per branch; side-by-side renders
  two `st.data_editor`s in `st.columns(2)`, N-K column `disabled=True`. Apply
  / Apply+LF / Remove (lines 1307-1308 and ~1196-1220) gated on
  `view_mode == "N"`.
- **SLD** (`diagrams.py`): radio after the early-return on missing VL (~line
  309). Cache key (currently `container_id` at lines 343-355) becomes
  `(container_id, variant_id)`. The fetch goes through
  `diagram_services.generate_sld(network, container_id, variant_id=...)`.
  Side-by-side renders two columns. N-K clicks ignored.
- **Reactive Capability Curves** (`reactive_curves_tab.py`): radio at the
  top; one or two `build_reactive_curves_view_model(network, …, variant_id=…)`
  calls; Plotly traces (`reactive_curves_tab.py:183-226`) extended:
  polygon shared, operating point (lines 196-201) emits two markers in
  Side-by-side mode (N as red `x`, N-K as blue `cross`), target diamond
  (lines 205-217) emits two with distinct legends.
- **Operational Limits** (`operational_limits_tab.py`): radio at the top;
  per-variant `compute_loading`; the loading table (lines 124-144) in
  Side-by-side shows
  `[Element, Type, Side, I_N, I_NK, Limit, Load%_N, Load%_NK, ΔLoad%]`
  sorted by ΔLoad% desc; `build_element_chart` (lines 177-232) accepts
  `current_flow_nk` and emits a second `add_hline` per side.

## PySide6 host (`iidm_viewer/qt/`)

### State extension (`qt/state.py:AppState`)

Add to `AppState`:

```python
# new properties
@property
def nk_contingency(self) -> dict | None
@property
def nk_variant_id(self) -> str | None
@property
def nk_lf_status(self) -> str

# new signals
nk_variant_changed = Signal(object)      # emits the new nk_variant_id or None
nk_loadflow_completed = Signal(object)   # emits LoadFlowResult

# new methods (all worker-routed via variants.*)
def build_nk_variant(self, contingency: dict) -> None
def run_nk_loadflow(self, generic_params=None, provider_params=None) -> LoadFlowResult | None
def clear_nk_variant(self) -> None
```

`install_network` clears `_nk_*` (the old raw handle is gone).
`notify_network_changed` after a topology mutation calls
`variants.drop_variant(network)` and emits `nk_variant_changed(None)`.

### SVG cache re-keying

Each tab's local SVG cache grows a `variant_id` dimension:

- `qt/sld_tab.py:43` `_cache: dict[str, tuple[str, str]]` →
  `dict[tuple[str, str], tuple[str, str]]` keyed by `(container_id, variant_id)`.
- `qt/nad_tab.py:43` same treatment (out of scope for the first step's UI but
  the cache key change is trivial and prevents stale SVGs when a variant is
  later wired in).

### UI placement

- **Picker + Build / Run N-K LF / Clear**: new dock widget
  `qt/nk_variant_dock.py` (analogous to the existing `change_log_panel.py`),
  registered in `qt/main_window.py` alongside the other docks. The dock's
  picker widget calls
  `security_analysis.normalize_manual_contingency` and
  `state.AppState.build_nk_variant`.
- **Per-tab view-mode toggle**: each affected tab gains a `QComboBox` (or a
  `QButtonGroup` of three `QRadioButton`s) at the top, mirroring the
  Streamlit per-tab radio. The combo's `currentTextChanged` signal triggers
  the tab's `refresh()`.
- **Side-by-side layout**: each affected tab wraps its existing single-state
  widget in a `QSplitter(Qt.Horizontal)`. The second pane is created lazily
  the first time the user picks Side-by-side mode.

### Per-tab changes

- `qt/data_explorer_tab.py` — view-mode combo; in Side-by-side, two
  `QTableView`s sharing the same `_ComponentFilterProxy`-style stack but
  pointed at two view-models built from
  `data_view.get_enriched_dataframe(network, component, variant_id=…)`.
  N-K table is read-only; the existing "Apply" / "Disconnect" / "Delete"
  toolbar buttons are disabled when `view_mode != "N"`.
- `qt/sld_tab.py` — view-mode combo; the `QWebEngineView` is moved into a
  `QSplitter`; in Side-by-side a second `QWebEngineView` is created and
  populated with the N-K SVG. Click events on the N-K view are intercepted
  and ignored (with a one-time status-bar hint).
- `qt/reactive_curves_tab.py` — view-mode combo; build one or two
  `ReactiveCurvesViewModel`s; the `QWebEngineView` rendering the Plotly HTML
  receives the merged figure (two operating-point markers + two target
  markers in N-K / Side-by-side). The four containment tables grow a
  `variant` column or render side-by-side.
- `qt/operational_limits_tab.py` — view-mode combo; per-variant
  `compute_loading`; loading `QTableView` model gains the dual columns; the
  per-element bar chart `QWebEngineView` receives the chart with two
  `add_hline` overlays.

## NiceGUI host (`iidm_viewer/web/`)

### State extension (`web/state.py:AppState`)

Same field additions and observer callbacks as Qt, in the dataclass-with-
listeners style of the existing `on_network_changed` / `on_selected_vl_changed`
/ `on_loadflow_completed` registration helpers:

```python
def on_nk_variant_changed(self, listener) -> None
def on_nk_loadflow_completed(self, listener) -> None

def build_nk_variant(self, contingency: dict) -> None
def run_nk_loadflow(self, ...) -> LoadFlowResult | None
def clear_nk_variant(self) -> None
```

`install_network` resets the `_nk_*` fields. After a topology-mutation
`notify_network_changed` calls `variants.drop_variant(network)`.

### SVG cache re-keying

`web/app.py:131-132` `_sld_cache: dict[str, …]` and
`_nad_cache: dict[tuple[str, int], …]` become
`dict[tuple[str, str], …]` and `dict[tuple[str, int, str], …]` respectively
(third element = `variant_id`).

### UI placement

- **Picker + Build / Run N-K LF / Clear**: new collapsible card / sidebar
  section in `web/app.py` next to the existing "Run AC Load Flow" button.
  Picker reuses `security_analysis.normalize_manual_contingency`.
- **Per-tab view-mode toggle**: each affected NiceGUI tab grows an
  `ui.toggle(["N", "N-K", "Side-by-side"])` at the top.
- **Side-by-side layout**: NiceGUI `ui.row()` with two `ui.column()` halves.

### Per-tab changes

NiceGUI's data explorer (`web/app.py:_build_data_explorer`, line ~3382), SLD
(`_set_sld`, line ~), reactive curves and operational limits tabs grow the
toggle and side-by-side rendering, calling the same variant-aware backbone
functions as Streamlit and Qt.

## Critical files to modify

Backbone (host-agnostic):

- `iidm_viewer/variants.py` *(new)*
- `iidm_viewer/component_registry.py` — `get_dataframe` keyword
- `iidm_viewer/data_view.py` — `get_enriched_dataframe` keyword
- `iidm_viewer/diagram_services.py` — `generate_sld` keyword
- `iidm_viewer/reactive_curves.py` — view-model builder + inner helpers
- `iidm_viewer/operational_limits.py` — flows, loading, view-model builder,
  per-element chart
- `iidm_viewer/security_analysis.py` — `normalize_manual_contingency`

Streamlit host:

- `iidm_viewer/caches.py` — variant-keyed
- `iidm_viewer/state.py` — `_invalidate_topology(network)` wrapper
- `iidm_viewer/app.py` — sidebar "N-K Variant" expander
- `iidm_viewer/components.py` — `render_view_mode_radio`
- `iidm_viewer/security_analysis_tab.py` — extract
  `render_manual_contingency_picker_streamlit`
- `iidm_viewer/data_explorer.py`, `iidm_viewer/diagrams.py`,
  `iidm_viewer/reactive_curves_tab.py`, `iidm_viewer/operational_limits_tab.py`
  — view-mode radio + side-by-side rendering

PySide6 host:

- `iidm_viewer/qt/state.py` — N-K fields, signals, methods
- `iidm_viewer/qt/nk_variant_dock.py` *(new)* — picker dock
- `iidm_viewer/qt/main_window.py` — register the dock
- `iidm_viewer/qt/security_analysis_tab.py` — share picker via
  `normalize_manual_contingency`
- `iidm_viewer/qt/data_explorer_tab.py`, `iidm_viewer/qt/sld_tab.py`,
  `iidm_viewer/qt/reactive_curves_tab.py`,
  `iidm_viewer/qt/operational_limits_tab.py` — combo + side-by-side + SVG
  cache re-key

NiceGUI host:

- `iidm_viewer/web/state.py` — N-K fields, observer callbacks, methods
- `iidm_viewer/web/app.py` — picker card, per-tab toggles, SVG cache re-key,
  side-by-side layouts in the four affected tab builders

## Out-of-scope tabs (must keep working unchanged across all three hosts)

Overview, Network Area Diagram, Network Map, Pmax, Voltage Analysis, Injection
Map, Extensions Explorer, Short Circuit Analysis, Network Reduction. They
read data via the default-`InitialState` path (no `variant_id`) and don't
touch any of the new session keys.

## Risks and mitigations

- **Variant cleanup on network replace.** The N-K variant lives on the OLD
  raw network; that handle is released on load. Each host's `install_network`
  (Streamlit `state.load_network`, Qt `AppState.install_network`, NiceGUI
  `AppState.install_network`) pops the `_nk_*` state without calling
  `variants.drop_variant` on the dying network.
- **Topology edits dangle the contingency.** Streamlit reroutes every
  `invalidate_on_topology_change()` call through
  `state._invalidate_topology(network)`. Qt/NiceGUI route every topology
  mutation through `AppState.notify_network_changed` which calls
  `variants.drop_variant(network)` and emits `nk_variant_changed(None)`.
  Each host surfaces a one-time toast / status message "N-K variant cleared
  because the base network changed."
- **Thread safety of `set_working_variant`.** Enforced by code review and a
  unit test `test_variant_state_is_restored_after_fetch`: every variant
  operation must be atomic inside a single `run(...)`. `variants.fetch_for_variant`
  is the only public switch+work+restore primitive; every variant-aware
  getter delegates to it. Between worker calls the working variant is always
  `"InitialState"` (invariant).
- **Cache memory growth.** Per-variant dicts cap at 2 entries
  (`InitialState`, `N-K`). `variants.drop_variant` pops the `"N-K"` slot
  from Streamlit's caches and from each host's SVG caches.
- **Cross-host drift.** All variant logic is in the backbone; per-host code
  is limited to (state, picker UI, view-mode toggle, side-by-side layout,
  SVG-cache re-key). A unit test in `tests/test_variants.py` runs the
  contingency-build + LF + drop sequence headlessly (no host) so the
  backbone behaviour is locked down.

## Verification plan

### Unit tests (new / extended)

- `tests/test_variants.py` *(new, host-agnostic)*:
  `test_build_contingency_variant_creates_variant`,
  `test_run_loadflow_on_variant_only_affects_target` (assert N-side `p`/`q`
  unchanged after N-K LF), `test_variant_state_is_restored_after_fetch`,
  `test_drop_variant_clears_nk`,
  `test_fetch_for_variant_atomic_round_trip`.
- `tests/test_caches.py` *(Streamlit)*: variant-keyed `_lf_gen`,
  dict-shaped caches, slot popping on `drop_variant`.
- `tests/test_state.py` *(Streamlit)*: `test_topology_change_drops_nk_variant`.
- `tests/test_qt_prototype.py`: `test_qt_appstate_nk_variant_lifecycle`,
  `test_qt_sld_cache_keyed_by_variant`.
- `tests/test_nicegui_prototype.py`: equivalent NiceGUI assertions.
- `tests/test_data_view.py`, `tests/test_reactive_curves.py`,
  `tests/test_operational_limits.py`, `tests/test_diagrams.py`: assert
  default-path behaviour identical to today; assert
  `variant_id="InitialState"` matches `variant_id=None`; add a
  `variant_id="N-K"` case.
- `tests/test_security_analysis.py`: `normalize_manual_contingency`
  matches today's embedded path output.

### End-to-end

Streamlit (per [AGENTS.md §2](../AGENTS.md) Playwright recipe):

```
- upload test_ieee14.xiidm
- select a different VL  (segfault canary)
- click "Run AC Load Flow"
- expand sidebar "N-K Variant"; pick Lines / L1-2-1 / id "single_line_outage"
- click "Build N-K"; click "Run N-K Load Flow"
- for tab in [Data Explorer, SLD, Reactive Curves, Operational Limits]:
    click tab; select "Side-by-side"; wait 3s
- assert ps -p $SPID alive AND no "Segmentation fault" in /tmp/streamlit.log
```

PySide6: scripted run via `iidm-viewer-pyside` headless (`QT_QPA_PLATFORM=offscreen`)
that drives the same scenario via `QTest` / explicit slot calls. Assert no
segfault and that N and N-K view-models have different `p`/`q` for the chosen
line.

NiceGUI: Playwright run against `iidm-viewer-nicegui` on a free port. Same
scenario, same assertions.

### Manual smoke

After E2E in each host: edit a generator's `target_p` in N (Apply+LF), verify
N-K is auto-cleared (toast + `_nk_variant_id` is None). Re-build N-K, then
upload a new file — verify all `_nk_*` keys are cleared and the picker UI
resets.

## Suggested implementation order

Each step is independently testable — land as separate commits.

1. **Backbone — variant primitives**: add `iidm_viewer/variants.py` with
   `fetch_for_variant`, `build_contingency_variant`, `run_loadflow_on_variant`,
   `drop_variant`. Unit tests for the atomic switch+restore invariant. No
   callers yet.
2. **Backbone — `variant_id` plumbing**: extend the six core surfaces with
   keyword-only `variant_id=None`. Tests assert parity for the default path.
3. **Picker normalizer**: add `security_analysis.normalize_manual_contingency`
   and prove both the existing Streamlit SA tab and the Qt SA tab can adopt
   it without behaviour change.
4. **Streamlit cache layer**: variant-key `caches.py`; reroute topology
   invalidation through `state._invalidate_topology`.
5. **Streamlit sidebar + per-tab rollout**, one tab per commit: Reactive
   Curves → Operational Limits → Data Explorer → SLD. End-to-end Playwright
   run after the fourth tab.
6. **PySide6 state extension** (`qt/state.py`): add the N-K fields, signals
   and methods; add the SVG-cache re-key in `qt/sld_tab.py`. No UI yet;
   tests in `tests/test_qt_prototype.py`.
7. **PySide6 UI**: add `qt/nk_variant_dock.py`, register in `main_window.py`,
   add the per-tab combo + side-by-side `QSplitter` in the four affected
   tabs. Headless run.
8. **NiceGUI state extension** (`web/state.py`): same as step 6 in the
   NiceGUI observer style; SVG-cache re-key in `web/app.py`.
9. **NiceGUI UI**: picker card, per-tab toggles, side-by-side rendering in
   the four affected NiceGUI tab builders. Playwright run.
