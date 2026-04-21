# iidm-viewer

A Streamlit web app for visualising and exploring electrical power networks in [IIDM](https://www.powsybl.org/pages/documentation/grid/formats/xiidm.html) format.

## Installation

```bash
pip install iidm_viewer
```

Or from source:

```bash
git clone https://github.com/gautierbureau/iidm-viewer.git
cd iidm-viewer
pip install -e .
```

## Building the JavaScript components

The Network Area Diagram, Single Line Diagram, and Network Map tabs are
powered by three custom Streamlit components. Each is a Vite/TypeScript
wrapper under `iidm_viewer/frontend/`:

| Component | Library | Entry |
|---|---|---|
| `nad_component` | [`@powsybl/network-viewer-core`](https://www.npmjs.com/package/@powsybl/network-viewer-core) (`NetworkAreaDiagramViewer`) | `frontend/nad_component/src/main.ts` |
| `sld_component` | [`@powsybl/network-viewer-core`](https://www.npmjs.com/package/@powsybl/network-viewer-core) (`SingleLineDiagramViewer`) | `frontend/sld_component/src/main.ts` |
| `map_component` | [`@powsybl/network-map-layers`](https://www.npmjs.com/package/@powsybl/network-map-layers) + MapLibre GL JS + deck.gl | `frontend/map_component/src/main.ts` |

Each component's `dist/` bundle is committed to the repo, so
`pip install iidm-viewer` (or `pip install -e .` from source) works
without Node.js. You only need to rebuild the bundles when changing
frontend sources.

Requires Node.js ≥ 18. From the repo root:

```bash
# Build all three components
for c in nad_component sld_component map_component; do
  (cd iidm_viewer/frontend/$c && npm ci && npm run build)
done
```

Or build a single component:

```bash
cd iidm_viewer/frontend/nad_component   # or sld_component / map_component
npm ci
npm run build   # → dist/index.html + dist/assets/<name>-component.js
```

CI (`.github/workflows/ci.yml`) rebuilds all three `dist/` trees on
every push; the release workflow rebuilds them fresh before packaging
the wheel. See each component's `frontend/<name>_component/README.md`
for internals.

## Running

```bash
iidm-viewer
```

Then open the URL printed in your terminal (default: `http://localhost:8501`).

## What to expect

Load any `.xiidm` / `.iidm` network file and explore it through 8 tabs:

| Tab | What you get |
|-----|-------------|
| **Overview** | Network metadata and component counts |
| **Network Map** | Interactive MapLibre + deck.gl map (requires substation position data) |
| **Network Area Diagram** | Interactive topology diagram with configurable depth; click a voltage level to navigate |
| **Single Line Diagram** | Per-voltage-level electrical diagram; click navigation arrows to jump to the next VL |
| **Data Explorer – Components** | Editable tables for buses, lines, generators, etc. with Load Flow integration |
| **Data Explorer – Extensions** | Extension data viewable and downloadable as CSV |
| **Reactive Capability Curves** | Generator Q-limits visualisation |
| **Operational Limits** | Current loading vs. limits compliance |

## Requirements

- Python ≥ 3.9
- `streamlit` ≥ 1.30
- `pypowsybl` ≥ 1.14.0
- `pandas`
- `plotly`

## Running tests

```bash
python -m pytest tests/
```
