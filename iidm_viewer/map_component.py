"""Custom Streamlit component that renders the interactive
geographical network map using ``@powsybl/network-map-layers`` +
MapLibre (see ``frontend/map_component/README.md``).

Python contract: a single call

    render_interactive_map(
        substations=[{id, name, voltageLevels: [...]}, ...],
        substation_positions=[{id, coordinate: {lon, lat}}, ...],
        lines=[{id, voltageLevelId1, voltageLevelId2,
                terminal1Connected, terminal2Connected,
                p1, p2, i1?, i2?, name?}, ...],
        height=670,
        key="map",
    ) -> None

The frontend returns no value for now; tooltips and pan/zoom are
handled entirely in the browser.
"""
from __future__ import annotations

import os

import streamlit.components.v1 as components

_COMPONENT_DIR = os.path.join(
    os.path.dirname(__file__), "frontend", "map_component", "dist"
)
_component = components.declare_component("iidm_map", path=_COMPONENT_DIR)


def render_interactive_map(
    substations,
    substation_positions,
    lines,
    height: int = 670,
    key: str = "map",
):
    return _component(
        substations=substations,
        substationPositions=substation_positions,
        lines=lines,
        height=height,
        default=None,
        key=key,
    )
