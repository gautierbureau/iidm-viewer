# SLD component

Custom Streamlit component that wraps
[`@powsybl/network-viewer-core`](https://www.npmjs.com/package/@powsybl/network-viewer-core)'s
`SingleLineDiagramViewer` to render an interactive Single Line Diagram.
The library draws the SLD, places the "next voltage level" navigation
arrows on feeders that point to adjacent VLs, and hit-tests them; our
only JS (~90 lines in `src/main.ts`) speaks Streamlit's iframe wire
protocol and forwards `onNextVoltageCallback(nextVId)` as
`{type: "sld-vl-click", vl, ts}` via `setComponentValue`.

## Files

| Path | Role |
|---|---|
| `src/main.ts` | Wrapper — the only code we maintain |
| `index.html` | Vite entry point (source) |
| `package.json`, `vite.config.ts`, `tsconfig.json` | Build config |
| `dist/` | Build output — committed so `pip install` works without Node |

## Develop

```bash
cd iidm_viewer/frontend/sld_component
npm ci
npm run build   # → dist/index.html + dist/assets/sld-component.js
```

CI (`.github/workflows/ci.yml`) rebuilds `dist/` on every push. The
release workflow rebuilds it fresh before packaging the wheel.

## Python-side contract

`iidm_viewer/sld_component.py` declares the component with
`path=frontend/sld_component/dist`. Python passes `svg` and `metadata`
(raw strings from `pypowsybl` SLD result) and receives back either
`None` or `{"type": "sld-vl-click", "vl": "VLx", "ts": ...}`. The
click event fires when the user clicks one of the navigation arrows
the viewer renders on feeders whose `nextVId` points to another VL.

## Interaction surface

What the underlying `SingleLineDiagramViewer` exposes (from
`node_modules/@powsybl/network-viewer-core/dist/index.d.ts`) vs. what
`src/main.ts` actually wires.

### Constructor callback slots

```ts
new SingleLineDiagramViewer(
  container, svgContent, svgMetadata, svgType,
  minWidth, minHeight, maxWidth, maxHeight,
  onNextVoltageCallback,   // wired -> 'sld-vl-click'
  onBreakerCallback,       // wired -> 'sld-breaker-click'
  onFeederCallback,        // dormant (null)
  onBusCallback,           // dormant (null)
  selectionBackColor,      // visual; '#009eff'
  onToggleHoverCallback,   // dormant (null)
);
```

| Callback | Signature | State | Notes |
|---|---|---|---|
| `onNextVoltageCallback` | `(nextVId, event) => void` | **wired** | Click the "→ next VL" arrow drawn at the end of each feeder whose `nextVId` is set. Emits `{type: 'sld-vl-click', vl, ts}`. The `event: MouseEvent` is discarded. |
| `onBreakerCallback` | `(breakerId, open, switchEl) => void` | **wired** | Click any switch/breaker symbol. The library animates open↔closed before firing. `open` is the *desired new state*. Emits `{type: 'sld-breaker-click', breakerId, open, ts}`. The `switchEl: SVGElement` is discarded. |
| `onFeederCallback` | `(equipmentId, equipmentType, svgId, x, y) => void` | dormant | Click on an equipment glyph at the end of a feeder bay. `equipmentType` is a pypowsybl SLD type string (`LOAD`, `GENERATOR`, `LINE`, `TWO_WINDINGS_TRANSFORMER`, `HVDC_LINE`, `DANGLING_LINE`, …). `(x, y)` is the click position in container coordinates. Natural hook for "click a line → jump to the substation at the other end" or "click a generator → open its Data Explorer row". |
| `onBusCallback` | `(busId, svgId, x, y) => void` | dormant | Click a busbar section. Useful for bus-scoped filtering or selecting all equipment on the bus. |
| `onToggleHoverCallback` | `(hovered, anchorEl, equipmentId, equipmentType) => void` | dormant | Hover-enter / hover-leave on any interactive element. Setting this callback also activates the library's private `addEquipmentsPopover` machinery — i.e. enables the built-in hover popovers. `anchorEl: EventTarget` is the DOM node under the cursor, suitable for positioning a popover. |

### Imperative API on the viewer instance

`src/main.ts` currently throws the viewer away and recreates it on
every `render()`. The library also offers:

```ts
viewer.setSvgContent(svgContent)   // hot-swap SVG without rebuilding handlers
viewer.setViewBox(viewBox)         // pan/zoom programmatically
viewer.getViewBox()
viewer.refreshZoom()
viewer.setWidth/Height(...)        // size control
viewer.getDimensionsFromSvg()
viewer.setContainer(container)     // re-anchor to a different DOM node
```

`setSvgContent` would preserve pan/zoom state across VL changes.
`setViewBox` lets the host land the user *centered on a specific
feeder* after a cross-tab jump rather than at the SVG's origin.

### Built-in interactions (no JS hook required)

- **Pan + mouse-wheel zoom** via SVG.js's panZoom plugin.
- **Switch animation** on click — the library animates open↔closed
  before invoking `onBreakerCallback`.
- **Next-VL navigation arrows** — drawn by the library on every
  feeder whose metadata has `nextVId`. They are only made interactive
  when `onNextVoltageCallback` is set; otherwise omitted.
- **Two SVG types** via the `svgType` constructor argument:
  `'voltage-level'` (VL-scoped zoom limits) and `'substation'`
  (multi-VL layout). The Streamlit tab already toggles between them
  via an "Expand to substation" button (`diagrams.py`).

### Metadata available to the host (no JS change needed)

The SVG metadata that pypowsybl returns and the JS parses is also
fully readable from Python:

```ts
interface SLDMetadataNode {
  id: string;
  vid: string;          // voltage-level id this node belongs to
  nextVId: string;      // adjacent VL behind this feeder, if any
  componentType: string;
  open: boolean;
  direction: string;
  vlabel: boolean;
  equipmentId: string;  // the pypowsybl equipment this node represents
}
```

`SldResult.metadata` is a JSON string carrying `components`, `nodes`,
`wires`, `lines`, `arrows`, `layoutParams`. The host can pre-index
`equipmentId → SLDMetadataNode` to react to feeder/bus clicks without
asking the JS for anything extra.

### Wiring scorecard

| Hook | Effort | Why it matters here |
|---|---|---|
| `onFeederCallback` → cross-tab/cross-VL navigation | 1 callback in `main.ts`, +1 rebuild, host routing | Equivalent of the Map→SLD and NAD→SLD demos but inside the SLD itself (line → other-end substation, generator → data tab). |
| `onToggleHoverCallback` → live P/Q/I popover | 1 callback in `main.ts`, +Qt `QLabel` painted at `(x, y)` | The kind of interaction Streamlit can't do without a full script rerun. Trivial in Qt: no rerun, just paint. |
| `setSvgContent` instead of tear-down/rebuild | Replace the `innerHTML=''` path in `render()` | Smoother VL transitions, preserves pan/zoom state. |
| `setViewBox` after cross-tab nav | Pass feeder `(x, y)` from metadata | Lands the user on the relevant feeder, not the SVG origin. |
| `onBusCallback` → bus filter | Small | Pays off once a Data Explorer tab exists in the Qt port. |

## Upgrading the library

```bash
npm install @powsybl/network-viewer-core@<new-version>
npm run build
git add package.json package-lock.json dist/
```

Commit the regenerated `dist/` alongside the version bump so the wheel
stays in sync.
