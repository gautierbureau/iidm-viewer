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

## Upgrading the library

```bash
npm install @powsybl/network-viewer-core@<new-version>
npm run build
git add package.json package-lock.json dist/
```

Commit the regenerated `dist/` alongside the version bump so the wheel
stays in sync.
