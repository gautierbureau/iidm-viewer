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
