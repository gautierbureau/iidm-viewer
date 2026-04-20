# Interactive NAD / SLD navigation

Park for later: make the diagrams behave like `@powsybl/network-viewer`
(click a NAD node to jump to its SLD, drag nodes around, walk through
neighbouring voltage levels via lines).

## Constraint

Streamlit renders the pypowsybl SVG via `st.components.v1.html`, which
is a one-way iframe. To get events back (clicks, drags), you need
either a small JS bridge (`window.parent.postMessage`) with a
receiver on the Python side, or a proper custom Streamlit component.

## Options, shortest to longest

### 1. Thin JS-in-SVG bridge (~1–2 days)

- pypowsybl-generated SVGs already carry `data-*` ids per VL/line.
- Inject a `<script>` into the SVG string that posts click events via
  `window.parent.postMessage`.
- Receive them with `streamlit-javascript` or a minimal custom
  component and update `st.session_state.selected_vl` / switch tabs.
- **Wins:** click-NAD → SLD, walk through lines to adjacent VLs.
- **Misses:** drag nodes — the SVG is static geometry, no layout engine
  in the browser.

### 2. Real Streamlit Component wrapping `@powsybl/network-viewer` (~1–2 weeks)

- Own an npm/React/TS package declared via
  `streamlit.components.v1.declare_component`.
- Full parity with `powsybl-network-viewer`: drag, pan/zoom, bidirectional
  events, line-based navigation.
- **Wins:** everything the JS library does.
- **Cost:** real frontend project, build pipeline, release process.

### 3. Reuse `pypowsybl-jupyter` via anywidget (~3–5 days, risky)

- `pypowsybl-jupyter` is an ipywidget around the same JS library.
- `anywidget` has some Streamlit interop, but that widget isn't
  anywidget-native — likely ends up repackaging parts of it.
- Middle effort, middle risk, not a clear win.

## Recommendation

Start with **(1)** to validate the UX of click-to-dive-into-SLD and
line-walking — highest-value interactions for lowest cost, reuses the
NAD we already render. Drag-nodes requires **(2)**; no shortcut skips
the component layer for that.

## Current state (2026-04)

**Stages 1, 2, and 3 are all implemented.** The NAD tab and the SLD
tab both render through library-backed custom Streamlit components;
clicking a VL node (NAD) or a navigation arrow (SLD) updates
`st.session_state.selected_vl` and reruns.

- `iidm_viewer/nad_component.py` declares the custom Streamlit component
  pointing at `iidm_viewer/frontend/nad_component/dist/`.
- The frontend (`iidm_viewer/frontend/nad_component/`) is a Vite +
  TypeScript project. `src/main.ts` (~60 lines) wraps
  `@powsybl/network-viewer-core`'s `NetworkAreaDiagramViewer` and
  speaks the Streamlit wire protocol directly
  (`streamlit:componentReady` / `streamlit:render` /
  `streamlit:setComponentValue` / `streamlit:setFrameHeight`). No
  `streamlit-component-lib` / React dependency.
- The library supplies pan, zoom, drag, hover, right-click, and the
  `onSelectNodeCallback(equipmentId, ...)` that fires on VL clicks.
  We translate that callback into
  `{type: "nad-vl-click", vl, ts}` via `setComponentValue`.
- `diagrams.render_nad_tab` calls `render_interactive_nad(svg, metadata,
  height, key)`, reads the returned click dict, updates
  `st.session_state.selected_vl`, and calls `st.rerun()`. No page reload;
  session state and the NetworkProxy survive.
- `dist/index.html` + `dist/assets/nad-component.js` are committed so
  `pip install` works without Node. CI (`.github/workflows/ci.yml`) and
  Release (`.github/workflows/release.yml`) run
  `npm ci && npm run build` in `iidm_viewer/frontend/nad_component/`
  before tests / wheel packaging.
- Stage 1's server-side SVG injection (`nad_interactive.py`) and the
  Stage 1 inline hit-testing in `index.html` are both gone — the
  library handles hit-testing.

### Stage 3 — interactive SLD tab

- `iidm_viewer/sld_component.py` declares a second custom component
  (`iidm_sld`) pointing at `iidm_viewer/frontend/sld_component/dist/`.
- The frontend mirrors the NAD scaffolding: a Vite + TypeScript
  project whose `src/main.ts` (~90 lines) wraps
  `@powsybl/network-viewer-core`'s `SingleLineDiagramViewer`. The
  constructor is called with `svgType = "voltage-level"` and an
  `onNextVoltageCallback(nextVId)` that posts
  `{type: "sld-vl-click", vl, ts}` via `setComponentValue`. Other
  callbacks (`onBreakerCallback`, `onFeederCallback`, `onBusCallback`)
  are `null` for now — click-to-navigate only.
- `diagrams.render_sld_tab` now reads both `.svg` and `.metadata` from
  the SLD result (two separate `run()` calls through the `NetworkProxy`,
  same caveat as NAD) and calls
  `render_interactive_sld(svg, metadata, height, key)`. On a
  `sld-vl-click` it updates `st.session_state.selected_vl` and
  `st.rerun()`s.
- CI (`.github/workflows/ci.yml`) and Release
  (`.github/workflows/release.yml`) build both `nad_component` and
  `sld_component` bundles. `pyproject.toml` excludes both source
  trees from the wheel and ships only the two `dist/` directories.

## Upgrade plan — minimal bidirectional component (path to Option 1)

Goal: one-click navigation from a NAD voltage-level to its SLD, with no
page reload and no new native JS build. Estimated 0.5–1 day.

### Shape

Replace the `st.components.v1.html(svg_html, height=700)` call in
`diagrams.render_nad_tab` with a small custom component declared via
`st.components.v1.declare_component(...)`. The component frontend is a
single static `index.html` file shipped inside the `iidm_viewer` package;
no npm / webpack / React required.

The stable Python ↔ frontend contract (preserved across Stage 1 and
Stage 2 below):

```python
render_interactive_nad(svg: str, metadata: str, height: int = 700, key: str)
    -> None | {"type": "nad-vl-click", "vl": "VLx"}
    -> None | {"type": "nad-edge-click", "edge": {...}}
```

Python always passes the raw pypowsybl outputs (`NadResult.svg`,
`NadResult.metadata`). The frontend decides how to render and how to
detect clicks. This way, the Python side never has to change when we
swap the frontend for the real library in Stage 2.

### Files (Stage 1)

```
iidm_viewer/
  frontend/
    nad_component/
      index.html         # 1 file, ~80 lines, no build step
  nad_component.py       # NEW: declare_component wrapper + render helper
  diagrams.py            # call render_interactive_nad(svg, metadata, ...)
  nad_interactive.py     # DELETED — hit-testing moves to index.html
tests/
  test_nad_interactive.py  # DELETED together with the module
  test_nad_component.py    # NEW: thin assertion on the declare_component call
```

### `nad_component/index.html` responsibilities (Stage 1)

1. Implement the Streamlit component wire protocol directly (no
   `streamlit-component-lib` dependency — the protocol is small):
   - On load, post `{type: "streamlit:componentReady", apiVersion: 1}`
     to `window.parent`.
   - Listen for `message` events with `type: "streamlit:render"`; the
     payload `event.data.args` carries `{svg, metadata}`.
   - After rendering, post
     `{type: "streamlit:setFrameHeight", height: <measured>}`.
2. Inject `args.svg` into a container div, parse `args.metadata`, and
   attach click handlers to `.nad-vl-nodes > g` and `.nad-branch-edges > g`
   — same logic `nad_interactive.py` currently does server-side, just
   moved to the browser.
3. On click, post
   `{type: "streamlit:setComponentValue", dataType: "json",
     value: {type: "nad-vl-click", vl: equipmentId}}` to `window.parent`.

### `nad_component.py` (~20 lines)

```python
import os
import streamlit.components.v1 as components

_COMPONENT_DIR = os.path.join(os.path.dirname(__file__), "frontend", "nad_component")
_component = components.declare_component("iidm_nad", path=_COMPONENT_DIR)

def render_interactive_nad(svg: str, metadata: str, height: int = 700, key: str = "nad"):
    """Returns the last click payload, or None."""
    return _component(svg=svg, metadata=metadata, height=height, default=None, key=key)
```

### Wiring in `diagrams.render_nad_tab`

```python
nad = network.get_network_area_diagram(voltage_level_ids=[selected_vl], depth=depth, ...)
click = render_interactive_nad(
    svg=nad.svg,
    metadata=nad.metadata,
    height=700,
    key=f"nad_{selected_vl}_{depth}",
)
if click and click.get("type") == "nad-vl-click":
    vl = click.get("vl")
    if vl and vl != st.session_state.get("selected_vl"):
        st.session_state.selected_vl = vl
        st.rerun()
```

No page reload; `st.session_state` and the `NetworkProxy` survive. The
sidebar selectbox picks up the new `selected_vl` on the next rerun, and
the SLD tab (unchanged) renders the target voltage level.

### Packaging (Stage 1)

Add `[tool.hatch.build.targets.wheel.force-include]` (or equivalent) so
`iidm_viewer/frontend/nad_component/index.html` ships in the wheel.

### Testing (Stage 1)

- Unit: `tests/test_nad_component.py` asserts `declare_component` is
  called with the expected path and that the wrapper forwards its
  return value.
- End-to-end: the existing Playwright recipe in AGENTS.md §2 already
  drives the NAD tab. Extend it with a click on a VL node and assert
  `st.session_state.selected_vl` has changed — Playwright can inspect
  the sidebar selectbox label.

## Upgrade plan — wrap `@powsybl/network-viewer-core` (Option 2)

Goal: reuse the Powsybl team's official JS library for NAD rendering —
free drag, pan/zoom, hover, right-click, bend-line, all maintained by
upstream. Estimated 1–2 weeks, mostly packaging, not code.

### What the library gives us

`@powsybl/network-viewer-core` exposes `NetworkAreaDiagramViewer`, whose
constructor takes the same inputs we already have from pypowsybl:

```ts
new NetworkAreaDiagramViewer(
    container: HTMLElement,
    svgContent: string,            // == NadResult.svg
    diagramMetadata: object|null,  // == JSON.parse(NadResult.metadata)
    options: {
        onSelectNodeCallback?:     (equipmentId, svgId, mousePosition) => void,
        onMoveNodeCallback?:       (...) => void,
        onMoveTextNodeCallback?:   (...) => void,
        onBendLineCallback?:       (...) => void,
        onRightClickCallback?:     (...) => void,
        onToggleHoverCallback?:    (...) => void,
    } | null,
)
```

`onSelectNodeCallback(equipmentId, ...)` is exactly the "click a VL,
give me its id" primitive we need — the library does all hit-testing
for us. Pan/zoom is built in.

### Wrapper code (tiny)

The whole TypeScript entry point is ~30 lines:

```ts
import { NetworkAreaDiagramViewer } from '@powsybl/network-viewer-core';

let viewer: NetworkAreaDiagramViewer | null = null;

function render(args: { svg: string; metadata: string }) {
    document.body.innerHTML = '<div id="nad" style="width:100%;height:100vh"></div>';
    viewer = new NetworkAreaDiagramViewer(
        document.getElementById('nad')!,
        args.svg,
        JSON.parse(args.metadata),
        {
            onSelectNodeCallback: (equipmentId) => {
                window.parent.postMessage({
                    type: 'streamlit:setComponentValue',
                    dataType: 'json',
                    value: { type: 'nad-vl-click', vl: equipmentId },
                }, '*');
            },
        },
    );
    window.parent.postMessage(
        { type: 'streamlit:setFrameHeight', height: 700 }, '*');
}

window.addEventListener('message', (e) => {
    if (e.data?.type === 'streamlit:render') render(e.data.args);
});
window.parent.postMessage(
    { type: 'streamlit:componentReady', apiVersion: 1 }, '*');
```

### Files (Stage 2)

```
iidm_viewer/
  frontend/
    nad_component/
      package.json         # dep: @powsybl/network-viewer-core
      vite.config.ts       # minimal config
      tsconfig.json
      src/
        main.ts            # the ~30 lines above
      dist/                # build output, committed or built at wheel pack
        index.html
        assets/bundle.js
  nad_component.py         # UNCHANGED — declare_component(path=.../dist)
  diagrams.py              # UNCHANGED — render_interactive_nad(svg, metadata)
```

Everything outside `frontend/nad_component/` is identical to Stage 1.
The Python contract is identical. The only swap is the contents of the
`frontend/nad_component/` directory: single static `index.html` →
bundled `dist/`.

### Packaging / release (Stage 2)

- Option A — commit `frontend/nad_component/dist/` to the repo. Wheels
  ship `dist/` via hatch `force-include`. No Node needed at `pip install`
  time. Cost: diff noise on rebuilds.
- Option B — build `dist/` in CI before packaging the wheel. Cleaner
  repo, but the release workflow gains a `npm ci && npm run build` step.

Recommend Option B and pin the `@powsybl/network-viewer-core` version
in `package.json` to stay in sync with pypowsybl's SVG/metadata schema.

### CI (Stage 2)

- GitHub Actions adds `actions/setup-node@v4` and a
  `npm ci && npm run build` step before `pytest`.
- Release workflow builds `dist/` before `hatch build`.
- No change to pytest or Playwright test recipes — the Python contract
  is the same.

## Invariants across both stages

These are explicitly preserved so the switch never costs us anything
elsewhere in the app.

| What | Stage 1 | Stage 2 | Stage 3 |
|---|---|---|---|
| **Sidebar `vl_selector`** (`components.py`) | unchanged | unchanged | unchanged |
| **Single Line Diagram tab** (`diagrams.render_sld_tab`) | unchanged — still renders pypowsybl's SLD SVG via `render_svg` | unchanged | now uses `sld_component` (Vite + `SingleLineDiagramViewer`); arrow-click navigates via `st.session_state.selected_vl` + `st.rerun()`, exactly like NAD |
| **Overview tab** (`network_info.render_overview`) | unchanged | unchanged | unchanged |
| **Network Map tab** (`network_map.py`) | unchanged | unchanged | unchanged |
| **Data Explorer (Components + Extensions)** | unchanged | unchanged | unchanged |
| **Reactive Capability Curves / Operational Limits / Pmax** | unchanged | unchanged | unchanged |
| **`state.py` / `NetworkProxy` / worker thread model** | unchanged — the frontend receives `svg` and `metadata` as plain strings that were already extracted inside the worker via `nad.svg` / `nad.metadata` | unchanged | unchanged — SLD now also pulls `.svg` and `.metadata` as two separate `run()` calls (same invariant) |
| **Load Flow button + `run_loadflow` / `lf_parameters`** | unchanged | unchanged | unchanged |
| **`st.session_state.selected_vl` as the single source of truth** | preserved — NAD clicks write to it and `st.rerun()`, exactly like the sidebar selectbox does today | preserved | preserved — SLD clicks write to it the same way |

Concretely, the diff for Stage 1 is confined to:

- **Added**: `iidm_viewer/frontend/nad_component/index.html`,
  `iidm_viewer/nad_component.py`, `tests/test_nad_component.py`,
  wheel-packaging entry in `pyproject.toml`.
- **Modified**: `iidm_viewer/diagrams.py` (one function:
  `render_nad_tab`).
- **Deleted**: `iidm_viewer/nad_interactive.py`,
  `tests/test_nad_interactive.py` (hit-testing moved into the browser).

Every other module stays byte-identical. Same for Stage 2, except the
contents of `iidm_viewer/frontend/nad_component/` change.

Stage 3 applied the same recipe to the SLD tab: `sld_component/` is a
sibling Vite project that wraps `SingleLineDiagramViewer`, wired via
`iidm_viewer/sld_component.py` and `diagrams.render_sld_tab`. No other
tab was touched.

### Possible next steps

- Forward `onBreakerCallback(breakerId, open, ...)` so clicking a
  switch in the SLD toggles it through the worker (needs a pypowsybl
  `network.update_switches(...)` round-trip + optional LF re-run).
- Forward `onFeederCallback(equipmentId, equipmentType, ...)` to a
  contextual drawer that shows the clicked equipment's data and limits.
- Add `onMoveNodeCallback` on NAD to persist manual layout tweaks per
  network.

### Why this is the right step even if we later pick Option 2

Option 2 is the same `declare_component` shape with a bigger frontend
build behind it. The Python-side contract
(`render_interactive_nad(svg, metadata) -> click dict`) and the
`diagrams.py` integration stay identical; only the
`frontend/nad_component/` directory changes. So the 1-day Stage 1
investment is not throwaway — it defines the seam we'd swap a real JS
library into.

## Difficulty summary

| Task | Cost |
|---|---|
| Minimal bidirectional component (above) | 0.5–1 day, pure Python + single HTML file |
| Adding drag / pan-zoom for static SVG | Not possible without a JS layout engine — skip to Option 2 |
| Full `@powsybl/network-viewer` component | 1–2 weeks: npm project, build, packaging, release pipeline |


## Follow-up: cross-tab navigation (NAD / Map click → SLD tab)

**Status:** parked. Rationale and implementation sketch below so we can
come back without re-investigating.

### What we want

- Click a voltage-level node in the NAD → land on the **Single Line
  Diagram** tab rendering that VL.
- Click a substation on the **Network Map** → same: land on the SLD
  tab with the clicked substation's first (or nominal-voltage-highest)
  VL selected.

Today both the NAD click (`nad-vl-click`) and the in-SLD navigation
arrow (`sld-vl-click`) update `st.session_state.selected_vl` and
rerun, but **stay on the originating tab**. The map component posts
no click events at all yet.

### Why this is parked

`st.tabs()` has no programmatic-selection API as of Streamlit 1.56.
Swapping the tab implementation is a cross-cutting change, and the
current NAD→NAD / SLD→SLD in-tab navigation is already useful, so we
shipped that first.

### Implementation sketch

Two independent pieces.

**1. Map click wiring — `iidm_viewer/frontend/map_component/src/main.ts`**

Mirror the pattern already used by NAD and SLD:

```ts
new SubstationLayer({
  ...existingProps,
  pickable: true,
  onClick: (info) => {
    const sub = info.object as MapSubstation | undefined;
    if (!sub) return;
    // Pick the highest-nominal-voltage VL on the substation as the
    // SLD target (matches what a user most likely wants to inspect).
    const vl = [...sub.voltageLevels]
      .sort((a, b) => b.nominalV - a.nominalV)[0]?.id;
    if (vl) {
      setComponentValue({ type: 'map-vl-click', vl, ts: Date.now() });
    }
  },
});
```

Python side, in `network_map.py::render_network_map`:

```python
click = render_interactive_map(substations, positions, lines, ...)
if click and click.get("type") == "map-vl-click":
    vl = click.get("vl")
    if vl and vl != st.session_state.get("selected_vl"):
        st.session_state.selected_vl = vl
        st.session_state.active_tab = "Single Line Diagram"  # see §2
        st.rerun()
```

Bundle smoke-test update: add `map-vl-click` and `onClick` to the
needle list in `tests/test_map_component.py`.

**2. Programmable tab switcher — `iidm_viewer/app.py`**

Replace `st.tabs([...])` with an `st.segmented_control` (or
`st.radio(horizontal=True, label_visibility="collapsed")`) backed by
`st.session_state["active_tab"]`. Sketch:

```python
TAB_ORDER = [
    "Overview", "Network Map", "Network Area Diagram",
    "Single Line Diagram", "Data Explorer Components",
    "Data Explorer Extensions", "Reactive Capability Curves",
    "Operational Limits",
]

if "active_tab" not in st.session_state:
    st.session_state.active_tab = "Overview"

active = st.segmented_control(
    "tab",
    options=TAB_ORDER,
    key="active_tab",
    label_visibility="collapsed",
)

if active == "Overview":
    render_overview(network)
elif active == "Single Line Diagram":
    render_sld_tab(network, selected_vl)
# …etc.
```

The NAD click handler in `diagrams.py::render_nad_tab` then also
writes `st.session_state.active_tab = "Single Line Diagram"` before
`st.rerun()`. Same session-state update wherever we want to route the
click (Map → SLD, NAD → SLD).

### Trade-offs

- **Segmented control vs native tabs:** segmented control keeps the
  Streamlit design language but doesn't look exactly like the tab
  strip we have today. Acceptable.
- **JS hack alternative:** inject `components.v1.html` that finds the
  target tab button by `aria-label` and clicks it. Preserves native
  `st.tabs()`, but is timing-fragile and breaks whenever Streamlit
  reshapes its DOM. Rejected.
- **`st.navigation` / multipage apps:** would rewrite the whole app
  structure and lose the single-file layout. Rejected for this scope.

### Estimate

~1 day end-to-end: 15 lines in `main.ts`, 5 lines in `network_map.py`,
~20-line diff in `app.py`, bundle smoke-test additions, docs refresh
(`docs/tabs.md`, `docs/network-map.md`, `AGENTS.md`).

## Bus-voltage legend — Option B (in-iframe legend)

**Status:** parked. Option A (Python-side legend under the SLD with a
fixed palette) is live — see
`iidm_viewer/diagrams.py::_render_bus_legend`. Option B below is the
more faithful alternative we'd reach for only if exact color-matching
with the SLD SVG becomes a requirement.

### Why we shipped Option A first

- No JS rebuild, no frontend changes, no new component prop.
- Post-LF voltages automatically refresh because `get_buses()` is
  just a worker-thread call made on each rerun.
- One known limitation: the legend dot colors don't match the bus
  colors drawn inside the SLD SVG. The palette is indexed by bus
  order in the VL, not derived from the SVG. For single-bus VLs
  (the common case) this is visually fine; for multi-bus VLs the
  dot/SVG color correspondence is missing.

### What Option B would add

Move the legend inside the SLD iframe, alongside the SVG. The
frontend reads the bus colors directly from the rendered SVG (via
`querySelectorAll` over the busbar-section elements the SLG library
emits) and displays them next to the voltage/angle numbers pushed in
from Python.

Sketch:

1. **Python** — extend `render_interactive_sld` to forward a list of
   buses with v/angle as a prop:
   ```python
   buses = [
       {"id": b.id, "v_mag": float(b.v_mag), "v_angle": float(b.v_angle)}
       for b in ...  # network.get_buses(all_attributes=True) filtered to VL
   ]
   return _component(svg=..., metadata=..., buses=buses, ...)
   ```
2. **Frontend** — in `iidm_viewer/frontend/sld_component/src/main.ts`
   after the library mounts the SVG, walk the DOM to find each bus's
   rendered color:
   ```ts
   const colors = new Map<string, string>();
   root.querySelectorAll<SVGElement>('[id]').forEach(el => {
     // Powsybl SLG tags busbar sections with the bus id; its stroke
     // is the color we want. Exact selector TBD against a real SLD
     // sample — test via DevTools on a multi-bus VL.
     const stroke = el.getAttribute('stroke');
     if (stroke && el.id) colors.set(el.id, stroke);
   });
   ```
   Render a small `<div class="sld-legend">` alongside the SVG listing
   each `{id, color, v_mag, v_angle}` row. Forward mount-and-resize
   via the existing `setFrameHeight` path.
3. **Smoke-test additions** — append `sld-legend` and
   `bus-voltage` tokens to the bundle needle list in
   `tests/test_sld_component.py` so the wiring can't regress silently.

### Trade-offs vs Option A

| | Option A (live) | Option B (parked) |
|---|---|---|
| Exact SLD-color matching | No (fixed palette) | Yes (read from SVG) |
| JS rebuild required | No | Yes (small edit to `main.ts`) |
| Post-LF refresh | Automatic | Requires new prop round-trip |
| Depends on SLG SVG markup | No | Yes — brittle if Powsybl reshapes bus IDs/classes |
| Effort | ~30 LOC Python | ~50 LOC TS + build + tests |

### When to switch

Pick Option B if users report the dot-vs-SVG color mismatch on
multi-bus VLs as a real usability issue. Otherwise the Python-side
legend is enough.
