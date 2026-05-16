# NAD component — Stage 2 frontend

Custom Streamlit component that wraps
[`@powsybl/network-viewer-core`](https://www.npmjs.com/package/@powsybl/network-viewer-core)
to render an interactive Network Area Diagram. Pan, zoom, drag, and
hover come from the library; our only JS (~60 lines in `src/main.ts`)
speaks Streamlit's iframe wire protocol and forwards
`onSelectNodeCallback(equipmentId, ...)` as
`{type: "nad-vl-click", vl, ts}` via `setComponentValue`.

## Files

| Path | Role |
|---|---|
| `src/main.ts` | Wrapper — the only code we maintain |
| `index.html` | Vite entry point (source) |
| `package.json`, `vite.config.ts`, `tsconfig.json` | Build config |
| `dist/` | Build output — committed so `pip install` works without Node |

## Develop

```bash
cd iidm_viewer/frontend/nad_component
npm ci
npm run build   # → dist/index.html + dist/assets/nad-component.js
```

CI (`.github/workflows/ci.yml`) rebuilds `dist/` on every push. The
release workflow rebuilds it fresh before packaging the wheel.

## Python-side contract

`iidm_viewer/nad_component.py` declares the component with
`path=frontend/nad_component/dist`. Python passes `svg` and `metadata`
(raw strings from `pypowsybl.NadResult`) and receives back either
`None` or `{"type": "nad-vl-click", "vl": "VLx", "ts": ...}` — the
same contract as Stage 1; only the frontend implementation changed.

## Interaction surface

What the underlying `NetworkAreaDiagramViewer` exposes (from
`node_modules/@powsybl/network-viewer-core/dist/index.d.ts`) vs. what
`src/main.ts` actually wires.

### Viewer-options callbacks (`NadViewerParametersOptions`)

The NAD viewer's constructor takes a single options bag instead of
positional arguments. `src/main.ts` passes only
`enableDragInteraction: true`, `enableAdaptiveTextZoom: true` with
`adaptiveTextZoomThreshold: Number.MAX_VALUE`, and
`onSelectNodeCallback`. Everything else is left at its default.

| Callback | Signature | State | Notes |
|---|---|---|---|
| `onSelectNodeCallback` | `(equipmentId, nodeId, mousePosition) => void` | **wired** | Click on a voltage-level node. `equipmentId` is the VL id. Wrapper emits `{type: 'nad-vl-click', vl, ts}`. The `nodeId` and `mousePosition: Point` arguments are discarded. |
| `onMoveNodeCallback` | `(equipmentId, nodeId, x, y, xOrig, yOrig) => void` | dormant | Fires at drag-end when the user repositions a VL node. Drag interaction is already enabled in `main.ts`, but the drop position is never reported back to Python — so layout edits are lost on rerender. Wiring this would let the host persist NAD positions per network. |
| `onMoveTextNodeCallback` | `(equipmentId, vlNodeId, textNodeId, shiftX, shiftY, shiftXOrig, shiftYOrig, connectionShiftX, connectionShiftY, connectionShiftXOrig, connectionShiftYOrig) => void` | dormant | Drag-end on a label. Same persistence story as node moves; the library tracks both the label offset and the offset of the wire connecting it to its node. |
| `onToggleHoverCallback` | `(hovered, mousePosition, equipmentId, equipmentType) => void` | dormant | NAD-flavoured hover-enter / hover-leave. Distinct from the SLD callback shape — payload here carries `mousePosition: Point` instead of an `anchorEl`. Setting it also activates the library's private `handleHighlightableElementHover` / `handleInjectionHover` / `handleEdgeHover` machinery, i.e. the built-in highlight-related-elements behaviour. |
| `onRightClickCallback` | `(svgId, equipmentId, equipmentType, mousePosition) => void` | dormant | Context-menu hook. Natural place to attach "open in SLD", "remove", "edit limits", etc. as right-click actions in the Qt port. |
| `onBendLineCallback` | `(svgId, equipmentId, equipmentType, linePoints, lineOperation) => void` | dormant | Fires when the user adds, drags, or removes a bend point on a line. `lineOperation` describes the gesture; `linePoints` is the new polyline (or `null` after a straighten). Needed if hand-tuned routing should persist. |

### Boolean / numeric options on `NadViewerParametersOptions`

| Option | Currently set | Effect |
|---|---|---|
| `enableDragInteraction` | `true` | Allows VL-node and label drag. Without a move callback wired, edits are visual-only and lost on rerender. |
| `enableAdaptiveTextZoom` | `true` | Activates the library's `createLegendBox()` foreignObject path; `main.ts` then deletes the static pypowsybl `<g class="nad-text-nodes">` so the library owns labels. |
| `adaptiveTextZoomThreshold` | `Number.MAX_VALUE` | Deliberately set to MAX_VALUE so labels are never auto-hidden — the adaptive path only drops labels when `maxDisplayedSize > threshold`. |
| `enableLevelOfDetail` | unset | When `true`, the library swaps in less detail at low zoom. Off would force full detail at all zoom levels. |
| `zoomLevels` | unset | Custom LOD breakpoints. |
| `addButtons` | unset | If `true`, the library renders its own zoom/action buttons bar inside the SVG (zoomIn / zoomOut / zoomToFit / save / screenshot). Free toolbar; just turn it on. |
| `initialViewBox` | unset | Open the NAD already centered/zoomed on a region. Useful for cross-tab landing (e.g. Map → NAD jumps to a substation). |
| `hoverPositionPrecision` | unset | Rounding granularity for the hover callback's position. |

### Imperative API on the viewer instance

`src/main.ts` currently throws the viewer away on every `render()` —
just like SLD. The library exposes a much richer instance API:

```ts
// Render lifecycle
viewer.setSvgContent(svg)                    // hot-swap SVG, preserve handlers
viewer.setViewBox(viewBox); getViewBox()     // pan/zoom programmatically
viewer.zoomToFit(); zoomIn(); zoomOut()
viewer.checkAndUpdateLevelOfDetail()         // re-evaluate LOD without a full rerender

// Programmatic layout edits
viewer.moveNodeToCoordinates(equipmentId, x, y)
viewer.moveTextNodeToCoordinates(equipmentId, shiftX, shiftY,
                                 connectionShiftX, connectionShiftY)

// Live state updates — no SVG regeneration
viewer.setBranchStates(branchStates: BranchState[])
viewer.setJsonBranchStates(json: string)
viewer.setVoltageLevelStates(states: VoltageLevelState[])
viewer.setJsonVoltageLevelStates(json: string)

// Export
viewer.saveSvg(); savePng(bg?); screenshot(bg?)
viewer.getSvg(); getJsonMetadata(); getDimensionsFromSvg()

// Multi-NAD linking
viewer.syncViewBoxWith(other | others[])     // synchronised pan/zoom across NADs
```

Two of these matter a lot for performance:

```ts
type BranchState = {
  branchId: string;
  value1: number | string;        // active power or flow value, side 1
  value2: number | string;
  connected1: boolean;
  connected2: boolean;
  connectedBus1: string;
  connectedBus2: string;
};

type VoltageLevelState = {
  voltageLevelId: string;
  busValue: { busId: string; voltage: number; angle: number }[];
};
```

After a load flow we could push new flows/voltages with
`setBranchStates` + `setVoltageLevelStates` instead of regenerating
the full NAD SVG on the worker — a much cheaper update path.

### Built-in interactions (no JS hook required)

- **Pan + mouse-wheel zoom** via SVG.js panZoom (internally toggled by
  the viewer's `enablePanzoom` / `disablePanzoom`).
- **Adaptive level-of-detail** when `enableLevelOfDetail` is on.
- **Hover-driven highlight** of related elements (the library's
  `highlightRelatedElements` / `addHighlightBusClass`) — active when
  `onToggleHoverCallback` is set, since the library guards on
  `isHoverCallbackUsed`.
- **Built-in zoom / save / screenshot buttons** by setting
  `addButtons: true` — no extra JS to write.

### Metadata available to the host (no JS change needed)

`NadResult.metadata` parses into:

```ts
interface DiagramMetadata {
  layoutParameters: LayoutParametersMetadata;
  svgParameters: SvgParametersMetadata;
  busNodes: BusNodeMetadata[];
  nodes: NodeMetadata[];        // VL nodes, with x, y, equipmentId
  injections?: InjectionMetadata[];
  edges: EdgeMetadata[];        // lines, with svgId, equipmentId, busNode1/2, …
  textNodes: TextNodeMetadata[];
}

interface NodeMetadata {
  svgId: string;
  equipmentId: string;          // the VL id
  x: number; y: number;         // layout position
}

interface EdgeMetadata {
  svgId: string;
  equipmentId: string;          // line / transformer / etc.
  node1: string; node2: string; // the two VL nodes
  busNode1: string; busNode2: string;
  type: string;
  bendingPoints?: PointMetadata[];
  edgeInfoMiddle?, edgeInfo1?, edgeInfo2?: EdgeInfoMetadata;
}
```

Python can pre-index `equipmentId → (x, y)` to drive `setViewBox`
cleanly, or read `bendingPoints` to round-trip user-edited routing.

### Wiring scorecard

| Hook | Effort | Why it matters here |
|---|---|---|
| `setBranchStates` / `setVoltageLevelStates` after a load flow | Capture the viewer instance; build state arrays from LF results | **High** — any LF result currently re-renders the whole NAD via pypowsybl; the live-update API would make LF refreshes essentially free. |
| `onToggleHoverCallback` → P/Q/I popover | Wire callback + host overlay | Same payoff as SLD hover — the kind of UI Streamlit can't render without a rerun. |
| `onMoveNodeCallback` + `onMoveTextNodeCallback` → persist layout | Both callbacks + per-network JSON cache on the host | Hand-tuned NAD positions stick across sessions. |
| `addButtons: true` | One option flip | Free zoom / save / screenshot toolbar. |
| `onRightClickCallback` → context menu | Callback + host menu | Right-click "Open in SLD", "Edit limits", "Remove from view"… |
| `setViewBox(initialViewBox)` on cross-tab nav | Read `(x, y)` from `NodeMetadata`, compute a centered box | Land on the right VL instead of the default origin. |
| `syncViewBoxWith` | Only useful with ≥2 NADs side by side | Compare-mode (before/after) would benefit. |

## Upgrading the library

```bash
npm install @powsybl/network-viewer-core@<new-version>
npm run build
git add package.json package-lock.json dist/
```

Commit the regenerated `dist/` alongside the version bump so the wheel
stays in sync.
