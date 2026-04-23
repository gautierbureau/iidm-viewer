# Active-tab gate

Park for later: skip the Python body of every non-visible tab on each
Streamlit rerun. Today `app.py` calls `render_overview`, `render_network_map`,
`render_sld_tab`, `render_data_explorer`, `render_security_analysis`, … on
every single rerun — even the SLD-only `sld-vl-click` navigation reruns.
Several of those renderers issue 10+ uncached pypowsybl round-trips via the
worker thread (see `network_info._country_totals`, `network_info._losses_by_country`,
component-statistics expander, security-analysis preview tables, …), so every
arrow-click in the SLD pays for the full cross-tab toll.

## The pattern

Streamlit's `st.tabs()` is purely client-side — switching tabs does not
trigger a rerun, and there is no Python-side API that tells you which
tab is currently visible. The common community workaround is an
"active-tab gate":

1. A tiny custom component (0-height iframe) reaches into
   `window.parent.document`, finds the tab bar (`[role="tab"]` buttons
   inside `[data-baseweb="tab-list"]`), and posts the active tab's
   **index** back via `setComponentValue` on every click plus once
   at mount.
2. `app.py` reads the index into `st.session_state["_active_tab_idx"]`
   (defaulting to `0` on first load).
3. Every `with tab_xxx:` body is guarded:

   ```python
   TAB_NAMES = ["Overview", "Network Map", …]
   tabs = st.tabs(TAB_NAMES)
   active = st.session_state.get("_active_tab_idx", 0)

   with tabs[0]:
       if active == 0:
           render_overview(network)
   with tabs[3]:
       if active == 3:
           render_sld_tab(network, selected_vl)
   # …
   ```

Inactive tabs become no-ops. An SLD arrow-click rerun only pays for the
SLD body, not for `render_overview` + `render_security_analysis` + …

## Tradeoffs to handle before merging

- **One-rerun latency on switch.** The first click on a new tab posts
  the new index → Streamlit reruns → the guard lets that tab's body
  execute. Users see a blank panel for ~100-300 ms. Tolerable for
  heavy tabs (SLD, Security Analysis), annoying for cheap ones
  (Overview). Consider rendering the Overview body unconditionally
  so first paint isn't empty.

- **Cross-origin DOM access.** The custom component's iframe must be
  able to call `window.parent.document.querySelectorAll('[role="tab"]')`.
  That works today on Streamlit's default iframe sandbox flags
  (`allow-same-origin` is set for `declare_component` iframes) but
  could regress if Streamlit tightens sandboxing. Add a smoke test
  that asserts the component returns a valid index on CI.

- **Key by index, not label.** Keying by rendered label text is brittle
  — any rename in `app.py`'s `st.tabs([...])` call needs a matching
  string literal in each guard. Keying by position index survives
  renames but **not** reorders. A helper
  `def tab_active(i): return st.session_state.get("_active_tab_idx", 0) == i`
  keeps the gate compact.

- **Reruns from non-tab widgets.** When the user clicks the "Run AC
  Load Flow" button in the sidebar, the active tab is still whatever
  it was — the component doesn't re-post on sidebar reruns, but
  `st.session_state["_active_tab_idx"]` persists, so the guard keeps
  working. Verify: after an LF rerun, the previously-active tab's
  body still renders.

- **Components that must run regardless of visibility.** Some tabs
  register side-effectful widgets whose Streamlit state is needed
  cross-tab (e.g. sliders that write `st.session_state[...]`). Those
  widgets won't run when the tab is gated out, so their session_state
  keys disappear. Audit each tab for widgets whose value is read by
  other tabs before gating.

- **Dialogs.** `@st.dialog` bodies are not inside `with tab_xxx:`
  blocks — they're unaffected by the gate. OK.

- **Adds one more custom component.** Another `frontend/<name>/`
  with its own `npm run build` step. Keep it small (~50 LOC TS,
  no deps beyond what Vite bundles) so it doesn't compound the
  build time.

## Implementation sketch

New module `iidm_viewer/active_tab.py`:

```python
import streamlit.components.v1 as components, os

_DIR = os.path.join(os.path.dirname(__file__), "frontend", "active_tab", "dist")
_component = components.declare_component("iidm_active_tab", path=_DIR)

def sync_active_tab(n_tabs: int, key: str = "active_tab_sync") -> int:
    """Return the index of the currently-visible tab (0 on first render)."""
    idx = _component(nTabs=n_tabs, default=0, key=key)
    try:
        return int(idx)
    except (TypeError, ValueError):
        return 0
```

New `frontend/active_tab/src/main.ts`:

```ts
function sendParent(msg: Record<string, unknown>) {
  window.parent.postMessage({ isStreamlitMessage: true, ...msg }, '*');
}
function setValue(v: unknown) {
  sendParent({ type: 'streamlit:setComponentValue', dataType: 'json', value: v });
}
function currentIndex(): number {
  const doc = window.parent.document;
  const tabs = doc.querySelectorAll('[data-baseweb="tab-list"] [role="tab"]');
  for (let i = 0; i < tabs.length; i++) {
    if (tabs[i].getAttribute('aria-selected') === 'true') return i;
  }
  return 0;
}
function install() {
  const doc = window.parent.document;
  doc.addEventListener('click', (e) => {
    const t = e.target as HTMLElement | null;
    if (t && t.closest('[role="tab"]')) {
      // Let Streamlit flip aria-selected first.
      setTimeout(() => setValue(currentIndex()), 0);
    }
  }, true);
  setValue(currentIndex());
}
sendParent({ type: 'streamlit:componentReady', apiVersion: 1 });
sendParent({ type: 'streamlit:setFrameHeight', height: 0 });
install();
```

`app.py` changes:

```python
from iidm_viewer.active_tab import sync_active_tab

TAB_NAMES = ["Overview", "Network Map", …, "Short Circuit Analysis"]
tabs = st.tabs(TAB_NAMES)
active = sync_active_tab(len(TAB_NAMES))

for i, tab in enumerate(tabs):
    with tab:
        if active != i:
            continue
        # existing render call for this tab
```

## Expected win

For the IEEE 14 fixture, `render_overview` alone issues ~12 worker
round-trips per rerun (id/name/format/case_date + 4 country-totals
calls + 2 branch-loss calls + optional 4 losses-by-country calls on a
post-LF network). Gating out Overview while navigating SLDs drops the
per-click Python RT from "overview + sld" down to "sld only",
which with our current caches is **0 RT on a revisited VL**. Even on
first visit, it shaves ~10-15 RT of overhead from the click latency.

The win scales with network size: every `network.get_xxx()` call not
issued is one ThreadPoolExecutor submit + one GraalVM JNI hop
skipped.
