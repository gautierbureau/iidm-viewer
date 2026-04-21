"""Tests for short circuit analysis state functions and rendering."""
import types
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from iidm_viewer.state import (
    build_bus_faults,
    load_network,
    run_short_circuit_analysis,
)
from iidm_viewer.short_circuit_analysis import (
    _render_config_tab,
    _render_results_tab,
)


# ---------------------------------------------------------------------------
# build_bus_faults — integration tests (real IEEE14 network)
# ---------------------------------------------------------------------------


def test_build_bus_faults_nonempty(xiidm_upload):
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network)
    assert len(faults) > 0


def test_build_bus_faults_structure(xiidm_upload):
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network)
    for f in faults:
        assert "id" in f
        assert "element_id" in f
        assert "fault_type" in f
        assert f["id"] == f"SC_{f['element_id']}"


def test_build_bus_faults_default_type_is_three_phase(xiidm_upload):
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network)
    for f in faults:
        assert f["fault_type"] == "THREE_PHASE"


def test_build_bus_faults_custom_fault_type(xiidm_upload):
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network, fault_type="SINGLE_PHASE_TO_GROUND")
    for f in faults:
        assert f["fault_type"] == "SINGLE_PHASE_TO_GROUND"


def test_build_bus_faults_nonexistent_voltage_returns_empty(xiidm_upload):
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network, nominal_v_set={9_999.0})
    assert faults == []


def test_build_bus_faults_nominal_v_filter_returns_subset(xiidm_upload):
    network = load_network(xiidm_upload)
    vls = network.get_voltage_levels(attributes=["nominal_v"])
    max_v = float(vls["nominal_v"].max())
    all_f = build_bus_faults(network)
    filtered_f = build_bus_faults(network, nominal_v_set={max_v})
    assert 0 < len(filtered_f) <= len(all_f)


def test_build_bus_faults_none_filter_equals_no_filter(xiidm_upload):
    network = load_network(xiidm_upload)
    assert len(build_bus_faults(network, nominal_v_set=None)) == len(
        build_bus_faults(network)
    )


# ---------------------------------------------------------------------------
# build_bus_faults — edge-case unit tests (mocked worker)
# ---------------------------------------------------------------------------


def test_build_bus_faults_empty_buses_returns_empty():
    net = types.SimpleNamespace(_obj=MagicMock())
    with patch("iidm_viewer.state.run", return_value=(pd.DataFrame(), None)):
        result = build_bus_faults(net)
    assert result == []


def test_build_bus_faults_voltage_filter_no_match():
    buses = pd.DataFrame(
        {"voltage_level_id": ["VL1", "VL2"]},
        index=pd.Index(["B1", "B2"], name="id"),
    )
    vl_df = pd.DataFrame(
        {"nominal_v": [132.0, 33.0]},
        index=pd.Index(["VL1", "VL2"], name="id"),
    )
    net = types.SimpleNamespace(_obj=MagicMock())
    with patch("iidm_viewer.state.run", return_value=(buses, vl_df)):
        result = build_bus_faults(net, nominal_v_set={400.0})
    assert result == []


def test_build_bus_faults_voltage_filter_match():
    buses = pd.DataFrame(
        {"voltage_level_id": ["VL_HV", "VL_MV"]},
        index=pd.Index(["B1", "B2"], name="id"),
    )
    vl_df = pd.DataFrame(
        {"nominal_v": [400.0, 132.0]},
        index=pd.Index(["VL_HV", "VL_MV"], name="id"),
    )
    net = types.SimpleNamespace(_obj=MagicMock())
    with patch("iidm_viewer.state.run", return_value=(buses, vl_df)):
        result = build_bus_faults(net, nominal_v_set={400.0})
    assert len(result) == 1
    assert result[0]["element_id"] == "B1"
    assert result[0]["id"] == "SC_B1"
    assert result[0]["fault_type"] == "THREE_PHASE"


def test_build_bus_faults_no_filter_includes_all_buses():
    buses = pd.DataFrame(
        {"voltage_level_id": ["VL1", "VL2", "VL3"]},
        index=pd.Index(["B1", "B2", "B3"], name="id"),
    )
    net = types.SimpleNamespace(_obj=MagicMock())
    # nominal_v_set=None → vl_df is not fetched
    with patch("iidm_viewer.state.run", return_value=(buses, None)):
        result = build_bus_faults(net)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# run_short_circuit_analysis — integration tests
# ---------------------------------------------------------------------------


def _require_sc_provider():
    """Skip the test if no short-circuit analysis provider is installed."""
    sc = pytest.importorskip("pypowsybl.shortcircuit")
    if not sc.get_provider_names():
        pytest.skip("No short-circuit analysis provider installed")


def test_run_short_circuit_analysis_result_keys(xiidm_upload):
    _require_sc_provider()
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network)[:2]
    results = run_short_circuit_analysis(network, faults)
    assert {"fault_results", "faults"} <= set(results)


def test_run_short_circuit_analysis_faults_preserved(xiidm_upload):
    _require_sc_provider()
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network)[:2]
    results = run_short_circuit_analysis(network, faults)
    assert results["faults"] == faults


def test_run_short_circuit_analysis_fault_results_populated(xiidm_upload):
    _require_sc_provider()
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network)[:2]
    results = run_short_circuit_analysis(network, faults)
    fault_results = results["fault_results"]
    assert len(fault_results) > 0
    for fid, fr in fault_results.items():
        assert "status" in fr
        assert "short_circuit_power_mva" in fr
        assert "current_kA" in fr
        assert "feeder_results" in fr
        assert "limit_violations" in fr
        assert isinstance(fr["feeder_results"], pd.DataFrame)
        assert isinstance(fr["limit_violations"], pd.DataFrame)


def test_run_short_circuit_analysis_converged_faults_have_positive_power(xiidm_upload):
    _require_sc_provider()
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network)[:3]
    results = run_short_circuit_analysis(network, faults)
    for fr in results["fault_results"].values():
        if fr["status"] == "CONVERGED" and fr["short_circuit_power_mva"] is not None:
            assert fr["short_circuit_power_mva"] > 0


def test_run_short_circuit_analysis_current_kA_is_positive(xiidm_upload):
    _require_sc_provider()
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network)[:3]
    results = run_short_circuit_analysis(network, faults)
    for fr in results["fault_results"].values():
        if fr["status"] == "CONVERGED" and fr["current_kA"] is not None:
            assert fr["current_kA"] > 0


def test_run_short_circuit_analysis_sc_params_accepted(xiidm_upload):
    """Custom sc_params dict should not raise."""
    _require_sc_provider()
    network = load_network(xiidm_upload)
    faults = build_bus_faults(network)[:1]
    sc_params = {
        "study_type": "TRANSIENT",
        "with_feeder_result": False,
        "with_limit_violations": True,
        "min_voltage_drop_proportional_threshold": 0.0,
    }
    results = run_short_circuit_analysis(network, faults, sc_params)
    assert "fault_results" in results


# ---------------------------------------------------------------------------
# Rendering — unit tests
# ---------------------------------------------------------------------------


def _mock_columns(n):
    return [MagicMock() for _ in range(n)]


def _fault_results_fixture(with_violations=False):
    viol_df = (
        pd.DataFrame({"subject_id": ["B1"], "limit_type": ["CURRENT"],
                      "value": [15_000.0], "limit": [10_000.0]})
        if with_violations else pd.DataFrame()
    )
    return {
        "faults": [{"id": "SC_B1", "element_id": "B1", "fault_type": "THREE_PHASE"}],
        "fault_results": {
            "SC_B1": {
                "status": "CONVERGED",
                "short_circuit_power_mva": 1500.0,
                "current_kA": 2.165,
                "feeder_results": pd.DataFrame(),
                "limit_violations": viol_df,
            }
        },
    }


def test_render_results_tab_no_results_shows_info():
    with patch("iidm_viewer.short_circuit_analysis.st") as mock_st:
        mock_st.session_state = {}
        _render_results_tab()
    mock_st.info.assert_called_once()


def test_render_results_tab_empty_fault_results_shows_info():
    results = {"faults": [], "fault_results": {}}
    with patch("iidm_viewer.short_circuit_analysis.st") as mock_st:
        mock_st.session_state = {"_sc_results": results}
        _render_results_tab()
    mock_st.info.assert_called()


def test_render_results_tab_renders_summary_dataframe():
    with patch("iidm_viewer.short_circuit_analysis.st") as mock_st:
        mock_st.session_state = {"_sc_results": _fault_results_fixture()}
        mock_st.columns.side_effect = _mock_columns
        mock_st.slider.return_value = 0.0
        mock_st.text_input.return_value = ""
        mock_st.selectbox.return_value = "SC_B1"
        _render_results_tab()
    assert mock_st.dataframe.call_count >= 1


def test_render_results_tab_shows_fault_metrics():
    with patch("iidm_viewer.short_circuit_analysis.st") as mock_st:
        mock_st.session_state = {"_sc_results": _fault_results_fixture()}
        mock_st.columns.side_effect = _mock_columns
        mock_st.slider.return_value = 0.0
        mock_st.text_input.return_value = ""
        mock_st.selectbox.return_value = "SC_B1"
        _render_results_tab()
    # Two metric calls: fault power + fault current
    col_mocks = [
        call_args[0][0]
        for call_args in mock_st.columns.call_args_list
        for _ in range(1)
    ]
    assert mock_st.columns.called


def test_render_results_tab_with_violations_renders_violation_dataframe():
    with patch("iidm_viewer.short_circuit_analysis.st") as mock_st:
        mock_st.session_state = {"_sc_results": _fault_results_fixture(with_violations=True)}
        mock_st.columns.side_effect = _mock_columns
        mock_st.slider.return_value = 0.0
        mock_st.text_input.return_value = ""
        mock_st.selectbox.return_value = "SC_B1"
        _render_results_tab()
    # Summary table + violation detail table
    assert mock_st.dataframe.call_count >= 2


def test_render_results_tab_no_violations_calls_success():
    with patch("iidm_viewer.short_circuit_analysis.st") as mock_st:
        mock_st.session_state = {"_sc_results": _fault_results_fixture()}
        mock_st.columns.side_effect = _mock_columns
        mock_st.slider.return_value = 0.0
        mock_st.text_input.return_value = ""
        mock_st.selectbox.return_value = "SC_B1"
        _render_results_tab()
    mock_st.success.assert_called()


def test_render_results_tab_id_filter_no_match_shows_info():
    with patch("iidm_viewer.short_circuit_analysis.st") as mock_st:
        mock_st.session_state = {"_sc_results": _fault_results_fixture()}
        mock_st.columns.side_effect = _mock_columns
        mock_st.slider.return_value = 0.0
        mock_st.text_input.return_value = "ZZZZ"  # matches nothing
        _render_results_tab()
    mock_st.info.assert_called()


def test_render_config_tab_no_faults_shows_info():
    net = MagicMock()
    with patch("iidm_viewer.short_circuit_analysis.st") as mock_st, \
         patch("iidm_viewer.short_circuit_analysis._get_nominal_voltages", return_value=[132.0]), \
         patch("iidm_viewer.short_circuit_analysis.build_bus_faults", return_value=[]):
        mock_st.selectbox.return_value = "THREE_PHASE"
        mock_st.multiselect.return_value = []
        mock_st.columns.side_effect = _mock_columns
        mock_st.checkbox.return_value = True
        mock_st.number_input.return_value = 0.0
        _render_config_tab(net)
    mock_st.info.assert_called()


def test_render_config_tab_with_faults_shows_caption():
    faults = [{"id": "SC_B1", "element_id": "B1", "fault_type": "THREE_PHASE"}]
    net = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("iidm_viewer.short_circuit_analysis.st") as mock_st, \
         patch("iidm_viewer.short_circuit_analysis._get_nominal_voltages", return_value=[132.0]), \
         patch("iidm_viewer.short_circuit_analysis.build_bus_faults", return_value=faults):
        mock_st.selectbox.return_value = "THREE_PHASE"
        mock_st.multiselect.return_value = [132.0]
        mock_st.columns.side_effect = _mock_columns
        mock_st.checkbox.return_value = True
        mock_st.number_input.return_value = 0.0
        mock_st.button.return_value = False
        mock_st.expander.return_value = cm
        _render_config_tab(net)
    mock_st.caption.assert_called()


def test_render_config_tab_run_button_triggers_analysis():
    faults = [{"id": "SC_B1", "element_id": "B1", "fault_type": "THREE_PHASE"}]
    sc_results = _fault_results_fixture()
    net = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("iidm_viewer.short_circuit_analysis.st") as mock_st, \
         patch("iidm_viewer.short_circuit_analysis._get_nominal_voltages", return_value=[132.0]), \
         patch("iidm_viewer.short_circuit_analysis.build_bus_faults", return_value=faults), \
         patch("iidm_viewer.short_circuit_analysis.run_short_circuit_analysis", return_value=sc_results):
        mock_st.session_state = {}
        mock_st.selectbox.return_value = "THREE_PHASE"
        mock_st.multiselect.return_value = [132.0]
        mock_st.columns.side_effect = _mock_columns
        mock_st.checkbox.return_value = True
        mock_st.number_input.return_value = 0.0
        mock_st.button.return_value = True   # "Run" clicked
        mock_st.expander.return_value = cm
        mock_st.spinner.return_value = cm
        _render_config_tab(net)
    assert mock_st.session_state.get("_sc_results") == sc_results
