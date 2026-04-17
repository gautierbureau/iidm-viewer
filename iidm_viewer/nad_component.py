"""Custom Streamlit component that renders a pypowsybl NAD SVG and
returns the last click (voltage-level) to Python.

Python contract (stable across Stage 1 and Stage 2):

    render_interactive_nad(svg, metadata) -> None
                                           | {"type": "nad-vl-click", "vl": "VLx", "ts": ...}

Stage 2 implementation: the frontend lives in
``iidm_viewer/frontend/nad_component/`` as a Vite-built TypeScript
project that wraps ``@powsybl/network-viewer-core``. ``npm run build``
there produces ``dist/index.html`` + ``dist/assets/nad-component.js``;
the wheel ships that ``dist/`` tree (see ``pyproject.toml``) so no
Node toolchain is needed at ``pip install`` time.

See ``docs/future-interactive-viewer.md`` for the upgrade rationale
and invariants.
"""
from __future__ import annotations

import os

import streamlit.components.v1 as components

_COMPONENT_DIR = os.path.join(
    os.path.dirname(__file__), "frontend", "nad_component", "dist"
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
