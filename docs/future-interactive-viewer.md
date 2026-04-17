# Future work — interactive NAD / SLD navigation

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

- `iidm_viewer/nad_interactive.py` injects `<style>` + `<script>` into the
  NAD SVG. Clicks on `.nad-vl-nodes > g` and `.nad-branch-edges > g` fire
  `window.parent.postMessage({channel: 'iidm-viewer', type: 'nad-vl-click',
  vl: 'VLx'}, '*')` (and `nad-edge-click` for edges).
- Verified against pypowsybl 1.14's IEEE_14 NAD SVG: the selector matches
  the id-bearing `<g>` directly and the metadata svgIds line up. The sender
  side works.
- **Missing**: nothing on the Python side receives these messages.
  `st.components.v1.html` is a one-way iframe with no setComponentValue
  channel. An earlier `window.top.location.href = '?selected_vl=...'`
  fallback was removed because (a) the Streamlit component iframe sandbox
  omits `allow-top-navigation`, and (b) a full page reload would discard
  `st.session_state` — the user would lose the uploaded network.

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

| What | Stage 1 | Stage 2 |
|---|---|---|
| **Sidebar `vl_selector`** (`components.py`) | unchanged | unchanged |
| **Single Line Diagram tab** (`diagrams.render_sld_tab`) | unchanged — still renders pypowsybl's SLD SVG via `render_svg` | unchanged |
| **Overview tab** (`network_info.render_overview`) | unchanged | unchanged |
| **Network Map tab** (`network_map.py`) | unchanged | unchanged |
| **Data Explorer (Components + Extensions)** | unchanged | unchanged |
| **Reactive Capability Curves / Operational Limits / Pmax** | unchanged | unchanged |
| **`state.py` / `NetworkProxy` / worker thread model** | unchanged — the frontend receives `svg` and `metadata` as plain strings that were already extracted inside the worker via `nad.svg` / `nad.metadata` | unchanged |
| **Load Flow button + `run_loadflow` / `lf_parameters`** | unchanged | unchanged |
| **`st.session_state.selected_vl` as the single source of truth** | preserved — NAD clicks write to it and `st.rerun()`, exactly like the sidebar selectbox does today | preserved |

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

If we ever want interactive SLD too (Option 2 also ships an
`SingleLineDiagramViewer`), it becomes a parallel `sld_component`
following the exact same pattern — again, no other tab is touched.

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

