"""Tests for security analysis state functions and rendering."""
import types
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from iidm_viewer.state import (
    build_n1_contingencies,
    load_network,
    run_security_analysis,
)
from iidm_viewer.security_analysis import (
    _render_config_tab,
    _render_contingencies_subtab,
    _render_limit_reductions_subtab,
    _render_monitored_subtab,
    _render_results_tab,
)


# ---------------------------------------------------------------------------
# build_n1_contingencies — integration tests (real IEEE14 network)
# ---------------------------------------------------------------------------


def test_build_n1_contingencies_lines_nonempty(xiidm_upload):
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")
    assert len(contingencies) > 0


def test_build_n1_contingencies_lines_structure(xiidm_upload):
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")
    for c in contingencies:
        assert "id" in c
        assert "element_id" in c
        assert c["id"] == f"N1_{c['element_id']}"


def test_build_n1_contingencies_transformers_nonempty(xiidm_upload):
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "2-Winding Transformers")
    assert len(contingencies) > 0


def test_build_n1_contingencies_unknown_type_returns_empty(xiidm_upload):
    network = load_network(xiidm_upload)
    assert build_n1_contingencies(network, "Unknown") == []


def test_build_n1_contingencies_nonexistent_voltage_returns_empty(xiidm_upload):
    network = load_network(xiidm_upload)
    assert build_n1_contingencies(network, "Lines", {9_999.0}) == []


def test_build_n1_contingencies_nominal_v_filter_returns_subset(xiidm_upload):
    network = load_network(xiidm_upload)
    vls = network.get_voltage_levels(attributes=["nominal_v"])
    max_v = float(vls["nominal_v"].max())
    all_c = build_n1_contingencies(network, "Lines")
    filtered_c = build_n1_contingencies(network, "Lines", {max_v})
    assert 0 < len(filtered_c) <= len(all_c)


def test_build_n1_contingencies_none_filter_equals_no_filter(xiidm_upload):
    network = load_network(xiidm_upload)
    assert len(build_n1_contingencies(network, "Lines", None)) == len(
        build_n1_contingencies(network, "Lines")
    )


# ---------------------------------------------------------------------------
# build_n1_contingencies — edge-case unit tests (mocked worker)
# ---------------------------------------------------------------------------


def test_build_n1_contingencies_empty_df_returns_empty():
    net = types.SimpleNamespace(_obj=MagicMock())
    with patch("iidm_viewer.state.run", return_value=(pd.DataFrame(), None)):
        result = build_n1_contingencies(net, "Lines")
    assert result == []


def test_build_n1_contingencies_voltage_filter_no_match():
    elem_df = pd.DataFrame(
        {"voltage_level1_id": ["VL1"], "voltage_level2_id": ["VL2"]},
        index=pd.Index(["L1"], name="id"),
    )
    vl_df = pd.DataFrame(
        {"nominal_v": [132.0, 33.0]},
        index=pd.Index(["VL1", "VL2"], name="id"),
    )
    net = types.SimpleNamespace(_obj=MagicMock())
    with patch("iidm_viewer.state.run", return_value=(elem_df, vl_df)):
        result = build_n1_contingencies(net, "Lines", {400.0})
    assert result == []


def test_build_n1_contingencies_voltage_filter_match_on_vl1():
    elem_df = pd.DataFrame(
        {"voltage_level1_id": ["VL_HV"], "voltage_level2_id": ["VL_MV"]},
        index=pd.Index(["L1"], name="id"),
    )
    vl_df = pd.DataFrame(
        {"nominal_v": [400.0, 132.0]},
        index=pd.Index(["VL_HV", "VL_MV"], name="id"),
    )
    net = types.SimpleNamespace(_obj=MagicMock())
    with patch("iidm_viewer.state.run", return_value=(elem_df, vl_df)):
        result = build_n1_contingencies(net, "Lines", {400.0})
    assert len(result) == 1
    assert result[0]["element_id"] == "L1"
    assert result[0]["id"] == "N1_L1"


def test_build_n1_contingencies_voltage_filter_match_on_vl2():
    """A line whose VL1 is 132 kV but VL2 is 400 kV should pass the 400 kV filter."""
    elem_df = pd.DataFrame(
        {"voltage_level1_id": ["VL_MV"], "voltage_level2_id": ["VL_HV"]},
        index=pd.Index(["T1"], name="id"),
    )
    vl_df = pd.DataFrame(
        {"nominal_v": [132.0, 400.0]},
        index=pd.Index(["VL_MV", "VL_HV"], name="id"),
    )
    net = types.SimpleNamespace(_obj=MagicMock())
    with patch("iidm_viewer.state.run", return_value=(elem_df, vl_df)):
        result = build_n1_contingencies(net, "2-Winding Transformers", {400.0})
    assert len(result) == 1
    assert result[0]["element_id"] == "T1"


# ---------------------------------------------------------------------------
# run_security_analysis — integration tests
# ---------------------------------------------------------------------------


def test_run_security_analysis_pre_converged(xiidm_upload):
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")[:3]
    results = run_security_analysis(network, contingencies)
    assert results["pre_status"] == "CONVERGED"


def test_run_security_analysis_result_keys(xiidm_upload):
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")[:2]
    results = run_security_analysis(network, contingencies)
    assert {"pre_status", "pre_violations", "post", "contingencies"} <= set(results)


def test_run_security_analysis_post_has_entry_per_contingency(xiidm_upload):
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")[:2]
    results = run_security_analysis(network, contingencies)
    for c in contingencies:
        assert c["id"] in results["post"]
        entry = results["post"][c["id"]]
        assert "status" in entry
        assert "limit_violations" in entry
        assert isinstance(entry["limit_violations"], pd.DataFrame)


def test_run_security_analysis_pre_violations_is_dataframe(xiidm_upload):
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")[:1]
    results = run_security_analysis(network, contingencies)
    assert isinstance(results["pre_violations"], pd.DataFrame)


def test_run_security_analysis_contingencies_preserved(xiidm_upload):
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")[:2]
    results = run_security_analysis(network, contingencies)
    assert results["contingencies"] == contingencies


def test_run_security_analysis_empty_contingencies(xiidm_upload):
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    results = run_security_analysis(network, [])
    assert "pre_status" in results
    assert isinstance(results["post"], dict)
    assert results["post"] == {}


def test_run_security_analysis_result_includes_monitored_keys(xiidm_upload):
    """New monitored-element result keys are always present (possibly empty)."""
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")[:1]
    results = run_security_analysis(network, contingencies)
    assert {
        "pre_branch_results",
        "pre_bus_results",
        "pre_3wt_results",
    } <= set(results)
    for entry in results["post"].values():
        assert {
            "branch_results",
            "bus_results",
            "three_windings_transformer_results",
        } <= set(entry)


def test_run_security_analysis_monitored_branches_populated(xiidm_upload):
    """Declaring monitored branches returns non-empty branch_results."""
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")[:1]
    lines = network.get_lines(attributes=[])
    branch_ids = list(lines.index[:3])
    monitored = [{
        "contingency_context_type": "ALL",
        "contingency_ids": None,
        "branch_ids": branch_ids,
        "voltage_level_ids": None,
        "three_windings_transformer_ids": None,
    }]
    results = run_security_analysis(
        network,
        contingencies,
        monitored_elements=monitored,
    )
    assert not results["pre_branch_results"].empty


def test_run_security_analysis_limit_reduction_accepted(xiidm_upload):
    """A limit reduction entry is accepted and the run still converges."""
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")[:1]
    reductions = [{
        "limit_type": "CURRENT",
        "permanent": True,
        "temporary": True,
        "value": 0.9,
        "contingency_context": "ALL",
    }]
    results = run_security_analysis(
        network,
        contingencies,
        limit_reductions=reductions,
    )
    assert results["pre_status"] == "CONVERGED"


# ---------------------------------------------------------------------------
# Rendering — unit tests
# ---------------------------------------------------------------------------


def _mock_columns(n):
    count = n if isinstance(n, int) else len(n)
    return [MagicMock() for _ in range(count)]


def _converged_results(violations=False):
    viol_df = (
        pd.DataFrame({"subject_id": ["L1"], "limit_type": ["CURRENT"],
                      "value": [900.0], "limit": [800.0]})
        if violations else pd.DataFrame()
    )
    return {
        "contingencies": [{"id": "N1_L1", "element_id": "L1"}],
        "pre_status": "CONVERGED",
        "pre_violations": pd.DataFrame(),
        "post": {"N1_L1": {"status": "CONVERGED", "limit_violations": viol_df}},
    }


def test_render_results_tab_no_results_shows_info():
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = {}
        _render_results_tab()
    mock_st.info.assert_called_once()


def test_render_results_tab_empty_post_shows_info():
    results = {
        "contingencies": [{"id": "N1_L1", "element_id": "L1"}],
        "pre_status": "CONVERGED",
        "pre_violations": pd.DataFrame(),
        "post": {},
    }
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = {"_sa_results": results}
        mock_st.columns.side_effect = _mock_columns
        _render_results_tab()
    mock_st.info.assert_called()


def test_render_results_tab_converged_no_violations_calls_success():
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = {"_sa_results": _converged_results()}
        mock_st.columns.side_effect = _mock_columns
        mock_st.slider.return_value = 0
        mock_st.text_input.return_value = ""
        mock_st.selectbox.return_value = "N1_L1"
        _render_results_tab()
    mock_st.success.assert_called()


def test_render_results_tab_renders_summary_dataframe():
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = {"_sa_results": _converged_results()}
        mock_st.columns.side_effect = _mock_columns
        mock_st.slider.return_value = 0
        mock_st.text_input.return_value = ""
        mock_st.selectbox.return_value = "N1_L1"
        _render_results_tab()
    assert mock_st.dataframe.call_count >= 1


def test_render_results_tab_with_violations_renders_violation_dataframe():
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = {"_sa_results": _converged_results(violations=True)}
        mock_st.columns.side_effect = _mock_columns
        mock_st.slider.return_value = 0
        mock_st.text_input.return_value = ""
        mock_st.selectbox.return_value = "N1_L1"
        _render_results_tab()
    # Summary table + violation detail table
    assert mock_st.dataframe.call_count >= 2


def test_render_results_tab_id_filter_no_match_shows_info():
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = {"_sa_results": _converged_results()}
        mock_st.columns.side_effect = _mock_columns
        mock_st.slider.return_value = 0
        mock_st.text_input.return_value = "ZZZZ"  # matches nothing
        _render_results_tab()
    mock_st.info.assert_called()


def _cm():
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_render_contingencies_subtab_empty_shows_info():
    net = MagicMock()
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_nominal_voltages", return_value=[132.0]), \
         patch("iidm_viewer.security_analysis.build_n1_contingencies", return_value=[]):
        mock_st.session_state = {}
        mock_st.selectbox.return_value = "Lines"
        mock_st.multiselect.return_value = []
        _render_contingencies_subtab(net)
    mock_st.info.assert_called()


def test_render_contingencies_subtab_stores_in_session():
    contingencies = [{"id": "N1_L1", "element_id": "L1"}]
    net = MagicMock()
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_nominal_voltages", return_value=[132.0]), \
         patch("iidm_viewer.security_analysis.build_n1_contingencies", return_value=contingencies):
        mock_st.session_state = {}
        mock_st.selectbox.return_value = "Lines"
        mock_st.multiselect.return_value = [132.0]
        mock_st.expander.return_value = _cm()
        _render_contingencies_subtab(net)
    assert mock_st.session_state["_sa_contingencies"] == contingencies
    mock_st.caption.assert_called()


def test_render_monitored_subtab_empty_shows_info():
    net = MagicMock()
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch(
             "iidm_viewer.security_analysis._get_ids",
             return_value={
                 "branches": ["L1"],
                 "voltage_levels": ["VL1"],
                 "three_windings_transformers": [],
             },
         ):
        mock_st.session_state = {}
        mock_st.form.return_value = _cm()
        mock_st.selectbox.return_value = "ALL"
        mock_st.multiselect.return_value = []
        mock_st.form_submit_button.return_value = False
        _render_monitored_subtab(net)
    mock_st.info.assert_called()


def test_render_monitored_subtab_form_submit_appends_entry():
    net = MagicMock()
    state: dict = {}
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch(
             "iidm_viewer.security_analysis._get_ids",
             return_value={
                 "branches": ["L1"],
                 "voltage_levels": [],
                 "three_windings_transformers": [],
             },
         ):
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.container.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        mock_st.selectbox.return_value = "ALL"
        # multiselect order: branches, voltage_levels, 3WTs
        mock_st.multiselect.side_effect = [["L1"], [], []]
        mock_st.form_submit_button.return_value = True
        mock_st.button.return_value = False  # avoid Remove-click in render loop
        _render_monitored_subtab(net)
    assert state["_sa_monitored"] == [{
        "contingency_context_type": "ALL",
        "contingency_ids": None,
        "branch_ids": ["L1"],
        "voltage_level_ids": None,
        "three_windings_transformer_ids": None,
    }]
    mock_st.rerun.assert_called()


def test_render_monitored_subtab_specific_requires_contingencies():
    net = MagicMock()
    state: dict = {"_sa_contingencies": [{"id": "N1_L1", "element_id": "L1"}]}
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch(
             "iidm_viewer.security_analysis._get_ids",
             return_value={
                 "branches": ["L2"],
                 "voltage_levels": [],
                 "three_windings_transformers": [],
             },
         ):
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.selectbox.return_value = "SPECIFIC"
        mock_st.multiselect.side_effect = [[], ["L2"], [], []]
        mock_st.form_submit_button.return_value = True
        _render_monitored_subtab(net)
    assert state.get("_sa_monitored") == []
    mock_st.warning.assert_called()


def test_render_limit_reductions_subtab_empty_shows_info():
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = {}
        mock_st.form.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        mock_st.number_input.return_value = 0.9
        mock_st.checkbox.return_value = True
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = False
        _render_limit_reductions_subtab()
    mock_st.info.assert_called()


def test_render_limit_reductions_subtab_submit_appends_entry():
    state: dict = {}
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.container.return_value = _cm()
        mock_st.expander.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        # inputs: value=0.8, min_dur=0, max_dur=0, min_v=0, max_v=0
        mock_st.number_input.side_effect = [0.8, 0, 0, 0, 0]
        mock_st.checkbox.side_effect = [True, False]
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = True
        mock_st.button.return_value = False  # avoid Remove-click in render loop
        _render_limit_reductions_subtab()
    assert state["_sa_limit_reductions"] == [{
        "limit_type": "CURRENT",
        "permanent": True,
        "temporary": False,
        "value": 0.8,
        "contingency_context": "ALL",
    }]
    mock_st.rerun.assert_called()


def test_render_limit_reductions_subtab_none_selected_warns():
    state: dict = {}
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        mock_st.number_input.side_effect = [0.8, 0, 0, 0, 0]
        # Neither permanent nor temporary
        mock_st.checkbox.side_effect = [False, False]
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = True
        _render_limit_reductions_subtab()
    assert state.get("_sa_limit_reductions") == []
    mock_st.warning.assert_called()


def test_render_config_tab_run_button_passes_monitored_and_reductions():
    contingencies = [{"id": "N1_L1", "element_id": "L1"}]
    monitored = [{
        "contingency_context_type": "ALL",
        "contingency_ids": None,
        "branch_ids": ["L1"],
        "voltage_level_ids": None,
        "three_windings_transformer_ids": None,
    }]
    reductions = [{
        "limit_type": "CURRENT",
        "permanent": True,
        "temporary": True,
        "value": 0.9,
        "contingency_context": "ALL",
    }]
    sa_results = {
        "pre_status": "CONVERGED",
        "pre_violations": pd.DataFrame(),
        "post": {"N1_L1": {"status": "CONVERGED", "limit_violations": pd.DataFrame()}},
        "contingencies": contingencies,
    }
    net = MagicMock()
    state = {
        "_sa_contingencies": contingencies,
        "_sa_monitored": monitored,
        "_sa_limit_reductions": reductions,
    }
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._render_contingencies_subtab"), \
         patch("iidm_viewer.security_analysis._render_monitored_subtab"), \
         patch("iidm_viewer.security_analysis._render_limit_reductions_subtab"), \
         patch(
             "iidm_viewer.security_analysis.run_security_analysis",
             return_value=sa_results,
         ) as mock_run:
        mock_st.session_state = state
        mock_st.tabs.return_value = (_cm(), _cm(), _cm())
        mock_st.columns.side_effect = _mock_columns
        mock_st.button.return_value = True
        mock_st.spinner.return_value = _cm()
        _render_config_tab(net)

    assert state.get("_sa_results") == sa_results
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs["monitored_elements"] == monitored
    assert kwargs["limit_reductions"] == reductions
