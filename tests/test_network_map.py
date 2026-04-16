"""Tests for iidm_viewer.network_map."""
import json

from iidm_viewer.state import load_network
from iidm_viewer.network_map import _extract_map_data


def test_extract_map_data_returns_substations(xiidm_upload):
    network = load_network(xiidm_upload)
    result = _extract_map_data(network)
    spos, smap, lmap = result[0], result[1], result[2]

    assert len(spos) == 11  # 11 substations in IEEE14
    assert len(smap) == 11
    assert all("id" in s and "coordinate" in s for s in spos)


def test_extract_map_data_returns_branches(xiidm_upload):
    network = load_network(xiidm_upload)
    result = _extract_map_data(network)
    lmap = result[2]

    # 17 lines + 3 transformers = 20 branches
    assert len(lmap) == 20
    assert all("voltageLevelId1" in l and "voltageLevelId2" in l for l in lmap)


def test_extract_map_data_returns_vl_coords_and_nv(xiidm_upload):
    network = load_network(xiidm_upload)
    result = _extract_map_data(network)
    vl_coords = result[3]
    vl_nv = result[4]

    assert len(vl_coords) == 14  # 14 voltage levels
    assert len(vl_nv) == 14
    # Check a known VL
    assert "VL1" in vl_coords
    assert vl_coords["VL1"]["lat"] == 48.86
    assert "VL1" in vl_nv
    assert vl_nv["VL1"] == 135.0


def test_extract_map_data_substations_have_voltage_levels(xiidm_upload):
    network = load_network(xiidm_upload)
    result = _extract_map_data(network)
    smap = result[1]

    # S4 has 3 VLs: VL4, VL7, VL9
    s4 = next(s for s in smap if s["id"] == "S4")
    vl_ids = {vl["id"] for vl in s4["voltageLevels"]}
    assert vl_ids == {"VL4", "VL7", "VL9"}


def test_extract_map_data_multi_vl_substations(xiidm_upload):
    """Substations with multiple VLs provide data for concentric circles."""
    network = load_network(xiidm_upload)
    result = _extract_map_data(network)
    smap = result[1]

    multi_vl = [s for s in smap if len(s["voltageLevels"]) > 1]
    assert len(multi_vl) == 2  # S4 (3 VLs) and S5 (2 VLs)

    for sub in multi_vl:
        for vl in sub["voltageLevels"]:
            assert "nominalV" in vl
            assert vl["nominalV"] > 0


def test_map_data_is_json_serializable(xiidm_upload):
    """All map data must be JSON-serializable for the Leaflet template."""
    network = load_network(xiidm_upload)
    result = _extract_map_data(network)
    spos, smap, lmap, vl_coords, vl_nv = result[0], result[1], result[2], result[3], result[4]

    # Should not raise
    json.dumps(spos)
    json.dumps(smap)
    json.dumps(lmap)
    json.dumps(vl_coords)
    json.dumps(vl_nv)
