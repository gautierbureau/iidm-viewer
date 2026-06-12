# N-K rollout follow-ups

Status: the N-K plan in [`n-k-variant-comparison.md`](n-k-variant-comparison.md)
is **fully delivered** across all three hosts (Streamlit / PySide6 / NiceGUI)
including the Side-by-side view on every affected tab (Reactive Curves,
Operational Limits, Data Explorer, SLD). This document catalogues what is
left or worth doing in a new session, in priority order.

## Branch + entry points

- Branch: `claude/add-contingency-states-GXzDe`
- Plan doc: [`n-k-variant-comparison.md`](n-k-variant-comparison.md) (design,
  invariants, verification recipe)
- Sharing doc: [`host-sharing.md`](host-sharing.md) (broader cross-host
  unification context)
- N-K backbone:
  - `iidm_viewer/variants.py` — host-agnostic primitives
    (`build_contingency_variant`, `run_loadflow_on_variant`, `drop_variant`,
    `fetch_for_variant`, `with_variant`).
  - `iidm_viewer/app_state.py` — N-K lifecycle on the base AppState
    (`build_nk_variant`, `run_nk_loadflow`, `clear_nk_variant`,
    `on_nk_variant_changed`, `on_nk_loadflow_completed`).
  - `iidm_viewer/security_analysis.py` —
    `normalize_manual_contingency` / `validate_manual_contingency` +
    `MANUAL_GROUPING_TOKENS`.
- Per-host picker UIs:
  - Streamlit: sidebar expander in `iidm_viewer/app.py`, view-mode helper
    `iidm_viewer/components.py:render_view_mode_radio`.
  - PySide6: `iidm_viewer/qt/nk_variant_dock.py` registered as a
    `QDockWidget` in `iidm_viewer/qt/main_window.py`.
  - NiceGUI: `_build_nk_variant_card()` in `iidm_viewer/web/app.py`.
- Tests: `tests/test_variants.py`, `tests/test_nk_dock.py`, plus
  per-host test files (`tests/test_qt_prototype.py` /
  `tests/test_nicegui_prototype.py`).
- Test counts at the end of the rollout: **1382 Streamlit + 88 NiceGUI +
  ~250 Qt** passing. No skips related to N-K.

## High-value, well-scoped

### 1. NiceGUI first-paint latency on large networks ✅ DONE

**Symptom (pre-fix)**: After the install_network fix, uploading Pégase 9k
no longer froze the UI, but the first `GET /` against a server with a
preloaded network still took ~5–7 s because every tab builder called its
own `refresh()` synchronously at construction.

**Investigation finding**: A naïve
`asyncio.create_task(_deferred_initial_refresh(refresh))` did **not**
actually defer the cost — once the coroutine resumed past the
`asyncio.sleep(0)`, the synchronous `refresh()` body blocked the event
loop until it returned, and NiceGUI's response handler couldn't run in
the meantime. Verified by bisecting (disabling all deferred tasks
dropped the response from 5 s to 0.2 s on Pégase 9k).

**Fix that landed**: New `_schedule_initial_refresh(refresh_fn)` module
helper that uses `ui.timer(0.05, refresh_fn, once=True)` — NiceGUI's
timer runs the callback after the page has rendered + the socket
connection has stabilised. The five builders (`_build_extensions_explorer`,
`_build_reactive_curves`, `_build_operational_limits`,
`_build_security_analysis`, `_build_short_circuit_analysis`) now schedule
their initial refresh via this helper instead of calling
`refresh()` synchronously.

**Result**:

| Network | Pre-fix | Post-fix |
|---|---|---|
| Empty | 0.18–0.39 s | 0.18–0.39 s (unchanged) |
| Pégase 9k preloaded | 5.0–7.6 s | 0.19–1.05 s (avg ~0.5 s) |

Subsequent refreshes (e.g. browser reload) may still block briefly
while pending refresh callbacks pile up — fine for the typical
single-load-per-session workflow.

### 2. End-to-end Playwright tests

[`n-k-variant-comparison.md`](n-k-variant-comparison.md) §Verification
(lines 480–499) calls for Playwright runs against Streamlit and NiceGUI
that scripts the full flow. Today there is no harness.

**Recipe**:
- New `tests/e2e/test_streamlit_nk_smoke.py` (already in the plan's
  recipe — gated by `pytest.importorskip("playwright")`):
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
- Parallel `tests/e2e/test_nicegui_nk_smoke.py` against `iidm-viewer-nicegui`
  on a free port.
- PySide6 equivalent via `QTest`/explicit slot calls (`QT_QPA_PLATFORM=offscreen`).

**Acceptance**: Each smoke runs in CI; the segfault assertion is the
canary the plan calls out specifically.

### 3. Script-recorder hooks for the N-K lifecycle

`iidm_viewer/script_recorder.py` records HMI actions so
`iidm-viewer-replay` can re-execute a session. `build_nk_variant`,
`run_nk_loadflow`, and `clear_nk_variant` on `AppState` are **not**
currently recorded — sessions involving N-K can't be replayed.

**Recipe**:
- Add `record_build_nk_variant(contingency)`,
  `record_run_nk_loadflow(generic, provider)`,
  `record_clear_nk_variant()` to `script_recorder.py`.
- Call them inside `AppState.build_nk_variant`,
  `AppState.run_nk_loadflow`, `AppState.clear_nk_variant`
  (mirrors how `run_loadflow_no_notify` already calls
  `script_recorder.record_run_loadflow`).
- Generator emits the matching `state.build_nk_variant(...)` etc. lines.
- Tests in `tests/test_script_recorder.py` + `tests/test_script_generator.py`.

**Acceptance**: A session that uploads → builds N-K → runs N-K LF →
clears N-K produces a script that, when replayed, reaches the same
end state.

## Medium-value polish

### 4. Qt + NiceGUI adopt the per-(net_key, lf_gen, variant_id) cache

The host-sharing plan envisaged the prototype hosts adopting the
Streamlit-style caching backend that already lives in
`iidm_viewer/caches.py` (`get_*_for_variant` wrappers). Today Qt and
NiceGUI re-fetch every refresh because the `_build_*` closures call
`build_*_view_model(network, variant_id=...)` directly — they get
variant-correctness but no caching. Threading
`caches.get_enriched_component_for_variant` / `get_lines_all_for_variant` /
etc. into the prototype builders would directly accelerate follow-up #1.

### 5. "Import contingencies from JSON" in the N-K dock

`iidm_viewer/security_analysis_tab.py:_render_json_upload_section`
exists for the SA tab (Streamlit). The N-K dock UX would benefit from
the same affordance so the user can load `pypowsybl` JSON contingency
files directly into the picker rather than re-entering them.

- Streamlit: extend the sidebar expander.
- PySide6: file picker button in `qt/nk_variant_dock.py`.
- NiceGUI: `ui.upload` inside `_build_nk_variant_card`.

### 6. Multiple named N-K variants

`variants.build_contingency_variant(network, contingency,
target_variant=NK_VARIANT_ID)` already accepts arbitrary names. The
UX surfaces a single "N-K" slot. A small extension would let users
build several named contingencies and switch among them
(`nk_variant_id` becomes a list; the dock shows a select).

The cache backend's `LF_GEN` dict is already keyed by variant id and
handles arbitrary names; the per-host SVG caches do too.

## Stretch

### 7. Extend N-K to the "out-of-scope" tabs

Per the plan §Out-of-scope (line 418): Overview, NAD, Network Map,
Pmax, Voltage Analysis, Injection Map, Extensions Explorer, Short
Circuit Analysis, Network Reduction.

The backbone is already in place — `variant_id` plumbing exists for
most cores (`component_registry`, `data_view`, `diagram_services`,
`operational_limits`, `reactive_curves`). Each tab is a small per-host
patch (view-mode combo + thread `variant_id` into the view-model
builder).

Most natural candidates first:
- **NAD** — `generate_nad` already accepts cache shape
  `(vl_id, depth, variant_id)`. Just needs UI dispatch.
- **Voltage Analysis** — bus voltages differ across variants.
- **Injection Map** — gen / load `p` / `q` differ across variants.

### 8. N-K-aware Security Analysis

Today SA runs against the working variant (always InitialState between
calls). A "Compare SA across N and N-K" toggle that runs the analysis
once per variant and shows the violation deltas would be a natural
follow-up.

### 9. User-facing docs

[`n-k-variant-comparison.md`](n-k-variant-comparison.md) is a design
doc, not a user guide. Worth writing a 200-word "How to use N vs N-K"
page with sidebar walkthrough + four screenshots (Build → Run LF →
Toggle Side-by-side → Clear).

## Known small issues

### 10. Qt regression-suite stress segfaults

Individual `tests/test_qt_prototype.py` tests pass under
`QT_QPA_PLATFORM=offscreen`. Running the **full** file as one batch
sometimes segfaults during `QtWebEngine` init (Chromium GPU process /
sandbox interaction). Workarounds applied in this session: running with
`-k <filter>` to narrow the set. A `pytest-qt` fixture refinement or
per-test isolation would let CI run the file in one shot.

### 11. Pégase MATPOWER converter

pypowsybl rejects the MATPOWER `.m` text format — it expects a binary
MAT-5 file. To exercise Pégase 9k / 13k in this session I wrote a
~50-line `/tmp/m2mat.py` shim using `scipy.io.savemat` (parses
`mpc.version` / `baseMVA` / `bus` / `gen` / `branch` tables, writes a
v5 MAT). Worth landing in `tools/` (or as a `conftest.py` fixture) so
future "test on a large network" sessions don't rebuild it. The script
is intentionally small — under 60 lines.

```python
# tools/matpower_to_mat.py — sketch
import re, sys, numpy as np, scipy.io as sio

def _parse_table(text, name):
    pat = re.compile(
        rf"mpc\.{re.escape(name)}\s*=\s*\[\s*(.*?)\s*\];", re.DOTALL,
    )
    m = pat.search(text)
    if not m: return None
    rows = [
        [float(t) for t in re.split(r"[\s,]+", line.split("%")[0].strip().rstrip(";"))
         if t and not t.startswith("%")]
        for line in m.group(1).split("\n")
        if line.split("%")[0].strip().rstrip(";")
    ]
    if not rows: return None
    w = max(len(r) for r in rows)
    return np.array([r + [np.nan]*(w-len(r)) for r in rows], dtype=float)

def convert(m_path, mat_path):
    text = open(m_path).read()
    mpc = {"version": np.array(["2"], dtype="U"), "baseMVA": np.array([[100.0]])}
    for n in ("bus", "gen", "branch"):
        arr = _parse_table(text, n)
        if arr is not None: mpc[n] = arr
    sio.savemat(mat_path, {"mpc": mpc}, format="5", oned_as="row")

if __name__ == "__main__":
    convert(sys.argv[1], sys.argv[2])
```

### 12. NiceGUI per_vl / per_metric ui.select dict orientation

Already fixed in this branch for the Voltage Analysis + Injection Map
tabs (commit `f733ae3`). Worth grepping for any remaining
`ui.select(options=dict(_XX_OPTIONS), value=next(iter(_XX_OPTIONS.values())))`
patterns — NiceGUI 3.x dict-options convention is `{value: label}`,
while the shared `_*_OPTIONS` constants are `{label: value}` for
Streamlit's `selectbox`. The fix is `{v: k for k, v in _XX_OPTIONS.items()}`
at the call site.

## Recommended next single step

**Follow-up #1 (NiceGUI first-paint latency)**. It directly addresses
the user-visible pain the Pégase report surfaced, follows the pattern
already established by the `install_network` fix, and is a clean
~1-hour task. Acceptance is measurable
(`curl` round-trip <1s on a preloaded Pégase 9k).
