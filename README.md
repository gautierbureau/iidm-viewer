# iidm-viewer

An app for visualising and exploring electrical power networks in
[IIDM](https://www.powsybl.org/pages/documentation/grid/formats/xiidm.html)
format. Ships with three interchangeable front-ends — Streamlit (the
default web app), NiceGUI (a single-page web alternative), and PySide6
(a native desktop window) — all driving the same underlying business
logic.

## Installation

The base install gives you the Streamlit front-end (`iidm-viewer`):

```bash
pip install iidm-viewer
```

The two other front-ends ship as optional extras so the default install
stays lean — PySide6 alone is ~150 MB, and the NiceGUI / `pywebview`
combo pulls in its own native runtime. Opt in only if you want them:

| Front-end | Install | Launcher |
|---|---|---|
| Streamlit (default) | `pip install iidm-viewer` | `iidm-viewer` |
| NiceGUI | `pip install 'iidm-viewer[nicegui]'` | `iidm-viewer-nicegui` |
| PySide6 desktop | `pip install 'iidm-viewer[pyside]'` | `iidm-viewer-pyside` |

Want everything: `pip install 'iidm-viewer[nicegui,pyside]'`.

Or from source:

```bash
git clone https://github.com/gautierbureau/iidm-viewer.git
cd iidm-viewer
pip install -e .                       # Streamlit only
pip install -e '.[nicegui,pyside]'     # all three front-ends
```

### One-line install (Linux)

For Ubuntu / other Linux desktops, the installer below sets up an
isolated virtualenv in `~/.iidm_viewer/`, adds an `iidm-viewer` shell
alias and an `iidm-viewer-stop` function, and registers a desktop entry
with an icon. The script is published as a release asset, so the URL
below always tracks the latest release:

```bash
curl -fsSL https://github.com/gautierbureau/iidm-viewer/releases/latest/download/install.sh | bash
```

To pin a specific version, replace `latest/download` with
`download/<tag>` (e.g. `download/v0.9.1`).

Requires `python3 >= 3.9` and `python3-venv`. The launcher reuses an
already-running server on `localhost:8501`, so reopening the app from
the desktop icon won't spawn duplicate processes.

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
iidm-viewer            # Streamlit web app    → http://localhost:8501
iidm-viewer-nicegui    # NiceGUI web app      → http://localhost:8080
iidm-viewer-pyside     # PySide6 native window
```

The Streamlit and NiceGUI launchers print the URL on startup; the
PySide6 launcher opens a window directly.

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

Base install (Streamlit front-end):

- Python ≥ 3.9
- `streamlit` ≥ 1.30
- `pypowsybl` ≥ 1.14.0
- `pandas`
- `plotly`

Optional extras pulled in on demand:

- `[nicegui]` — `nicegui` ≥ 3.0, `pywebview` ≥ 5 (for `--native` mode)
- `[pyside]` — `PySide6` ≥ 6.6

## Running tests

```bash
python -m pytest tests/
```
