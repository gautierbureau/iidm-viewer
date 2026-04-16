# iidm-viewer

A Streamlit web app for visualising and exploring electrical power networks in [IIDM](https://www.powsybl.org/pages/documentation/grid/formats/xiidm.html) format.

## Installation

```bash
pip install iidm-viewer
```

Or from source:

```bash
git clone https://github.com/gautierbureau/iidm-viewer.git
cd iidm-viewer
pip install -e .
```

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
| **Network Map** | Interactive Leaflet map (requires substation position data) |
| **Network Area Diagram** | Topology diagram with configurable depth |
| **Single Line Diagram** | Per-voltage-level electrical diagram |
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
