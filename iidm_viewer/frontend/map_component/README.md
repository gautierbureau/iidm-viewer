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

The component returns nothing — tooltips and pan/zoom are handled
entirely in the browser. If we later want click-to-navigate (pick a
substation → set `selected_vl`), add a deck.gl `onClick` to
`SubstationLayer` and forward via `setComponentValue`, mirroring the
NAD / SLD pattern.

## Upgrading the library

```bash
npm install @powsybl/network-map-layers@<new-version>
npm run build
git add package.json package-lock.json dist/
```

Pin the `@deck.gl/*` and `@luma.gl/*` peer deps to the major that
matches `network-map-layers`'s `peerDependencies`.
