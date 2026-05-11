# Map component

Custom Streamlit component that renders the interactive geographical
network map. Wraps
[`@powsybl/network-map-layers`](https://www.npmjs.com/package/@powsybl/network-map-layers)
(deck.gl layers purpose-built for PowSyBl networks: `SubstationLayer`,
`LineLayer`) on top of
[MapLibre GL JS](https://maplibre.org/) with raw OpenStreetMap raster
tiles as the basemap. The library supplies voltage-tier colouring
(`getNominalVoltageColor`), concentric rings per substation, and
parallel-line/flow rendering. Our `src/main.ts` (~220 lines) speaks
Streamlit's iframe wire protocol, converts raw Python dicts into the
library's typed models (`MapEquipments`, `GeoData`,
`MapLineWithType[]`) and attaches a DOM tooltip on hover.

## Files

| Path | Role |
|---|---|
| `src/main.ts` | Wrapper — the only code we maintain |
| `index.html` | Vite entry point (source) |
| `package.json`, `vite.config.ts`, `tsconfig.json` | Build config |
| `dist/` | Build output — committed so `pip install` works without Node |

## Develop

```bash
cd iidm_viewer/frontend/map_component
npm ci
npm run build   # → dist/index.html + dist/assets/map-component.js
```

CI (`.github/workflows/ci.yml`) rebuilds `dist/` on every push. The
release workflow rebuilds it fresh before packaging the wheel.

## Python-side contract

`iidm_viewer/map_component.py` declares the component with
`path=frontend/map_component/dist`. Python passes, as plain JSON:

```python
{
  "substations": [{id, name?, voltageLevels: [{id, nominalV, substationId, ...}]}],
  "substationPositions": [{id, coordinate: {lon, lat}}],
  "lines": [{id, voltageLevelId1, voltageLevelId2, terminal1Connected,
             terminal2Connected, p1, p2, i1?, i2?, name?}],
  "height": 670,
}
```

JS → Python: `{type: 'map-substation-click', substationId, vlIds, ts}`
when the user clicks a substation. `vlIds` is the substation's
voltage-level ids ordered by descending nominal V (so a host that
defaults to "the first one" lands on the highest-voltage VL — e.g.
the 400 kV before the 90 kV one). The PySide6 prototype uses this
payload to jump to the SLD tab on the clicked VL; the Streamlit map
tab currently ignores it.

Tooltips, pan and zoom are handled entirely in the browser.

## Interaction surface

What `@powsybl/network-map-layers` + the underlying deck.gl `Layer`
base class expose vs. what `src/main.ts` actually wires.

### Layer props (custom)

`SubstationLayer` and `LineLayer` extend `CompositeLayer`, so they
inherit every deck.gl `LayerProps` field (see "deck.gl layer event
props" below). Their package-specific props:

`SubstationLayer` props (from `_SubstationLayerProps`):

| Prop | Currently set | Effect |
|---|---|---|
| `data` | array of `MapSubstation` | Source of substations. |
| `network`, `geoData`, `getNominalVoltageColor` | wired | Equipment lookup, geo lookup, voltage-tier palette. |
| `filteredNominalVoltages` | `null` (all visible) | When set to a number list, hides substations that lack a VL at one of those nominal voltages. Interactive legend → filter mapping. |
| `labelsVisible`, `labelColor`, `labelSize` | `false`, black, 12 | Toggle and style substation labels. |
| `getNameOrId` | wired | Customise label text. |
| `pickable` | `true` | Allows the `onClick`/`onHover` deck.gl events to fire. |

`LineLayer` props (from `_LineLayerProps`):

| Prop | Currently set | Effect |
|---|---|---|
| `data` | typed lines array | Lines + 2W-transformers, with `equipmentType`. |
| `disconnectedLineColor` | `[100,100,100,255]` | Color override for disconnected lines. |
| `filteredNominalVoltages` | `network.getNominalVoltages()` (all) | Tier filter, same idea as substation layer. |
| `lineFlowMode: LineFlowMode` | unset | `STATIC` / `ANIMATED_ARROWS` / `FEEDERS`. **Power-flow animation along lines.** |
| `lineFlowColorMode: LineFlowColorMode` | unset | Recolor lines by flow magnitude / overload state. |
| `lineFlowAlertThreshold` | unset | Highlight threshold (e.g. `>90%` of permanent limit). |
| `showLineFlow` | `false` | Master switch for the flow visualisation. |
| `lineFullPath`, `lineParallelPath` | `true`, `true` | Honor detailed geometry; offset parallels. |
| `updatedLines` | `[]` | Pass an incremental delta instead of re-rendering all lines after a small change. |
| `areFlowsValid` | `false` | Gate the colour-by-flow path; flip to `true` after a converged LF. |
| `labelsVisible`, `labelColor`, `labelSize`, `iconSize`, `distanceBetweenLines`, `maxParallelOffset`, `minParallelOffset`, `substationRadius`, `substationMaxPixel`, `minSubstationRadiusPixel` | various | Visual tuning. |
| `pickable` | `true` | Allows the deck.gl events to fire. |

### deck.gl layer event props (inherited from `CompositeLayerProps`)

Every deck.gl `Layer` accepts these picking-event props when
`pickable: true`. `main.ts` only wires one of them on
`SubstationLayer`; the rest are dormant on both layers.

| Prop | Signature | State | Notes |
|---|---|---|---|
| `onClick` | `(info, event) => boolean \| void` | **wired on `SubstationLayer`** | Forwarded as `{type: 'map-substation-click', substationId, vlIds, ts}`. Dormant on `LineLayer`. |
| `onHover` | `(info, event) => boolean \| void` | dormant (we hand-roll hover via `overlay.pickObject` in `mousemove`) | Built-in alternative to the manual tooltip. Less control over positioning but cheaper. |
| `onDragStart` / `onDrag` / `onDragEnd` | `(info, event) => void` | dormant | Would enable moving substations to new geo coordinates inline (geo-editing). |
| `autoHighlight` + `highlightColor` | `boolean`, `Color` | unset | deck.gl's built-in hover highlight (recolour the hit object). Free emphasis without writing a JS state machine. |
| `visible`, `opacity` | `boolean`, `number` | unset | Toggle and fade per layer (e.g. dim lines while focusing substations). |
| `getCursor` | `(state) => string` | unset | Set the cursor on hover (e.g. `'pointer'` over substations). |

### Imperative API (the wrapper has direct access)

`main.ts` keeps module-level `map: maplibregl.Map` and
`overlay: MapboxOverlay` references. Both expose a rich API the
wrapper barely uses today:

```ts
// MapLibre (basemap)
map.flyTo({ center, zoom, duration })       // animated camera move
map.fitBounds(bounds, { padding, duration })// already used on first load
map.easeTo(...); jumpTo(...); zoomTo(...);
map.setLayoutProperty('osm-tiles', 'visibility', 'none')   // toggle basemap
map.addSource(...); addLayer(...)           // overlay extra tile sources (satellite, …)
map.on('click', 'osm-tiles', handler)       // base-map level click

// deck.gl overlay
overlay.setProps({ layers, viewState, ... })       // already used on data version bump
overlay.pickObject({ x, y, radius })               // already used for tooltips
overlay.pickMultipleObjects({ x, y, radius, depth })
overlay.pickObjects({ x, y, width, height })
```

`flyTo` / `fitBounds` would let a Map → SLD/NAD jump *return* by
animating the camera to the relevant substation when the user
switches back to the Map tab.

### Built-in interactions (no JS hook required)

- **Pan + mouse-wheel zoom + pinch** from MapLibre. `box-zoom`,
  `double-click zoom`, `rotate`, `pitch` are all on by default; can
  be toggled via `map.boxZoom.disable()` etc.
- **OSM raster basemap** with the default attribution control.
- **deck.gl picking** with hit-testing handled by the layer — we
  only need `pickable: true` + an event prop.

### Wiring scorecard

| Hook | Effort | Why it matters here |
|---|---|---|
| `LineLayer.onClick` → focus the substation at the other end | Add `onClick` in `buildLayers` + rebuild + host routing | Map's missing "follow a line" interaction. Pairs naturally with the existing substation click. |
| `LineLayer.showLineFlow + lineFlowMode: ANIMATED_ARROWS` + `lineFlowColorMode` | Flip three props and feed `areFlowsValid: true` after an LF | **Big** demo win: animated power flow along lines, with overload highlighting — a feature both Streamlit and Qt hosts could simply expose as a "Show flows" toggle. |
| `filteredNominalVoltages` toggled from an interactive legend | Legend is already drawn by the wrapper; just emit a click handler that mutates the layer props | Quick "hide 90 kV / show only 400 kV" filter. |
| `autoHighlight: true` on both layers | One prop each | Free hover highlight; cheaper than the manual tooltip path. |
| `onHover` instead of the manual `mousemove + pickObject` path | Replace ~20 lines in `main.ts` | Cleaner, lets deck.gl manage hit-test caching. Decision call: the manual path gives precise DOM tooltip placement; the built-in path is faster. |
| `flyTo` on inbound cross-tab nav | Pass target lat/lon from the host, animate camera | Smooth return path when navigating back from SLD/NAD to the Map. |
| `updatedLines` instead of full-data rebuild | Diff before pushing new data | Cheaper updates after switch toggles / topology edits. |

## Upgrading the library

```bash
npm install @powsybl/network-map-layers@<new-version>
npm run build
git add package.json package-lock.json dist/
```

Pin the `@deck.gl/*` and `@luma.gl/*` peer deps to the major that
matches `network-map-layers`'s `peerDependencies`.
