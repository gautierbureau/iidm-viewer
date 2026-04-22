# Worker round-trip audit

Snapshot of how many pypowsybl-worker round-trips each tab issues on
an average Streamlit rerun (e.g. an SLD arrow-click), after the current
set of caches in `diagrams.py`, `network_map.py`, `filters.py`,
`state.get_voltage_levels_df`, and `security_analysis._get_ids`.

Conventions, from `powsybl_worker.NetworkProxy`:

- `network.attr` → **1 RT** (single `run(getattr, …)`).
- `network.method(...)` → **2 RT** (`getattr` + `call`).
- `wrapped.attr` (attribute on a wrapped `SldResult` / `NadResult` /
  `BusBreakerTopology`) → **1 RT**.
- `run(fn)` where `fn` issues many calls inside one closure → **1 RT**
  (the closure runs on the worker thread; each pypowsybl call inside
  is free of the ThreadPoolExecutor submit overhead, but still hops
  GraalVM — usually cheap once the isolate is warm).
- Anything routed through `@st.cache_data`, `st.session_state`, or the
  dedicated per-tab cache dicts → **0 RT** once warm.

The per-tab numbers below are for a warm session: network loaded,
caches populated, no topology edits since last LF. First-visit costs
are noted separately.

## Per-rerun totals (warm)

| Tab                      | RT / rerun | First-visit extra | Source of cost |
|---|---:|---:|---|
| Sidebar (`vl_selector`)  | **0** | 2 | `get_voltage_levels_df` cached in `_vl_lookup_cache` |
| Overview                 | **0** | 12 – 20 | `_overview_cache` keyed by `(net_key, lf_gen)` |
| Network Map              | **0** | 1 closure | `_map_data_cache` + `_map_data_version` wire-payload skip |
| Network Area Diagram     | **0** | 4 | `_nad_cache` keyed by `(vl, depth)` |
| Single Line Diagram      | **0 – 3** | 4 + 2 | `_sld_cache`, `_buses_all`, `_bbs_cache`, `_sub_map_cache` |
| Data Explorer Components | **2** | — | `getattr(network, method)(all_attributes=True, …)` uncached |
| Data Explorer Extensions | **2** | — | `network.get_extensions(name)` uncached |
| Reactive Capability Curves | **4** | — | `get_reactive_capability_curve_points` + `get_generators` |
| Operational Limits       | **0** | 6 | shared `caches.get_lines_all` / `get_2wt_all` / `get_operational_limits_df` |
| Pmax Visualization       | **2 – 4** | — | `get_lines(all_attributes=True)` uncached (can share `caches.get_lines_all`) + `get_buses(all_attributes=True)` |
| Voltage Analysis         | **8** | — | 4 separate uncached `get_*` |
| Injection Map            | **0** | 1 closure | `_injection_map_cache` |
| Security Analysis        | **2 – 8** | 1 closure | `_sa_id_cache` hit, but `_get_nominal_voltages` + each filterable DF uncached |
| Short Circuit Analysis   | **2** | — | `_get_nominal_voltages` uncached |

**Aggregate per rerun ≈ 26 – 37 RT** (IEEE 14 fixture, no LF logs
expander open) — down from ~50 – 65 before Overview and Operational
Limits were cached. Most of the remaining cost is spent on tab bodies
the user isn't even looking at, because Streamlit runs every
`with tab_xxx:` block on every rerun.

First-visit adds ~12 RT across SLD, NAD, and the one-shot injection /
map closures.

## Detailed breakdown

### Overview — `iidm_viewer/network_info.py::render_overview`

No caching. Every rerun issues:

| Call | RT |
|---|---:|
| `network.id` | 1 |
| `network.name` | 1 |
| `network.source_format` | 1 |
| `network.case_date` | 1 |
| `_country_totals` → `get_voltage_levels(attributes=["substation_id"])` | 2 |
| `_country_totals` → `get_substations(attributes=["country"])` | 2 |
| `_country_totals` → `get_generators(attributes=["voltage_level_id", "target_p", "p"])` | 2 |
| `_country_totals` → `get_loads(attributes=["voltage_level_id", "p0", "p"])` | 2 |
| `_branch_losses_totals` → `get_lines(attributes=["p1", "p2"])` | 2 |
| `_branch_losses_totals` → `get_2_windings_transformers(attributes=["p1", "p2"])` | 2 |
| **Post-LF (losses data present) adds `_losses_by_country`:** | |
| `get_voltage_levels` + `get_substations` (duplicate of `_country_totals`) | 4 |
| `get_lines` + `get_2_windings_transformers` (duplicate of `_branch_losses_totals`) | 4 |
| **Expander "Component statistics" when opened:** | |
| `getattr(network, method)()` × 18 component types | 36 |

**Action: cache per `(net_key, lf_generation)`**. Add a
`_overview_cache` keyed by `(_net_key(network), _lf_gen)` that stores
`(country_df, losses_dict, losses_by_country_series, component_counts)`.
Invalidate in `run_loadflow`, `load_network`, `create_empty_network`,
and on any topology edit that pops `_vl_lookup_cache`. Share the
`get_lines` / `get_2_windings_transformers` fetch with the Operational
Limits and Pmax tabs via a common `_branches_cache` (see next section).

Estimated saving: **12 → 0 RT** steady state (20 → 0 with LF), **56 → 0**
when the expander is open.

### Operational Limits — `iidm_viewer/operational_limits.py::render_operational_limits`

After routing `_get_current_flows`, `_compute_loading`,
`_get_branch_losses`, and `_get_filtered_element_ids` through the
shared `iidm_viewer/caches.py` helpers, warm reruns cost **0 RT**:

| Call path | RT |
|---|---:|
| `render_operational_limits` → `get_operational_limits_df` | 0 (cached in `_oplimits_cache` by `net_key`) |
| `_compute_loading` / `_get_branch_losses` / `_get_current_flows` → `get_lines_all` | 0 (cached in `_lines_all_cache` by `(net_key, lf_gen)`) |
| `_compute_loading` / `_get_branch_losses` / `_get_current_flows` → `get_2wt_all` | 0 (cached in `_2wt_all_cache` by `(net_key, lf_gen)`) |
| `_get_filtered_element_ids` → `get_lines_all` / `get_2wt_all` | 0 (same caches) |

First-visit cost is **~6 RT** (one `get_operational_limits` +
`get_lines(all_attributes=True)` + `get_2_windings_transformers(all_attributes=True)`)
plus another 4 RT the first time after each load flow.

Invalidation: every topology-edit site in `state.py` that pops
`_vl_lookup_cache` also pops the three new caches; `run_loadflow`
bumps `_lf_gen` so flow/loss columns recompute on the next visit.

### Voltage Analysis — `iidm_viewer/voltage_analysis.py::render_voltage_analysis`

| Call | RT |
|---|---:|
| `_vl_nominal_v` → `get_voltage_levels(attributes=["nominal_v"])` | 2 |
| `_bus_voltages` → `get_buses(all_attributes=True)` | 2 |
| `_shunt_compensation` → `get_shunt_compensators(all_attributes=True)` | 2 |
| `_svc_compensation` → `get_static_var_compensators(all_attributes=True)` | 2 |

**Action**: reuse `_get_buses_all` (already cached in
`diagrams.py` — move it to a shared `caches.py`). Add per-network
caches for `shunt_compensators` and `static_var_compensators` (LF
invalidates via `_lf_gen`). `_vl_nominal_v` is static per network —
can piggyback on `get_voltage_levels_df` by adding a `nominal_v`
column, or cache separately.

Estimated saving: **8 → 0 RT**.

### Pmax Visualization — `iidm_viewer/pmax_visualization.py::_compute_pmax_data`

| Call | RT |
|---|---:|
| `get_lines(all_attributes=True)` | 2 |
| `get_buses(all_attributes=True)` | 2 |

**Action**: shares `_branches_cache` with Operational Limits and
`_buses_all` with SLD.

Estimated saving: **4 → 0 RT**.

### Reactive Capability Curves — `iidm_viewer/reactive_curves.py::render_reactive_curves`

| Call | RT |
|---|---:|
| `get_reactive_capability_curve_points()` | 2 |
| `get_generators(all_attributes=True)` | 2 |

**Action**: cache both per `(net_key, lf_gen)` (curves are static per
network; generator `p` changes with LF).

Estimated saving: **4 → 0 RT**.

### Data Explorer Components — `iidm_viewer/data_explorer.py::render_data_explorer`

| Call | RT |
|---|---:|
| `getattr(network, method_name)(all_attributes=True, **kwargs)` where method depends on selected component | 2 |

**Action**: cache per `(net_key, lf_gen, component, voltage_level_id)`.
Invalidation is messier because the tab edits the network — the
`update_components` / `remove_components` paths in `state.py` must
pop the cache. Every edit path already pops `_vl_lookup_cache`, so
reuse the same invalidation sites.

Estimated saving: **2 → 0 RT** (but only when the user lingers; every
component-type switch still costs 2 RT on first view).

### Data Explorer Extensions — `iidm_viewer/extensions_explorer.py::render_extensions_explorer`

| Call | RT |
|---|---:|
| `network.get_extensions(extension)` | 2 |

**Action**: cache per `(net_key, extension_name)`. Invalidate on
`update_extension` / `remove_extension` (already pop `_vl_lookup_cache`).

Estimated saving: **2 → 0 RT** on re-view of the same extension.

### Short Circuit Analysis — `iidm_viewer/short_circuit_analysis.py::_get_nominal_voltages`

| Call | RT |
|---|---:|
| `get_voltage_levels(attributes=["nominal_v"])` | 2 |

**Action**: reuse the shared nominal-v list (see Voltage Analysis). Move
to `state.get_voltage_levels_df` — it already has `nominal_v` in its
columns.

Estimated saving: **2 → 0 RT**.

### Security Analysis — `iidm_viewer/security_analysis.py`

`_get_ids(network)` is the model: one `run(_gather)` closure fetches
every ID list in a single worker hop, cached per `_sa_id_cache`. Two
uncached paths remain:

| Call | RT |
|---|---:|
| `_get_nominal_voltages` → `get_voltage_levels(attributes=["nominal_v"])` | 2 |
| `_get_filterable_df` → `getattr(network, getter)(all_attributes=True)` per type | 2 each |

**Action**: share nominal-v with Voltage / Short-Circuit. Cache
`_get_filterable_df` per `(net_key, type, lf_gen)`.

Estimated saving: **2-8 → 0 RT**.

### SLD — `iidm_viewer/diagrams.py::render_sld_tab` (bus-breaker fallback)

On bus-breaker networks `_get_busbar_sections` returns `None`, so
every SLD navigation hits the fallback path in `_resolve_bus_colors`:

| Call | RT |
|---|---:|
| `network.get_bus_breaker_topology(selected_vl)` | 2 |
| `tp.buses` (attribute on wrapped object) | 1 |

**Action**: cache `get_bus_breaker_topology` per `(net_key, vl)` inside
`_resolve_bus_colors`. The topology is static for the network.

Estimated saving: **3 → 0 RT** on bus-breaker networks after first VL visit.

## Cross-cutting patterns

### 1. Introduce an `lf_generation` counter

Many caches today are invalidated manually by `state.run_loadflow` via
`st.session_state.pop(...)`. That approach requires every cache site
to remember to register itself in `run_loadflow`. A single counter
`st.session_state["_lf_gen"] += 1` incremented in `run_loadflow`
(and reset in `load_network` / `create_empty_network`) lets each cache
key by `(net_key, lf_gen)` and become self-invalidating.

Same pattern for topology edits: `_topo_gen` bumped alongside every
`_vl_lookup_cache` pop (there are ~20 such sites today).

### 2. Consolidate "heavy-dataframe" fetches

`get_lines(all_attributes=True)`, `get_2_windings_transformers(all_attributes=True)`,
`get_buses(all_attributes=True)`, `get_generators(all_attributes=True)`,
`get_loads(all_attributes=True)` are each requested by 2-4 tabs with no
shared cache. A `iidm_viewer/caches.py` module exposing
`get_branches_all(network)`, `get_buses_all(network)`,
`get_generators_all(network)`, etc. — each keyed by `(net_key, lf_gen)` —
would eliminate the duplication across Overview / Operational Limits /
Pmax / Voltage Analysis / Reactive Curves.

### 3. Active-tab gate (the structural fix)

All the per-tab caching above is palliative. The real fix is to not
run unrelated tab bodies at all on navigation reruns. See
[`active-tab-gate.md`](active-tab-gate.md) for the exploration. With
per-tab caching **and** the active-tab gate, a warm SLD arrow-click
navigates with:

- 0 RT in SLD body (already done)
- 0 RT anywhere else (gate skips the bodies)

Without the gate but with full per-tab caching, the same click costs:

- 0 RT in SLD
- 0 RT in every other tab (caches all hit)

So with full per-tab caching, the gate becomes a nice-to-have rather
than a necessity for click latency. But the gate still saves the
**first-visit costs** for every tab the user never opens, which can
be substantial on large networks.

## Suggested order of implementation

Ranked by RT saved per rerun × implementation simplicity:

1. **Overview cache** (~12 RT). One file, one `(net_key, lf_gen)` key.
2. **Shared branches cache** covering Overview / Operational Limits /
   Pmax (~12 + 4 RT of duplication removed). Needs `caches.py` helper.
3. **Shared buses cache** covering Voltage Analysis / Pmax / SLD
   (unify with existing `_buses_all`). ~4 RT saved.
4. **Voltage Analysis per-table caches** (~8 RT total including the
   shared-buses dedup).
5. **Reactive Curves cache** (~4 RT).
6. **Short Circuit `_get_nominal_voltages` sharing** (~2 RT).
7. **Data Explorer per-component cache** (~2 RT on re-view; mainly
   helps the "browse around" UX).
8. **SLD bus-breaker-topology cache** (~3 RT on bus-breaker networks).
9. **Security Analysis filterable-DF cache** (~2-8 RT).
10. **Active-tab gate** (structural — eliminates all first-visit costs
    for unopened tabs).

Items 1-3 alone would drop a typical warm rerun from ~50 RT to ~15 RT
— roughly the same as what the active-tab gate would deliver.
