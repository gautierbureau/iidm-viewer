"""Tests for the remove-pattern functions in state.py and data_explorer.py.

All tests that call remove_components() patch ``iidm_viewer.state.st`` to
provide a plain dict as session_state, because remove_components always calls
``st.session_state.pop("_vl_lookup_cache", None)`` regardless of the path
taken.  Helper functions (_resolve_hvdc_removal, _find_vl_ids_for_substations)
do not touch session_state and are tested without any patching.

The ``node_breaker_network`` fixture (conftest.py) builds a fresh
``create_four_substations_node_breaker_network()`` per test function.
"""
import pandas as pd
import pytest
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

from iidm_viewer.state import (
    REMOVABLE_COMPONENTS,
    _FEEDER_BAY_TYPES,
    _HVDC_TYPES,
    _SHALLOW_REMOVE_TYPES,
    _find_vl_ids_for_substations,
    _resolve_hvdc_removal,
    remove_components,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

@contextmanager
def _mock_session_state():
    """Replace st.session_state with a plain dict for the duration of the block."""
    with patch("iidm_viewer.state.st") as mock_st:
        mock_st.session_state = {}
        yield mock_st.session_state


# ---------------------------------------------------------------------------
# Registry sanity checks (pure Python, no network)
# ---------------------------------------------------------------------------


def test_removable_components_contains_all_expected_types():
    expected = {
        "Loads", "Generators", "Batteries",
        "Shunt Compensators", "Static VAR Compensators",
        "HVDC Lines", "VSC Converter Stations", "LCC Converter Stations",
        "Lines", "2-Winding Transformers", "Dangling Lines",
        "Voltage Levels", "Substations",
    }
    assert expected <= REMOVABLE_COMPONENTS


def test_feeder_bay_types_are_plain_injections():
    assert _FEEDER_BAY_TYPES == frozenset({
        "Loads", "Generators", "Batteries",
        "Shunt Compensators", "Static VAR Compensators",
    })


def test_hvdc_types_cover_all_three_elements():
    assert _HVDC_TYPES == frozenset({
        "HVDC Lines", "VSC Converter Stations", "LCC Converter Stations",
    })


def test_shallow_remove_types_are_correct():
    assert _SHALLOW_REMOVE_TYPES == {
        "Lines",
        "2-Winding Transformers",
        "Dangling Lines",
    }


def test_feeder_bay_types_and_hvdc_types_are_disjoint():
    assert _FEEDER_BAY_TYPES.isdisjoint(_HVDC_TYPES)


def test_voltage_levels_and_substations_in_removable():
    assert "Voltage Levels" in REMOVABLE_COMPONENTS
    assert "Substations" in REMOVABLE_COMPONENTS


# ---------------------------------------------------------------------------
# _resolve_hvdc_removal
# ---------------------------------------------------------------------------


def _first_hvdc(network):
    """Return (hvdc_id, cs1_id, cs2_id) for the first HVDC line in the network."""
    hvdc_df = network.get_hvdc_lines()
    assert not hvdc_df.empty, "network has no HVDC lines"
    hvdc_id = hvdc_df.index[0]
    return hvdc_id, hvdc_df.at[hvdc_id, "converter_station1_id"], hvdc_df.at[hvdc_id, "converter_station2_id"]


def test_resolve_hvdc_from_line_id_returns_both_stations(node_breaker_network):
    hvdc_id, cs1, cs2 = _first_hvdc(node_breaker_network)
    station_ids, line_ids = _resolve_hvdc_removal(
        node_breaker_network, "HVDC Lines", [hvdc_id]
    )
    assert set(station_ids) == {cs1, cs2}
    assert line_ids == [hvdc_id]


def test_resolve_hvdc_from_station1_returns_partner_and_line(node_breaker_network):
    hvdc_id, cs1, cs2 = _first_hvdc(node_breaker_network)
    station_ids, line_ids = _resolve_hvdc_removal(
        node_breaker_network, "VSC Converter Stations", [cs1]
    )
    assert set(station_ids) == {cs1, cs2}
    assert set(line_ids) == {hvdc_id}


def test_resolve_hvdc_from_station2_returns_same_triple(node_breaker_network):
    hvdc_id, cs1, cs2 = _first_hvdc(node_breaker_network)
    station_ids, line_ids = _resolve_hvdc_removal(
        node_breaker_network, "VSC Converter Stations", [cs2]
    )
    assert set(station_ids) == {cs1, cs2}
    assert set(line_ids) == {hvdc_id}


def test_resolve_hvdc_deduplicates_when_both_stations_selected(node_breaker_network):
    """Selecting both stations of the same HVDC must not double the line id."""
    hvdc_id, cs1, cs2 = _first_hvdc(node_breaker_network)
    station_ids, line_ids = _resolve_hvdc_removal(
        node_breaker_network, "VSC Converter Stations", [cs1, cs2]
    )
    assert set(station_ids) == {cs1, cs2}
    assert line_ids.count(hvdc_id) == 1


def test_resolve_hvdc_all_lines_returns_all_stations(node_breaker_network):
    """Selecting all HVDC lines at once returns all four converter stations."""
    hvdc_df = node_breaker_network.get_hvdc_lines()
    assert len(hvdc_df) >= 2

    all_hvdc_ids = hvdc_df.index.tolist()
    expected_stations = set()
    for hid in all_hvdc_ids:
        expected_stations.add(hvdc_df.at[hid, "converter_station1_id"])
        expected_stations.add(hvdc_df.at[hid, "converter_station2_id"])

    station_ids, line_ids = _resolve_hvdc_removal(
        node_breaker_network, "HVDC Lines", all_hvdc_ids
    )

    assert set(station_ids) == expected_stations
    assert set(line_ids) == set(all_hvdc_ids)


# ---------------------------------------------------------------------------
# _find_vl_ids_for_substations
# ---------------------------------------------------------------------------


def test_find_vl_ids_for_s1_contains_s1vl1_and_s1vl2(node_breaker_network):
    vl_ids = _find_vl_ids_for_substations(node_breaker_network, ["S1"])
    assert "S1VL1" in vl_ids
    assert "S1VL2" in vl_ids


def test_find_vl_ids_unknown_substation_returns_empty(node_breaker_network):
    vl_ids = _find_vl_ids_for_substations(node_breaker_network, ["NO_SUCH_SUB"])
    assert vl_ids == []


def test_find_vl_ids_multiple_substations_aggregated(node_breaker_network):
    vl_ids = _find_vl_ids_for_substations(node_breaker_network, ["S1", "S2"])
    vl_set = set(vl_ids)
    assert {"S1VL1", "S1VL2"}.issubset(vl_set)
    assert "S2VL1" in vl_set


# ---------------------------------------------------------------------------
# remove_components — feeder-bay injections
# ---------------------------------------------------------------------------


def test_remove_generator_removes_from_network(node_breaker_network):
    gens = node_breaker_network.get_generators()
    gen_id = gens.index[0]

    with _mock_session_state():
        result = remove_components(node_breaker_network, "Generators", [gen_id])

    assert result == [gen_id]
    assert gen_id not in node_breaker_network.get_generators().index


def test_remove_generator_also_removes_bay_switches(node_breaker_network):
    """remove_feeder_bays cleans up breaker + disconnectors, not just the injection."""
    gens = node_breaker_network.get_generators()
    gen_id = gens.index[0]
    switches_before = set(node_breaker_network.get_switches().index)

    with _mock_session_state():
        remove_components(node_breaker_network, "Generators", [gen_id])

    switches_after = set(node_breaker_network.get_switches().index)
    assert switches_after < switches_before  # at least breaker + disconnector gone


def test_remove_load_removes_from_network(node_breaker_network):
    loads = node_breaker_network.get_loads()
    load_id = loads.index[0]

    with _mock_session_state():
        result = remove_components(node_breaker_network, "Loads", [load_id])

    assert result == [load_id]
    assert load_id not in node_breaker_network.get_loads().index


def test_remove_multiple_generators_all_gone(node_breaker_network):
    gens = node_breaker_network.get_generators()
    ids = gens.index[:2].tolist()

    with _mock_session_state():
        result = remove_components(node_breaker_network, "Generators", ids)

    assert set(result) == set(ids)
    remaining = node_breaker_network.get_generators().index
    assert not any(g in remaining for g in ids)


def test_feeder_removal_returns_exactly_selected_ids(node_breaker_network):
    """For plain injections there is no cascade — returned ids == selected ids."""
    gens = node_breaker_network.get_generators()
    gen_id = gens.index[0]

    with _mock_session_state():
        result = remove_components(node_breaker_network, "Generators", [gen_id])

    assert result == [gen_id]


# ---------------------------------------------------------------------------
# remove_components — HVDC triples
# ---------------------------------------------------------------------------


def test_remove_hvdc_line_removes_line_and_both_stations(node_breaker_network):
    hvdc_id, cs1, cs2 = _first_hvdc(node_breaker_network)

    with _mock_session_state():
        result = remove_components(node_breaker_network, "HVDC Lines", [hvdc_id])

    assert set(result) == {hvdc_id, cs1, cs2}
    assert hvdc_id not in node_breaker_network.get_hvdc_lines().index
    all_stations = set(
        node_breaker_network.get_vsc_converter_stations().index.tolist()
        + node_breaker_network.get_lcc_converter_stations().index.tolist()
    )
    assert cs1 not in all_stations
    assert cs2 not in all_stations


def test_remove_vsc_station_cascades_to_full_hvdc_triple(node_breaker_network):
    hvdc_df = node_breaker_network.get_hvdc_lines()
    vsc_df = node_breaker_network.get_vsc_converter_stations()

    hvdc_id = cs1_id = cs2_id = None
    for hid in hvdc_df.index:
        cs1 = hvdc_df.at[hid, "converter_station1_id"]
        if cs1 in vsc_df.index:
            hvdc_id, cs1_id, cs2_id = hid, cs1, hvdc_df.at[hid, "converter_station2_id"]
            break
    if hvdc_id is None:
        pytest.skip("No VSC-based HVDC line in this network")

    with _mock_session_state():
        result = remove_components(node_breaker_network, "VSC Converter Stations", [cs1_id])

    assert set(result) == {hvdc_id, cs1_id, cs2_id}
    assert hvdc_id not in node_breaker_network.get_hvdc_lines().index


def test_remove_lcc_station_cascades_to_full_hvdc_triple(node_breaker_network):
    hvdc_df = node_breaker_network.get_hvdc_lines()
    lcc_df = node_breaker_network.get_lcc_converter_stations()

    hvdc_id = cs1_id = cs2_id = None
    for hid in hvdc_df.index:
        cs1 = hvdc_df.at[hid, "converter_station1_id"]
        if cs1 in lcc_df.index:
            hvdc_id, cs1_id, cs2_id = hid, cs1, hvdc_df.at[hid, "converter_station2_id"]
            break
    if hvdc_id is None:
        pytest.skip("No LCC-based HVDC line in this network")

    with _mock_session_state():
        result = remove_components(node_breaker_network, "LCC Converter Stations", [cs1_id])

    assert set(result) == {hvdc_id, cs1_id, cs2_id}
    assert hvdc_id not in node_breaker_network.get_hvdc_lines().index


# ---------------------------------------------------------------------------
# remove_components — Voltage Levels
# ---------------------------------------------------------------------------


def test_remove_voltage_level_not_present_afterwards(node_breaker_network):
    vl_id = node_breaker_network.get_voltage_levels().index[0]

    with _mock_session_state():
        result = remove_components(node_breaker_network, "Voltage Levels", [vl_id])

    assert result == [vl_id]
    assert vl_id not in node_breaker_network.get_voltage_levels().index


def test_remove_voltage_level_cascades_to_contained_generator(node_breaker_network):
    gens = node_breaker_network.get_generators(all_attributes=True)
    vl_id = gen_id = None
    for gid in gens.index:
        vl = gens.at[gid, "voltage_level_id"]
        if vl:
            vl_id, gen_id = vl, gid
            break
    if vl_id is None:
        pytest.skip("No generator with a voltage_level_id")

    with _mock_session_state():
        remove_components(node_breaker_network, "Voltage Levels", [vl_id])

    assert gen_id not in node_breaker_network.get_generators().index


def test_remove_voltage_level_with_hvdc_station_removes_hvdc_line(node_breaker_network):
    """pn.remove_voltage_levels cascades: removing a VL with an HVDC station
    must also remove the HVDC line (and both stations)."""
    hvdc_df = node_breaker_network.get_hvdc_lines()
    vsc_df = node_breaker_network.get_vsc_converter_stations(all_attributes=True)

    vl_id = hvdc_id = None
    for hid in hvdc_df.index:
        cs1 = hvdc_df.at[hid, "converter_station1_id"]
        if cs1 in vsc_df.index and "voltage_level_id" in vsc_df.columns:
            vl_id = vsc_df.at[cs1, "voltage_level_id"]
            hvdc_id = hid
            break
    if vl_id is None:
        pytest.skip("Could not locate a VL containing an HVDC converter station")

    with _mock_session_state():
        remove_components(node_breaker_network, "Voltage Levels", [vl_id])

    assert vl_id not in node_breaker_network.get_voltage_levels().index
    assert hvdc_id not in node_breaker_network.get_hvdc_lines().index


def test_remove_multiple_voltage_levels(node_breaker_network):
    vls = node_breaker_network.get_voltage_levels().index.tolist()
    ids = vls[:2]

    with _mock_session_state():
        result = remove_components(node_breaker_network, "Voltage Levels", ids)

    assert set(result) == set(ids)
    remaining = node_breaker_network.get_voltage_levels().index
    assert not any(v in remaining for v in ids)


# ---------------------------------------------------------------------------
# remove_components — Substations
# ---------------------------------------------------------------------------


def test_remove_substation_result_includes_substation_and_vl_ids(node_breaker_network):
    vl_ids = _find_vl_ids_for_substations(node_breaker_network, ["S1"])
    assert vl_ids  # sanity

    with _mock_session_state():
        result = remove_components(node_breaker_network, "Substations", ["S1"])

    assert "S1" in result
    assert set(vl_ids).issubset(set(result))


def test_remove_substation_removes_its_voltage_levels(node_breaker_network):
    vl_ids = _find_vl_ids_for_substations(node_breaker_network, ["S2"])
    assert vl_ids

    with _mock_session_state():
        remove_components(node_breaker_network, "Substations", ["S2"])

    remaining = node_breaker_network.get_voltage_levels().index
    assert not any(v in remaining for v in vl_ids)


def test_remove_substation_with_no_vls_returns_only_substation_id(node_breaker_network):
    """An empty substation has no VLs to cascade through; result is just the sub id."""
    from iidm_viewer.state import create_container
    create_container(node_breaker_network, "Substations", {
        "id": "EMPTY_SUB", "name": "", "country": "FR", "TSO": ""
    })
    assert _find_vl_ids_for_substations(node_breaker_network, ["EMPTY_SUB"]) == []

    with _mock_session_state():
        result = remove_components(node_breaker_network, "Substations", ["EMPTY_SUB"])

    assert result == ["EMPTY_SUB"]


# ---------------------------------------------------------------------------
# remove_components — shallow branches
# ---------------------------------------------------------------------------


def test_remove_line(node_breaker_network):
    lines = node_breaker_network.get_lines()
    if lines.empty:
        pytest.skip("No AC lines in this network")
    line_id = lines.index[0]

    with _mock_session_state():
        result = remove_components(node_breaker_network, "Lines", [line_id])

    assert result == [line_id]
    assert line_id not in node_breaker_network.get_lines().index


def test_remove_2_winding_transformer(node_breaker_network):
    trafos = node_breaker_network.get_2_windings_transformers()
    if trafos.empty:
        pytest.skip("No 2-winding transformers in this network")
    trafo_id = trafos.index[0]

    with _mock_session_state():
        result = remove_components(node_breaker_network, "2-Winding Transformers", [trafo_id])

    assert result == [trafo_id]
    assert trafo_id not in node_breaker_network.get_2_windings_transformers().index


# ---------------------------------------------------------------------------
# _add_to_removal_log (data_explorer)
# ---------------------------------------------------------------------------


def test_add_to_removal_log_stores_element_id_and_snapshot():
    from iidm_viewer.data_explorer import _add_to_removal_log

    snapshot_df = pd.DataFrame(
        {"p0": [100.0], "q0": [10.0]},
        index=pd.Index(["LOAD1"], name="id"),
    )
    fake_state = {}
    with patch("iidm_viewer.data_explorer.st.session_state", fake_state):
        _add_to_removal_log("Loads", ["LOAD1"], snapshot_df)

    log = fake_state["_removal_log_Loads"]
    assert len(log) == 1
    assert log[0]["element_id"] == "LOAD1"
    assert log[0]["snapshot"]["p0"] == 100.0
    assert log[0]["snapshot"]["q0"] == 10.0


def test_add_to_removal_log_deduplicates_repeated_ids():
    from iidm_viewer.data_explorer import _add_to_removal_log

    snapshot_df = pd.DataFrame(
        {"p0": [100.0]},
        index=pd.Index(["LOAD1"], name="id"),
    )
    fake_state = {}
    with patch("iidm_viewer.data_explorer.st.session_state", fake_state):
        _add_to_removal_log("Loads", ["LOAD1"], snapshot_df)
        _add_to_removal_log("Loads", ["LOAD1"], snapshot_df)  # same id again

    assert len(fake_state["_removal_log_Loads"]) == 1


def test_add_to_removal_log_cascaded_id_gets_empty_snapshot():
    """IDs not in snapshot_df (cascaded elements) receive an empty snapshot dict."""
    from iidm_viewer.data_explorer import _add_to_removal_log

    snapshot_df = pd.DataFrame(
        {"p0": [100.0]},
        index=pd.Index(["LOAD1"], name="id"),
    )
    fake_state = {}
    with patch("iidm_viewer.data_explorer.st.session_state", fake_state):
        _add_to_removal_log("Loads", ["LOAD1", "CASCADED_HVDC"], snapshot_df)

    log = fake_state["_removal_log_Loads"]
    assert len(log) == 2
    by_id = {e["element_id"]: e for e in log}
    assert by_id["LOAD1"]["snapshot"]["p0"] == 100.0
    assert by_id["CASCADED_HVDC"]["snapshot"] == {}


def test_add_to_removal_log_multiple_components_use_separate_keys():
    from iidm_viewer.data_explorer import _add_to_removal_log

    fake_state = {}
    with patch("iidm_viewer.data_explorer.st.session_state", fake_state):
        _add_to_removal_log("Loads", ["L1"], pd.DataFrame())
        _add_to_removal_log("Generators", ["G1"], pd.DataFrame())

    assert "_removal_log_Loads" in fake_state
    assert "_removal_log_Generators" in fake_state
    assert fake_state["_removal_log_Loads"][0]["element_id"] == "L1"
    assert fake_state["_removal_log_Generators"][0]["element_id"] == "G1"


# ---------------------------------------------------------------------------
# _reorder_columns (data_explorer)
# ---------------------------------------------------------------------------


def test_reorder_columns_returns_df_unchanged_when_no_priority_for_component():
    from iidm_viewer.data_explorer import _reorder_columns

    df = pd.DataFrame({"id": [1], "target_p": [100.0], "name": ["G"]})
    df.index.name = "id"
    result = _reorder_columns(df, "Switches")  # no priority for Switches
    assert list(result.columns) == list(df.columns)


def test_reorder_columns_returns_df_unchanged_when_name_col_absent():
    from iidm_viewer.data_explorer import _reorder_columns

    df = pd.DataFrame({"target_p": [100.0], "connected": [True]})
    result = _reorder_columns(df, "Generators")  # has priority but no 'name' col
    assert list(result.columns) == list(df.columns)


def test_reorder_columns_returns_df_unchanged_when_none_of_priority_cols_present():
    from iidm_viewer.data_explorer import _reorder_columns

    df = pd.DataFrame({"name": ["G1"], "r": [0.1], "x": [1.0]})
    # Generators priority cols are target_p etc — none present here
    result = _reorder_columns(df, "Generators")
    assert list(result.columns) == list(df.columns)


def test_reorder_columns_moves_target_p_right_after_name():
    from iidm_viewer.data_explorer import _reorder_columns

    df = pd.DataFrame({
        "name": ["G1"],
        "voltage_level_id": ["VL1"],
        "target_p": [100.0],
    })
    result = _reorder_columns(df, "Generators")
    cols = list(result.columns)
    assert cols.index("target_p") == cols.index("name") + 1


# ---------------------------------------------------------------------------
# _compute_changes (data_explorer)
# ---------------------------------------------------------------------------


def test_compute_changes_returns_empty_when_no_editable_cols_in_df():
    from iidm_viewer.data_explorer import _compute_changes

    df = pd.DataFrame({"name": ["G1"], "p": [100.0]})
    result = _compute_changes(df, df.copy(), ["target_p"])  # target_p not in df
    assert result.empty


def test_compute_changes_returns_empty_when_nothing_changed():
    from iidm_viewer.data_explorer import _compute_changes

    df = pd.DataFrame({"target_p": [100.0], "connected": [True]}, index=["G1"])
    result = _compute_changes(df, df.copy(), ["target_p", "connected"])
    assert result.empty


def test_compute_changes_detects_single_cell_change():
    from iidm_viewer.data_explorer import _compute_changes

    orig = pd.DataFrame({"target_p": [100.0], "target_q": [0.0]}, index=["G1"])
    edited = orig.copy()
    edited.at["G1", "target_p"] = 200.0

    result = _compute_changes(orig, edited, ["target_p", "target_q"])
    assert len(result) == 1
    assert result.at["G1", "target_p"] == 200.0
    # target_q did not change — must be absent or NaN in the sparse result
    assert "target_q" not in result.columns or pd.isna(result.at["G1", "target_q"])


def test_compute_changes_only_includes_changed_columns_in_each_row():
    from iidm_viewer.data_explorer import _compute_changes

    orig = pd.DataFrame({"a": [1.0, 2.0], "b": [10.0, 20.0]}, index=["R1", "R2"])
    edited = orig.copy()
    edited.at["R1", "a"] = 99.0  # only column 'a' changed on R1

    result = _compute_changes(orig, edited, ["a", "b"])
    assert "R1" in result.index
    assert result.at["R1", "a"] == 99.0
    # 'b' did not change on R1 so it should be absent or NaN in the result
    assert "b" not in result.columns or pd.isna(result.at["R1", "b"])


def test_compute_changes_nan_vs_nan_treated_as_equal():
    from iidm_viewer.data_explorer import _compute_changes

    orig = pd.DataFrame({"target_q": [float("nan")]}, index=["G1"])
    edited = orig.copy()

    result = _compute_changes(orig, edited, ["target_q"])
    assert result.empty


def test_compute_changes_multiple_rows_changed():
    from iidm_viewer.data_explorer import _compute_changes

    orig = pd.DataFrame({"p0": [10.0, 20.0, 30.0]}, index=["L1", "L2", "L3"])
    edited = orig.copy()
    edited.at["L1", "p0"] = 11.0
    edited.at["L3", "p0"] = 33.0

    result = _compute_changes(orig, edited, ["p0"])
    assert set(result.index) == {"L1", "L3"}
    assert result.at["L1", "p0"] == 11.0
    assert result.at["L3", "p0"] == 33.0


# ---------------------------------------------------------------------------
# _add_to_change_log (data_explorer)
# ---------------------------------------------------------------------------


def test_add_to_change_log_creates_entry_with_before_and_after():
    from iidm_viewer.data_explorer import _add_to_change_log

    orig = pd.DataFrame({"target_p": [100.0]}, index=pd.Index(["G1"]))
    changes = pd.DataFrame({"target_p": [200.0]}, index=pd.Index(["G1"]))

    fake_state = {}
    with patch("iidm_viewer.data_explorer.st.session_state", fake_state):
        _add_to_change_log("get_generators", changes, orig)

    log = fake_state["_change_log_get_generators"]
    assert len(log) == 1
    assert log[0]["element_id"] == "G1"
    assert log[0]["property"] == "target_p"
    assert log[0]["before"] == 100.0
    assert log[0]["after"] == 200.0


def test_add_to_change_log_updates_after_on_second_edit():
    from iidm_viewer.data_explorer import _add_to_change_log

    orig = pd.DataFrame({"target_p": [100.0]}, index=pd.Index(["G1"]))
    edit1 = pd.DataFrame({"target_p": [150.0]}, index=pd.Index(["G1"]))
    edit2 = pd.DataFrame({"target_p": [250.0]}, index=pd.Index(["G1"]))

    fake_state = {}
    with patch("iidm_viewer.data_explorer.st.session_state", fake_state):
        _add_to_change_log("get_generators", edit1, orig)
        _add_to_change_log("get_generators", edit2, orig)

    log = fake_state["_change_log_get_generators"]
    assert len(log) == 1
    assert log[0]["before"] == 100.0
    assert log[0]["after"] == 250.0


def test_add_to_change_log_removes_entry_when_reverted_to_original():
    from iidm_viewer.data_explorer import _add_to_change_log

    orig = pd.DataFrame({"target_p": [100.0]}, index=pd.Index(["G1"]))
    fwd = pd.DataFrame({"target_p": [200.0]}, index=pd.Index(["G1"]))
    back = pd.DataFrame({"target_p": [100.0]}, index=pd.Index(["G1"]))

    fake_state = {}
    with patch("iidm_viewer.data_explorer.st.session_state", fake_state):
        _add_to_change_log("get_generators", fwd, orig)
        _add_to_change_log("get_generators", back, orig)

    assert fake_state["_change_log_get_generators"] == []


def test_add_to_change_log_skips_nan_new_values():
    from iidm_viewer.data_explorer import _add_to_change_log

    orig = pd.DataFrame({"target_p": [100.0]}, index=pd.Index(["G1"]))
    changes = pd.DataFrame({"target_p": [float("nan")]}, index=pd.Index(["G1"]))

    fake_state = {}
    with patch("iidm_viewer.data_explorer.st.session_state", fake_state):
        _add_to_change_log("get_generators", changes, orig)

    assert fake_state.get("_change_log_get_generators", []) == []


def test_add_to_change_log_before_value_is_none_when_col_not_in_original():
    from iidm_viewer.data_explorer import _add_to_change_log

    orig = pd.DataFrame({"other_col": [1.0]}, index=pd.Index(["G1"]))
    changes = pd.DataFrame({"target_p": [200.0]}, index=pd.Index(["G1"]))

    fake_state = {}
    with patch("iidm_viewer.data_explorer.st.session_state", fake_state):
        _add_to_change_log("get_generators", changes, orig)

    log = fake_state["_change_log_get_generators"]
    assert log[0]["before"] is None
    assert log[0]["after"] == 200.0


def test_add_to_change_log_separate_keys_per_method():
    from iidm_viewer.data_explorer import _add_to_change_log

    orig = pd.DataFrame({"p0": [10.0]}, index=pd.Index(["L1"]))
    changes = pd.DataFrame({"p0": [20.0]}, index=pd.Index(["L1"]))

    fake_state = {}
    with patch("iidm_viewer.data_explorer.st.session_state", fake_state):
        _add_to_change_log("get_generators", changes, orig)
        _add_to_change_log("get_loads", changes, orig)

    assert "_change_log_get_generators" in fake_state
    assert "_change_log_get_loads" in fake_state
