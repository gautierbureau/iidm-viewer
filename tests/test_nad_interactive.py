"""Unit tests for the NAD SVG interactivity injection."""
import json
import re
from types import SimpleNamespace

import pytest

from iidm_viewer.nad_interactive import (
    _edge_vl_map,
    _vl_node_map,
    make_interactive_nad_svg,
)


def _fake_nad(svg: str, metadata: dict) -> SimpleNamespace:
    return SimpleNamespace(svg=svg, metadata=json.dumps(metadata))


def test_vl_node_map_extracts_svg_to_equipment_mapping():
    meta = {
        "nodes": [
            {"svgId": 0, "equipmentId": "VL1"},
            {"svgId": "4", "equipmentId": "VL2"},
            {"svgId": 8, "equipmentId": ""},  # dropped (empty VL id)
            {"svgId": None, "equipmentId": "VL3"},  # dropped (no svg id)
        ],
    }
    assert _vl_node_map(meta) == {"0": "VL1", "4": "VL2"}


def test_edge_vl_map_captures_endpoints_and_equipment_id():
    meta = {
        "edges": [
            {"svgId": 24, "node1": 0, "node2": 4, "equipmentId": "L1-2-1"},
            {"svgId": None, "node1": 1, "node2": 2, "equipmentId": "skip"},
        ],
    }
    out = _edge_vl_map(meta)
    assert out == {
        "24": {"node1": "0", "node2": "4", "equipmentId": "L1-2-1"},
    }


def test_make_interactive_preserves_single_svg_close_tag():
    result = _fake_nad(
        "<svg><g class='nad-vl-nodes'><g id='0'/></g></svg>",
        {"nodes": [{"svgId": 0, "equipmentId": "VL1"}]},
    )
    out = make_interactive_nad_svg(result)
    assert out.count("</svg>") == 1
    # Injection sits before the closing tag.
    assert out.index("<script>") < out.index("</svg>")


def test_make_interactive_embeds_vl_and_edge_maps():
    result = _fake_nad(
        "<svg></svg>",
        {
            "nodes": [{"svgId": 0, "equipmentId": "VL1"}],
            "edges": [
                {"svgId": 24, "node1": 0, "node2": 4, "equipmentId": "L1-2-1"}
            ],
        },
    )
    out = make_interactive_nad_svg(result)
    # The JS object literal must contain the VL mapping and the edge info.
    m = re.search(r"var VL_NODES = (\{[^}]*\});", out)
    assert m, "VL_NODES map not injected"
    assert json.loads(m.group(1)) == {"0": "VL1"}
    m = re.search(r"var EDGES = (\{.*?\});", out, re.S)
    assert m, "EDGES map not injected"
    assert "L1-2-1" in m.group(1)


def test_make_interactive_declares_click_message_channel():
    """The client code must post a message with the agreed-upon channel."""
    result = _fake_nad("<svg></svg>", {"nodes": [], "edges": []})
    out = make_interactive_nad_svg(result)
    assert "'iidm-viewer'" in out or '"iidm-viewer"' in out
    assert "'nad-vl-click'" in out or '"nad-vl-click"' in out


def test_make_interactive_is_idempotent_in_shape():
    """Calling twice must not break the SVG (no nested injection side effects)."""
    result = _fake_nad(
        "<svg><g class='nad-vl-nodes'><g id='0'/></g></svg>",
        {"nodes": [{"svgId": 0, "equipmentId": "VL1"}]},
    )
    once = make_interactive_nad_svg(result)
    twice_input = SimpleNamespace(svg=once, metadata=result.metadata)
    twice = make_interactive_nad_svg(twice_input)
    # Both calls produce a parseable single-SVG document.
    assert once.count("</svg>") == 1
    assert twice.count("</svg>") == 1


def test_make_interactive_on_real_ieee14_svg():
    """End-to-end check: run on an actual pypowsybl NadResult."""
    import pypowsybl.network as pn

    net = pn.load("test_ieee14.xiidm")
    nad = net.get_network_area_diagram(voltage_level_ids=["VL1"], depth=1)
    out = make_interactive_nad_svg(nad)
    assert "VL1" in out
    assert "nad-vl-click" in out
    assert out.count("</svg>") == 1
