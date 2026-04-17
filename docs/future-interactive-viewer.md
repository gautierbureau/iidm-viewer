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

### Files

```
iidm_viewer/
  frontend/
    nad_component/
      index.html         # the iframe document Streamlit loads
  nad_interactive.py     # still builds the augmented SVG string
  nad_component.py       # NEW: declare_component wrapper + render helper
  diagrams.py            # call nad_component.render(...) instead of html()
```

### `nad_component/index.html` responsibilities

1. Implement the Streamlit component bootstrap protocol directly (no
   `streamlit-component-lib` dependency — the wire format is small):
   - Listen for `message` events with `type: "streamlit:render"`; the
     payload (`event.data.args`) carries the `svg_html` string we need
     to inject.
   - On load, post `{type: "streamlit:componentReady", apiVersion: 1}` to
     `window.parent`.
   - After the SVG is in the DOM, post
     `{type: "streamlit:setFrameHeight", height: <measured>}`.
2. Inject `args.svg_html` into `document.body.innerHTML`. That SVG already
   carries the click handlers from `nad_interactive.py`; the only change
   is the `notify` function — instead of raw `postMessage`, it now calls
   `window.parent.postMessage({type: "streamlit:setComponentValue",
   dataType: "json", value: {type: "nad-vl-click", vl: "..."}}, "*")`.
   Easiest: make `nad_interactive.py` emit both forms, keyed on whether a
   `window.__iidmStreamlitComponent` flag is present (set by
   `index.html`).

### `nad_component.py` (~20 lines)

```python
import os
import streamlit.components.v1 as components

_COMPONENT_DIR = os.path.join(os.path.dirname(__file__), "frontend", "nad_component")
_component = components.declare_component("iidm_nad", path=_COMPONENT_DIR)

def render_interactive_nad(svg_html: str, height: int = 700, key: str = "nad"):
    """Returns the last click payload: {'type': 'nad-vl-click', 'vl': 'VLx'} or None."""
    return _component(svg_html=svg_html, height=height, default=None, key=key)
```

### Wiring in `diagrams.render_nad_tab`

```python
click = render_interactive_nad(html, height=700, key=f"nad_{selected_vl}_{depth}")
if click and click.get("type") == "nad-vl-click":
    vl = click.get("vl")
    if vl and vl != st.session_state.get("selected_vl"):
        st.session_state.selected_vl = vl
        st.rerun()
```

No page reload, session state and the NetworkProxy survive, the sidebar
selectbox picks up the new `selected_vl`, and the SLD tab renders the
target voltage level.

### Packaging

Add `[tool.hatch.build.targets.wheel.force-include]` (or equivalent) so
`iidm_viewer/frontend/nad_component/index.html` ships in the wheel.

### Testing

- `tests/test_nad_interactive.py` unchanged — still validates the SVG
  injection.
- New `tests/test_nad_component.py` can assert that
  `render_interactive_nad` calls `declare_component` with the expected
  path and that the wrapper reads the returned value. Full end-to-end
  click behaviour needs Playwright (see AGENTS.md §2) — the existing
  recipe already drives the NAD tab.

### Why this is the right step even if we later pick Option 2

Option 2 (wrapping `@powsybl/network-viewer`) is the same
`declare_component` shape with a bigger frontend build behind it. The
Python-side contract (`render_interactive_nad(svg_html) -> click dict`)
and the `diagrams.py` integration stay identical; only the
`frontend/nad_component/` directory changes from a 50-line HTML stub to
an npm-built bundle. So the 1-day investment is not throwaway — it
defines the seam we'd swap a real JS library into.

## Difficulty summary

| Task | Cost |
|---|---|
| Minimal bidirectional component (above) | 0.5–1 day, pure Python + single HTML file |
| Adding drag / pan-zoom for static SVG | Not possible without a JS layout engine — skip to Option 2 |
| Full `@powsybl/network-viewer` component | 1–2 weeks: npm project, build, packaging, release pipeline |

