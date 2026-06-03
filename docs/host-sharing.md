# Cross-host sharing: cache backend, AppState base, per-tab view-models

Park for later: design doc for unifying logic across the three iidm-viewer
hosts (Streamlit, PySide6 `qt/`, NiceGUI `web/`) so they share maximum
backbone code and differ only in renderer + per-tab interaction handlers.
Status: planning only, no code yet. Prerequisite (or parallel work) for the
[N-K variant-comparison refactor](n-k-variant-comparison.md).

## Why

Three concurrent audits revealed asymmetries the existing backbone hasn't yet
closed:

1. **Cache layer is Streamlit-only.** `caches.py` (~25 cache slots) is keyed
   by `(net_key, _lf_gen)` in `st.session_state`. Qt holds two tab-local
   `dict[container_id, …]` SVG caches; NiceGUI holds two module-level SVG
   caches plus **15+ scattered `.clear()` calls** across topology-mutation
   handlers. Qt and NiceGUI **re-fetch raw DataFrames on every tab refresh**
   — they have no equivalent of Streamlit's `_lines_all_cache`,
   `_generators_all_cache`, etc.
2. **AppState is fragmented.** Qt's `qt/state.py:AppState` and NiceGUI's
   `web/state.py:AppState` already expose a nearly identical surface
   (`network`, `selected_vl`, `last_report_json`, `change_log`,
   `install_network`, `set_selected_vl`, `run_loadflow`,
   `notify_network_changed`). Streamlit has no `AppState` class — equivalent
   state lives directly in `st.session_state` keys, plus a **second,
   parallel** change-log mechanism (per-method `_change_log_{method_name}`
   lists) that exists alongside but never feeds the shared
   `ChangeLog` class.
3. **Five of thirteen tabs lack a view-model.** Overview, Reactive Curves,
   Voltage Analysis, Operational Limits ship a `ViewModel` dataclass +
   builder that all three hosts consume. Data Explorer, Security Analysis,
   Short Circuit, Pmax, Extensions Explorer, and Injection Map (which mixes
   streamlit imports into its "core") have only helper functions — each host
   re-assembles its own view state. Audit estimated **1,150–1,500 LOC** of
   non-renderer logic could collapse into shared cores.

## Target architecture

```
iidm_viewer/
├── backbone (host-agnostic, no streamlit/PySide6/NiceGUI imports)
│   ├── network_loader.py            already shared
│   ├── loadflow.py                  already shared
│   ├── component_registry.py        already shared
│   ├── data_view.py                 already shared (helpers; needs view-model)
│   ├── diagram_services.py          already shared
│   ├── change_log.py                already shared
│   ├── cache_backend.py             NEW — Protocol + invalidation rules
│   ├── app_state.py                 NEW — base AppState abstract class
│   ├── variants.py                  NEW (see N-K plan)
│   └── <per-tab cores>              network_info_core, voltage_analysis_core,
│                                    operational_limits, reactive_curves +
│                                    new: data_explorer_core,
│                                    security_analysis_core (rename),
│                                    short_circuit_core, pmax_core,
│                                    extensions_core, injection_map_core
├── host: Streamlit
│   ├── app.py                       entry point
│   ├── state.py                     becomes a thin AppState subclass
│   ├── caches.py                    becomes a CacheBackend instantiation
│   └── <tab wrappers>               pure rendering + interaction handlers
├── host: PySide6 (qt/)
│   ├── main_window.py               entry point
│   ├── state.py                     thin AppState subclass
│   └── <tab wrappers>               pure rendering + interaction
└── host: NiceGUI (web/)
    ├── app.py                       entry point + thin AppState subclass
    └── <tab wrappers>               pure rendering + interaction
```

The contract: a "host tab wrapper" should contain only (a) widget
construction, (b) event handlers that call backbone view-model methods, (c)
host-specific layout. Any pandas reshaping, any state-machine bookkeeping,
any cache lookup → core.

## Inventory of work

### Cache backend (from cache audit)

| Cache category | Streamlit slots | Qt | NiceGUI |
|---|---|---|---|
| Generation counter `_lf_gen` | 1 | none | none |
| Raw DataFrames (lines, 2WT, 3WT, gens, buses) | 5 | none | none |
| Enriched DataFrames + VL lookups | 5 | none | none |
| Per-component DF + extension DF | 2 dict-shaped | none | none |
| Diagram SVGs (SLD, NAD, BBT) | 3 | 2 (tab-local) | 2 (module-level) |
| Map / geographic | 4 | none | none |
| Analysis-result caches (SA, RCC, dq/dv) | 5 | partial | none |

Streamlit invalidation lives in `caches.py:invalidate_on_*` (called from
`state.py` at ~6 sites). NiceGUI today does **manual `.clear()` calls in
15+ places** in `web/app.py` (lines 389, 459, 1479, 1700, 1887, 2067, 2429,
3064, 3500, 3538, 3593, 3655, 6887, 7241, 7383). Qt has no centralized
invalidation because it has no DataFrame cache and `set_network` resets the
tab-local SVG dicts.

### AppState (from AppState audit)

| State item | Streamlit | Qt | NiceGUI | Sharable? |
|---|---|---|---|---|
| `network`, `selected_vl`, `last_report_json` | `st.session_state` keys | properties | properties | ✓ identical semantics |
| `change_log` | per-method `st.session_state` lists | shared `ChangeLog()` instance | shared `ChangeLog()` instance | needs translation layer |
| LF params, import options | dialog-local | AppState fields | AppState fields | ✓ — Streamlit needs to lift to AppState |
| Network load lifecycle | inline in `state.py:load_network` | `install_network` method | `install_network` method | ✓ identical semantics, different notification |
| Topology / LF notification | implicit rerun + cache pops | PySide6 `Signal.emit` | callback loop | abstract as listener hooks |
| UI-local (`component_type`, `nad_depth`) | session_state keys | tab-widget state | tab-widget state | leave per-host |

### Per-tab view-model extraction (from per-tab audit)

| Tab | Core today | View-model? | Wrapper non-render LOC | Priority |
|---|---|---|---|---|
| Overview | `network_info_core.py` | ✓ `OverviewData` | low | — |
| Voltage Analysis | `voltage_analysis_core.py` | ✓ `VoltageAnalysisData` | low | — |
| Operational Limits | `operational_limits.py` | ✓ `OperationalLimitsViewModel` | low | — |
| Reactive Curves | `reactive_curves.py` | ✓ `ReactiveCurvesViewModel` | medium | minor cleanup |
| **Data Explorer** | `data_view.py` helpers only | ✗ | **~900 ST + ~710 Qt** | 🔴 **CRITICAL** — biggest LOC win |
| **Security Analysis** | `security_analysis.py` dict builders | ✗ | ~800 ST + ~430 Qt | 🔴 **CRITICAL** |
| Short Circuit | `short_circuit_analysis.py` helpers | ✗ | ~160 ST + ~340 Qt | 🟠 high |
| Pmax | `pmax_visualization.py` helpers | ✗ | ~100 ST + ~250 Qt | 🟡 medium |
| Extensions | `extensions_data.py` helpers | ✗ | ~200 ST + ~280 Qt | 🟡 medium |
| **Injection Map** | `injection_map.py` mixes streamlit imports | ✗ | extraction re-implemented in Qt + Web | 🟠 high — purity first |
| Network Map | `diagram_services.extract_map_data` | ✗ | small | 🟢 low |
| NAD / SLD | `diagram_services.generate_*` | ✗ | small | 🟢 low |

## Sub-plan 1 — CacheBackend abstraction

### New module `iidm_viewer/cache_backend.py` (host-agnostic)

```python
class CacheBackend(Protocol):
    """Minimal storage interface a host plugs in."""
    def get(self, key: str, default=None) -> Any: ...
    def set(self, key: str, value) -> None: ...
    def setdefault(self, key: str, default) -> Any: ...
    def pop(self, key: str, default=None) -> Any: ...
    def keys(self) -> Iterable[str]: ...

# Pure functions — keying + invalidation rules, no storage assumption
def cache_key(net_key, lf_gen, variant_id="InitialState", *extra) -> tuple: ...
def get_or_compute(
    backend, slot: str, key, compute: Callable[[], Any]
) -> Any: ...
def invalidate_topology(backend) -> None: ...
def invalidate_load_flow(backend, variant_id="InitialState") -> None: ...
def invalidate_network_replace(backend) -> None: ...

# Cache-slot constants (single source of truth for the ~25 names)
LINES_ALL = "_lines_all_cache"
…
TOPOLOGY_SLOTS = (LINES_ALL, …)
LOAD_FLOW_SLOTS = (BUSES_ALL, …)
NETWORK_REPLACE_SLOTS = (SUBSTATION_POSITIONS, …)
```

### Host implementations

```python
# iidm_viewer/caches.py becomes
class StreamlitSessionBackend(CacheBackend):
    def get(self, k, d=None):    return st.session_state.get(k, d)
    def set(self, k, v):         st.session_state[k] = v
    …

# iidm_viewer/qt/state.py adds
class DictBackend(CacheBackend):
    def __init__(self):  self._d = {}
    …

# iidm_viewer/web/state.py — uses DictBackend too (no Qt-specific concerns)
```

### Per-tab getters become backend-parameterised

```python
# in component_registry.py / data_view.py / operational_limits.py / …
def get_lines_all(network, *, backend, variant_id="InitialState"):
    key = cache_key(_net_key(network), _lf_gen(backend, variant_id), variant_id)
    return get_or_compute(
        backend, LINES_ALL, key,
        lambda: variants.fetch_for_variant(network, "get_lines",
                                           variant_id, all_attributes=True),
    )
```

Variant-aware keying lives **here**, in the host-agnostic backbone — no
per-host duplication for the N-K refactor.

### Streamlit migration

- `caches.py` keeps its public API (`get_lines_all`, `invalidate_on_*`, …)
  but each function delegates to the new `cache_backend` helpers with the
  Streamlit backend injected. ~35–40 LOC change inside `caches.py`, **zero
  changes** in the 10 importers (`state.py`, `diagrams.py`,
  `voltage_analysis.py`, `filters.py`, `data_explorer.py`, three `*_tab.py`,
  `network_reduction.py`).
- The three diagram caches currently in `diagrams.py` (`_sld_cache`,
  `_nad_cache`, `_bbt_cache`) move to `caches.py` slot constants.

### Qt migration

- `qt/state.py:AppState` instantiates a `DictBackend` once and exposes it as
  `self.cache_backend`. The two tab-local SVG caches (`SldTab._cache`,
  `NadTab._cache`) move to `self.state.cache_backend` slots
  `_sld_cache` / `_nad_cache`. `install_network` calls
  `invalidate_network_replace(self.cache_backend)`.
- Topology / LF mutations route through new signals
  `topology_changed` / `loadflow_completed`, both wired in `AppState` to call
  `invalidate_topology(backend)` / `invalidate_load_flow(backend)` before
  the rest of the world reacts.
- **New gain**: Qt now caches DataFrames it currently re-fetches. Big
  networks should refresh faster (audit estimates 2–4 worker round-trips
  saved per tab refresh on Operational Limits / Overview).

### NiceGUI migration

- `web/state.py:AppState` instantiates a `DictBackend`. The 15+ manual
  `.clear()` sites collapse to 3 calls (network replace, topology change,
  load flow) routed via the same listener hooks Qt uses.
- The two module-level dicts (`_sld_cache`, `_nad_cache`) move into the
  backend, deleted from `web/app.py:131-132`.

## Sub-plan 2 — Unified AppState base class

### New module `iidm_viewer/app_state.py`

```python
class AppState(ABC):
    """Host-agnostic base. Storage + notification are abstract."""

    # Storage hooks ------------------------------------------------
    @abstractmethod
    def _get(self, key: str, default=None) -> Any: ...
    @abstractmethod
    def _set(self, key: str, value) -> None: ...

    # Notification hooks ------------------------------------------
    @abstractmethod
    def _emit_network_changed(self, network) -> None: ...
    @abstractmethod
    def _emit_selected_vl_changed(self, vl_id) -> None: ...
    @abstractmethod
    def _emit_loadflow_completed(self, result) -> None: ...
    @abstractmethod
    def _emit_topology_changed(self) -> None: ...

    # Public surface (shared) -------------------------------------
    @property
    def network(self): return self._get("network")
    @property
    def selected_vl(self): return self._get("selected_vl")
    @property
    def last_report_json(self): return self._get("last_report_json")
    @property
    def cache_backend(self) -> CacheBackend: ...
    @property
    def change_log(self) -> ChangeLog: ...

    def load_network_from_path(self, path, *, parameters=None,
                                post_processors=None): ...
    def load_network_from_bytes(self, name, raw, *, parameters=None,
                                 post_processors=None): ...
    def create_empty_network(self, network_id="network"): ...
    def install_network(self, network) -> None:
        """Shared dance: reset selected_vl, clear change_log, invalidate
        caches, emit signal, pick default VL, emit selected_vl."""

    def set_selected_vl(self, vl_id): ...
    def notify_network_changed(self): ...
    def run_loadflow(self, *, generic_params=None,
                      provider_params=None): ...
    def notify_topology_changed(self, *, affects_geography=False): ...
```

### Host subclasses

```python
# iidm_viewer/state.py
class StreamlitAppState(AppState):
    def _get(self, k, d=None): return st.session_state.get(k, d)
    def _set(self, k, v):       st.session_state[k] = v
    def _emit_network_changed(self, n):  st.session_state.pop("_lf_report_json", None)
    def _emit_selected_vl_changed(self, vl): pass   # streamlit reruns
    def _emit_loadflow_completed(self, r):
        invalidate_load_flow(self.cache_backend, variant_id="InitialState")
    def _emit_topology_changed(self):
        invalidate_topology(self.cache_backend)

# iidm_viewer/qt/state.py
class QtAppState(AppState, QObject):
    network_changed = Signal(object)
    selected_vl_changed = Signal(str)
    loadflow_completed = Signal(object)
    topology_changed = Signal()
    # _get/_set wrap an internal dict
    # _emit_* call .emit on the signal

# iidm_viewer/web/state.py
class WebAppState(AppState):
    # _get/_set wrap an internal dict
    # _emit_* iterate registered listener callbacks (existing _on_*_listeners)
```

### Change log unification

Streamlit's per-method `_change_log_{method_name}` lists are the only real
storage divergence. Plan:

- `AppState.change_log` returns a shared `ChangeLog` instance backed by
  `self._get("change_log")`. The instance lives in storage (Streamlit:
  `st.session_state["change_log"]`; Qt/NiceGUI: in-memory).
- `data_explorer._render_change_log` and the equivalent in
  `extensions_explorer.py` are rewritten to call `change_log.entries(method)`
  instead of `st.session_state.get(f"_change_log_{method}", [])`.
- `state.add_to_change_log(method_name, …)` is replaced by
  `change_log.record(component=method_to_component(method_name), …)`.
- The deprecated per-method lists are removed; backward compat is not a
  goal (this is a host-agnostic refactor).

### Streamlit-only blockers

These need light UI refactors but no semantic change:

- LF parameter dialog (`lf_parameters.py:get_lf_parameters()`) is read by
  `state.run_loadflow`. After refactor, `state.lf_generic_params` /
  `state.lf_provider_params` live in `AppState`; the dialog writes them via
  `state.set_lf_params(generic, provider)`. ~30 LOC change in
  `lf_parameters.py`, ~5 LOC in `state.py`.
- Import-options dialog (`io_options.py`) similarly persists into
  `state.import_format` / `state.import_params` /
  `state.import_post_processors`.

## Sub-plan 3 — View-model extraction (ranked)

Each tab gets a new core module (or extension of an existing one) shaped
like the working examples (`operational_limits.OperationalLimitsViewModel`
+ `build_operational_limits_view_model`). Hosts consume the view-model and
emit edit/click events back to the controller methods on the core.

### 🔴 Data Explorer  (largest LOC win, most complex)

New `iidm_viewer/data_explorer_core.py`:

```python
@dataclass
class DataExplorerViewModel:
    component: str
    rows_df: pd.DataFrame        # enriched + filtered
    editable_attributes: list[str]
    filter_specs: dict           # which filters are active
    pending_edits: dict          # element_id → {attr: value}
    change_log_entries: list[ChangeLogEntry]
    selection: list[str]         # selected element ids
    creation_form_state: dict | None
    can_apply: bool              # gated on view_mode for N-K too

def build_data_explorer_view_model(state, component: str,
                                    *, variant_id=None) -> DataExplorerViewModel: ...

def apply_pending_edits(state, vm: DataExplorerViewModel) -> ApplyResult: ...
def revert_all(state, vm: DataExplorerViewModel) -> None: ...
def compute_changes(original_df, edited_df, editable_cols) -> pd.DataFrame:
    """Moved from data_explorer.py:72; 100% pure pandas."""
```

What moves: `_compute_changes` (purely pandas), `_revert_all_changes`,
filter-state machine, edit-form orchestration, per-component creation-form
state assembly. ~400–500 LOC out of Streamlit, ~300 LOC out of Qt.

### 🔴 Security Analysis

Rename `security_analysis.py` → `security_analysis_core.py` (or move the
non-shared bits to `security_analysis_tab.py`). Add:

```python
@dataclass
class SecurityAnalysisViewModel:
    contingencies: list[Contingency]
    monitored_elements: list[Element]
    actions: list[Action]
    operator_strategies: list[Strategy]
    last_result: SecurityAnalysisResult | None

def add_contingency(vm, contingency): ...   # all the list-state-machine
def remove_contingency(vm, contingency_id): ...
def normalize_manual_contingency(...): ...   # already needed by N-K plan
```

Each host wraps the same view-model state machine; widgets only render +
emit `add_contingency` / `remove_…` / `run_security_analysis` events.

### 🟠 Short Circuit Analysis

Same shape as Security Analysis but smaller. `ShortCircuitViewModel` with
fault list, parameters, last result.

### 🟠 Injection Map

Pure-core extraction: move `_extract_injection_data` and friends from
`injection_map.py` (which imports streamlit for caching) to a new
`injection_map_core.py` with **no framework imports**. Streamlit version
becomes a thin renderer. Qt and NiceGUI delete their re-implementations.

### 🟡 Pmax / Extensions

Smaller LOC payoff but follows the same pattern. Defer until the heavy
hitters are landed.

## Sequencing relative to N-K

The N-K refactor needs three things this plan provides:

- variant-aware cache keying → sub-plan 1
- `nk_contingency` / `nk_variant_id` / view-mode state slots → sub-plan 2
- view-mode toggle in Data Explorer + side-by-side rendering — easier on
  top of a `DataExplorerViewModel` than on top of today's ~900-LOC monolith

Practical recommendation:

| Step | What | Includes from this plan | N-K from `n-k-variant-comparison.md` |
|---|---|---|---|
| 1 | **CacheBackend** | Sub-plan 1 in full | — |
| 2 | **AppState base + Streamlit subclass** | Sub-plan 2 minus change-log migration | — |
| 3 | **Change-log unification** | finish Sub-plan 2 | — |
| 4 | **N-K backbone primitives** | — | `variants.py`, `variant_id` keywords |
| 5 | **N-K Streamlit UI (Reactive Curves + Operational Limits first)** | — | per-tab radio + side-by-side |
| 6 | **Data Explorer view-model** | Sub-plan 3 #1 | (enables N-K Data Explorer cleanly) |
| 7 | **N-K Data Explorer + SLD UI** | — | side-by-side editors / SVGs |
| 8 | **N-K parity in Qt + NiceGUI** | — | per-host UI |
| 9 | **Security Analysis view-model** | Sub-plan 3 #2 | (lets manual picker live in core) |
| 10 | **Short Circuit / Injection / Pmax / Extensions view-models** | Sub-plan 3 #3-6 | — |

Steps 1–3 are the prerequisite slice. Step 4 onwards mirrors the N-K plan,
just landing on a thinner host layer.

## Verification

- **CacheBackend**: unit tests for each invalidation rule (topology / LF /
  network replace) against both `StreamlitSessionBackend` and `DictBackend`.
  Existing Streamlit cache tests in `tests/test_*.py` continue to pass
  unchanged (the public API is preserved).
- **AppState**: unit tests in `tests/test_app_state.py` cover the network
  load lifecycle for all three subclasses, asserting:
  `install_network` clears the change log and pops the right cache slots;
  `set_selected_vl` emits exactly one notification; `run_loadflow` bumps
  the right `_lf_gen` slot.
- **View-models**: each new view-model gets unit tests that don't import
  streamlit / PySide6 / NiceGUI. Existing `tests/test_data_view.py`,
  `tests/test_security_analysis.py` are extended.
- **E2E parity**: Streamlit Playwright (per [AGENTS.md §2](../AGENTS.md)),
  Qt offscreen run, NiceGUI Playwright — each runs the IEEE 14 smoke
  scenario before and after each phase and diffs the rendered output for
  the four pre-existing view-model tabs (must be identical).

## Out of scope / non-goals

- **No new tabs.** This refactor doesn't introduce features beyond what's
  already in each host.
- **No renderer changes.** Streamlit widgets stay Streamlit widgets, etc.
  The goal is to shrink the wrapper, not change the look.
- **No backward compatibility for the change-log per-method lists.** They
  exist only in Streamlit; the refactor deletes them in favour of the
  shared `ChangeLog`. The visible Change Log panel keeps the same look.
- **No move of the worker thread.** `powsybl_worker.py` and `NetworkProxy`
  stay exactly as they are — they're already the strongest piece of shared
  backbone.
- **No abstraction of UI-local state** (e.g. Streamlit's `component_type`,
  `nad_depth`). Those stay in `st.session_state`, not in `AppState`. The
  base class is for things every host has.

## Risks and mitigations

- **Bigger refactor blast radius than N-K.** Mitigation: each sub-plan
  step lands independently and ships before the next starts. Steps 1–3
  are the prerequisite slice for N-K; later steps can land any time.
- **Streamlit cache behaviour subtle to preserve.** `caches.py` already
  has explicit "cache pops even though `_lf_gen` would auto-invalidate, to
  free memory" comments. Mitigation: keep the existing
  `_TOPOLOGY_CACHE_KEYS` / `_LOAD_FLOW_CACHE_KEYS` lists as the
  authoritative invalidation specification; the new `cache_backend`
  helpers consume them unchanged.
- **Change-log migration breaks the visible UI.** Mitigation:
  `tests/test_data_explorer.py` already snapshots the change-log render
  output; the migration must preserve it byte-for-byte. A scripted
  before/after diff catches regression.
- **View-model extractions create churn in 3 hosts at once.** Mitigation:
  do the core extraction first (purely additive), then migrate one host at
  a time. Old wrapper code stays alive until the migration commit removes
  it.
