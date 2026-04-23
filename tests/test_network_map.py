"""Tests for iidm_viewer.network_map.

``_extract_map_data`` returns a 4-tuple matching the shape the
``@powsybl/network-map-layers`` deck.gl component consumes:

    (substations, substation_positions, lines, line_positions)
"""
import json

from iidm_viewer.network_map import _extract_map_data
from iidm_viewer.state import load_network


def test_extract_map_data_returns_expected_counts(xiidm_upload):
    network = load_network(xiidm_upload)
    substations, positions, lines, line_positions = _extract_map_data(network)

    assert len(positions) == 11  # 11 substations in IEEE14
    assert len(substations) == 11
    # 17 lines (pypowsybl-jupyter's extract_map_data does not include
    # 2-winding transformers; tie lines / HVDC lines are merged in but
    # IEEE14 has none).
    assert len(lines) == 17
    assert isinstance(line_positions, list)


def test_extract_map_data_substation_shape(xiidm_upload):
    network = load_network(xiidm_upload)
    substations, _, _, _ = _extract_map_data(network)

    for s in substations:
        assert "id" in s
        assert "name" in s
        assert "voltageLevels" in s
        for vl in s["voltageLevels"]:
            assert "id" in vl
            assert "substationId" in vl and vl["substationId"] == s["id"]
            assert "nominalV" in vl and vl["nominalV"] > 0


def test_extract_map_data_position_shape(xiidm_upload):
    network = load_network(xiidm_upload)
    _, positions, _, _ = _extract_map_data(network)

    # Each position carries a {lon, lat} coordinate (GeoDataSubstation shape).
    for p in positions:
        assert "id" in p
        assert "coordinate" in p
        coord = p["coordinate"]
        assert "lon" in coord and "lat" in coord
        assert -180 <= coord["lon"] <= 180
        assert -90 <= coord["lat"] <= 90


def test_extract_map_data_line_shape(xiidm_upload):
    network = load_network(xiidm_upload)
    _, _, lines, _ = _extract_map_data(network)

    for l in lines:
        assert "id" in l
        assert "voltageLevelId1" in l and "voltageLevelId2" in l
        # Booleans, not pandas bool — the frontend relies on native JSON types.
        assert isinstance(l["terminal1Connected"], bool)
        assert isinstance(l["terminal2Connected"], bool)
        assert isinstance(l["p1"], float) and isinstance(l["p2"], float)


def test_s4_has_three_voltage_levels(xiidm_upload):
    """S4 is a known multi-VL substation in the IEEE14 fixture."""
    network = load_network(xiidm_upload)
    substations, _, _, _ = _extract_map_data(network)

    s4 = next(s for s in substations if s["id"] == "S4")
    vl_ids = {vl["id"] for vl in s4["voltageLevels"]}
    assert vl_ids == {"VL4", "VL7", "VL9"}


def test_multi_vl_substations(xiidm_upload):
    """Multiple VLs on one substation drive the concentric-rings rendering."""
    network = load_network(xiidm_upload)
    substations, _, _, _ = _extract_map_data(network)

    multi_vl = [s for s in substations if len(s["voltageLevels"]) > 1]
    assert len(multi_vl) == 2  # S4 (3 VLs) and S5 (2 VLs)


def test_map_data_is_json_serializable(xiidm_upload):
    """All map data travels through Streamlit's JSON wire; must round-trip."""
    network = load_network(xiidm_upload)
    substations, positions, lines, line_positions = _extract_map_data(network)

    json.dumps(substations)
    json.dumps(positions)
    json.dumps(lines)
    json.dumps(line_positions)
