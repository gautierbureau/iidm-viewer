"""Custom Streamlit component that renders a pypowsybl NAD SVG and
returns the last click (voltage-level or branch edge) to Python.

Python contract preserved across Stage 1 (this file, static index.html
doing hit-testing) and Stage 2 (bundled @powsybl/network-viewer-core):

    render_interactive_nad(svg, metadata) -> None
                                           | {"type": "nad-vl-click", "vl": "VLx", "ts": ...}
                                           | {"type": "nad-edge-click", "edge": {...}, "ts": ...}
"""
from __future__ import annotations

import os

import streamlit.components.v1 as components

_COMPONENT_DIR = os.path.join(
    os.path.dirname(__file__), "frontend", "nad_component"
)
_component = components.declare_component("iidm_nad", path=_COMPONENT_DIR)


def render_interactive_nad(svg: str, metadata: str, height: int = 700, key: str = "nad"):
    return _component(
        svg=svg,
        metadata=metadata,
        height=height,
        default=None,
        key=key,
    )
