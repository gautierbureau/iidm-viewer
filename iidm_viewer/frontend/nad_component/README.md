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

## Upgrading the library

```bash
npm install @powsybl/network-viewer-core@<new-version>
npm run build
git add package.json package-lock.json dist/
```

Commit the regenerated `dist/` alongside the version bump so the wheel
stays in sync.
