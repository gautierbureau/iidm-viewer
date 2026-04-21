"""Custom Streamlit component that renders a pypowsybl SLD SVG and
returns the last clicked navigation arrow (next voltage level) to
Python.

Python contract:

    render_interactive_sld(svg, metadata) -> None
                                           | {"type": "sld-vl-click", "vl": "VLx", "ts": ...}

The frontend lives in ``iidm_viewer/frontend/sld_component/`` as a
Vite-built TypeScript project that wraps ``@powsybl/network-viewer-core``'s
``SingleLineDiagramViewer``. ``npm run build`` produces
``dist/index.html`` + ``dist/assets/sld-component.js``; the wheel ships
that ``dist/`` tree (see ``pyproject.toml``) so no Node toolchain is
needed at ``pip install`` time.
"""
from __future__ import annotations

import os

import streamlit.components.v1 as components

_COMPONENT_DIR = os.path.join(
    os.path.dirname(__file__), "frontend", "sld_component", "dist"
)
_component = components.declare_component("iidm_sld", path=_COMPONENT_DIR)


def render_interactive_sld(
    svg: str,
    metadata: str,
    height: int = 700,
    svg_type: str = "voltage-level",
    key: str = "sld",
):
    return _component(
        svg=svg,
        metadata=metadata,
        height=height,
        svgType=svg_type,
        default=None,
        key=key,
    )
