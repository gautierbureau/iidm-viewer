"""Tests for security analysis state functions and rendering."""
import types
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from iidm_viewer.state import (
    build_n1_contingencies,
    build_n2_contingencies,
    load_network,
    run_security_analysis,
)
from iidm_viewer.security_analysis import (
    _action_summary,
    _get_filterable_df,
    _render_actions_subtab,
    _render_config_tab,
    _render_contingencies_subtab,
    _render_limit_reductions_subtab,
    _render_monitored_subtab,
    _render_operator_strategies_subtab,
    _render_results_tab,
)
from iidm_viewer.state import _apply_action


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
# build_n2_contingencies
# ---------------------------------------------------------------------------


def test_build_n2_contingencies_returns_unordered_pairs(xiidm_upload):
    network = load_network(xiidm_upload)
    n1 = build_n1_contingencies(network, "Lines")
    n2 = build_n2_contingencies(network, "Lines")
    n = len(n1)
    assert len(n2) == n * (n - 1) // 2
    # Each entry has two element ids and an N2_ prefix
    for c in n2:
        assert c["id"].startswith("N2_")
        assert "element_ids" in c and len(c["element_ids"]) == 2
        a, b = c["element_ids"]
        assert a < b


def test_build_n2_contingencies_respects_voltage_filter(xiidm_upload):
    network = load_network(xiidm_upload)
    # pick a voltage that yields zero N-1 ⇒ zero N-2
    assert build_n2_contingencies(network, "Lines", {9_999.0}) == []


# ---------------------------------------------------------------------------
# run_security_analysis — integration tests
# ---------------------------------------------------------------------------


def test_run_security_analysis_n2_dispatches_multiple_elements(xiidm_upload):
    """Two-element contingency should route through add_multiple_elements_contingency."""
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    n1 = build_n1_contingencies(network, "Lines")[:2]
    ids = [c["element_id"] for c in n1]
    multi = [{"id": f"N2_{ids[0]}_{ids[1]}", "element_ids": ids}]
    results = run_security_analysis(network, multi)
    assert "post" in results
    assert f"N2_{ids[0]}_{ids[1]}" in results["post"]


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
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()), \
         patch("iidm_viewer.security_analysis.build_n1_contingencies", return_value=[]), \
         patch("iidm_viewer.security_analysis.build_n2_contingencies", return_value=[]):
        mock_st.session_state = {}
        mock_st.form.return_value = _cm()
        mock_st.radio.return_value = "N-1"
        mock_st.selectbox.return_value = "Lines"
        mock_st.multiselect.return_value = []
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = False
        _render_contingencies_subtab(net)
    mock_st.info.assert_called()


def test_render_contingencies_subtab_stores_in_session():
    contingencies = [{"id": "N1_L1", "element_id": "L1", "element_ids": ["L1"]}]
    net = MagicMock()
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_nominal_voltages", return_value=[132.0]), \
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()), \
         patch("iidm_viewer.security_analysis.build_n1_contingencies", return_value=contingencies), \
         patch("iidm_viewer.security_analysis.build_n2_contingencies", return_value=[]):
        mock_st.session_state = {}
        mock_st.form.return_value = _cm()
        mock_st.radio.return_value = "N-1"
        mock_st.selectbox.return_value = "Lines"
        mock_st.multiselect.return_value = [132.0]
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = False
        mock_st.expander.return_value = _cm()
        _render_contingencies_subtab(net)
    assert mock_st.session_state["_sa_contingencies"] == contingencies
    mock_st.caption.assert_called()


def test_render_contingencies_subtab_n2_mode_uses_n2_builder():
    net = MagicMock()
    n2 = [
        {"id": "N2_L1_L2", "element_ids": ["L1", "L2"]},
        {"id": "N2_L1_L3", "element_ids": ["L1", "L3"]},
    ]
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_nominal_voltages", return_value=[132.0]), \
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()), \
         patch("iidm_viewer.security_analysis.build_n1_contingencies", return_value=[]), \
         patch("iidm_viewer.security_analysis.build_n2_contingencies", return_value=n2) as mock_n2:
        mock_st.session_state = {}
        mock_st.form.return_value = _cm()
        mock_st.radio.return_value = "N-2"
        mock_st.selectbox.return_value = "Lines"
        mock_st.multiselect.return_value = []
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = False
        mock_st.expander.return_value = _cm()
        _render_contingencies_subtab(net)
    mock_n2.assert_called_once()
    assert mock_st.session_state["_sa_contingencies"] == n2


def test_render_contingencies_subtab_manual_n1_per_element():
    net = MagicMock()
    state: dict = {}
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_nominal_voltages", return_value=[]), \
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()), \
         patch("iidm_viewer.security_analysis.build_n1_contingencies", return_value=[]), \
         patch("iidm_viewer.security_analysis.build_n2_contingencies", return_value=[]):
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.container.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        mock_st.radio.side_effect = ["N-1", "One contingency per element (N-1)"]
        # selectbox: auto element type, manual element type
        mock_st.selectbox.side_effect = ["Lines", "Lines"]
        mock_st.multiselect.return_value = ["L1"]
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = True
        mock_st.button.return_value = False
        mock_st.expander.return_value = _cm()
        _render_contingencies_subtab(net)
    assert state["_sa_manual_contingencies"] == [
        {"id": "N1_L1", "element_id": "L1", "element_ids": ["L1"]},
    ]
    assert state["_sa_contingencies"] == state["_sa_manual_contingencies"]


def test_render_contingencies_subtab_manual_grouped_nk():
    net = MagicMock()
    ids = dict(_ids_fixture(), lines=["L1", "L2", "L3"])
    state: dict = {}
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_nominal_voltages", return_value=[]), \
         patch("iidm_viewer.security_analysis._get_ids", return_value=ids), \
         patch("iidm_viewer.security_analysis.build_n1_contingencies", return_value=[]), \
         patch("iidm_viewer.security_analysis.build_n2_contingencies", return_value=[]):
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.container.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        mock_st.radio.side_effect = ["N-1", "Single grouped contingency (N-k)"]
        mock_st.selectbox.side_effect = ["Lines", "Lines"]
        mock_st.multiselect.return_value = ["L1", "L2"]
        mock_st.text_input.return_value = "grouped_outage"
        mock_st.form_submit_button.return_value = True
        mock_st.button.return_value = False
        mock_st.expander.return_value = _cm()
        _render_contingencies_subtab(net)
    assert state["_sa_manual_contingencies"] == [
        {"id": "grouped_outage", "element_ids": ["L1", "L2"]},
    ]


# ---------------------------------------------------------------------------
# _get_filterable_df + filter integration in manual form
# ---------------------------------------------------------------------------


def test_get_filterable_df_caches_per_network_and_type():
    net = MagicMock()
    df = pd.DataFrame({"p": [1.0, 2.0]}, index=pd.Index(["L1", "L2"], name="id"))
    net.get_lines.return_value = df
    net.get_lines.__name__ = "get_lines"
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis.build_vl_lookup",
               return_value=pd.DataFrame(
                   columns=["id", "substation_id", "nominal_v", "country"])), \
         patch(
             "iidm_viewer.security_analysis.enrich_with_joins",
             side_effect=lambda d, _: d,
         ):
        mock_st.session_state = {}
        first = _get_filterable_df(net, "Lines")
        second = _get_filterable_df(net, "Lines")
    assert list(first.index) == ["L1", "L2"]
    assert second is first  # cached object reused
    net.get_lines.assert_called_once_with(all_attributes=True)


def test_get_filterable_df_empty_returns_empty_dataframe():
    net = MagicMock()
    net.get_generators.return_value = pd.DataFrame()
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = {}
        out = _get_filterable_df(net, "Generators")
    assert out.empty


def test_render_contingencies_subtab_manual_options_come_from_filtered_df():
    """Manual multiselect options reflect the filtered DataFrame index, not
    the raw id list from _get_ids."""
    net = MagicMock()
    state: dict = {}
    raw_df = pd.DataFrame({"p": [1.0, 2.0, 3.0]},
                          index=pd.Index(["L1", "L2", "L3"], name="id"))
    narrowed = raw_df.loc[["L2"]]
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_nominal_voltages", return_value=[]), \
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()), \
         patch("iidm_viewer.security_analysis._get_filterable_df", return_value=raw_df), \
         patch("iidm_viewer.security_analysis.render_filters", return_value=narrowed) as mock_flt, \
         patch("iidm_viewer.security_analysis.build_n1_contingencies", return_value=[]), \
         patch("iidm_viewer.security_analysis.build_n2_contingencies", return_value=[]):
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.container.return_value = _cm()
        mock_st.radio.side_effect = ["N-1", "One contingency per element (N-1)"]
        mock_st.selectbox.side_effect = ["Lines", "Lines"]
        mock_st.multiselect.return_value = []
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = False
        mock_st.expander.return_value = _cm()
        _render_contingencies_subtab(net)
    mock_flt.assert_called_once()
    # multiselect for manual ids called with the narrowed index (["L2"])
    id_multiselect_calls = [
        call for call in mock_st.multiselect.call_args_list
        if call.kwargs.get("key") == "sa_manual_ids"
        or (len(call.args) >= 1 and "lines to include" in str(call.args[0]).lower())
    ]
    assert id_multiselect_calls
    options = id_multiselect_calls[0].kwargs.get("options") or id_multiselect_calls[0].args[1]
    assert list(options) == ["L2"]


def test_render_contingencies_subtab_manual_grouped_requires_id():
    net = MagicMock()
    state: dict = {}
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_nominal_voltages", return_value=[]), \
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()), \
         patch("iidm_viewer.security_analysis.build_n1_contingencies", return_value=[]), \
         patch("iidm_viewer.security_analysis.build_n2_contingencies", return_value=[]):
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.radio.side_effect = ["N-1", "Single grouped contingency (N-k)"]
        mock_st.selectbox.side_effect = ["Lines", "Lines"]
        mock_st.multiselect.return_value = ["L1"]
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = True
        mock_st.expander.return_value = _cm()
        _render_contingencies_subtab(net)
    assert state.get("_sa_manual_contingencies") == []
    mock_st.warning.assert_called()


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


def test_render_config_tab_run_button_passes_all_inputs():
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
    actions = [{
        "action_id": "open_L1",
        "type": "SWITCH",
        "switch_id": "SW1",
        "open": True,
    }]
    strategies = [{
        "operator_strategy_id": "strat1",
        "contingency_id": "N1_L1",
        "action_ids": ["open_L1"],
        "condition_type": "TRUE_CONDITION",
    }]
    sa_results = {
        "pre_status": "CONVERGED",
        "pre_violations": pd.DataFrame(),
        "post": {"N1_L1": {"status": "CONVERGED", "limit_violations": pd.DataFrame()}},
        "operator_strategies": {},
        "contingencies": contingencies,
    }
    net = MagicMock()
    state = {
        "_sa_contingencies": contingencies,
        "_sa_monitored": monitored,
        "_sa_limit_reductions": reductions,
        "_sa_actions": actions,
        "_sa_operator_strategies": strategies,
    }
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._render_contingencies_subtab"), \
         patch("iidm_viewer.security_analysis._render_monitored_subtab"), \
         patch("iidm_viewer.security_analysis._render_limit_reductions_subtab"), \
         patch("iidm_viewer.security_analysis._render_actions_subtab"), \
         patch("iidm_viewer.security_analysis._render_operator_strategies_subtab"), \
         patch(
             "iidm_viewer.security_analysis.run_security_analysis",
             return_value=sa_results,
         ) as mock_run:
        mock_st.session_state = state
        mock_st.tabs.return_value = (_cm(), _cm(), _cm(), _cm(), _cm())
        mock_st.columns.side_effect = _mock_columns
        mock_st.button.return_value = True
        mock_st.spinner.return_value = _cm()
        _render_config_tab(net)

    assert state.get("_sa_results") == sa_results
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs["monitored_elements"] == monitored
    assert kwargs["limit_reductions"] == reductions
    assert kwargs["actions"] == actions
    assert kwargs["operator_strategies"] == strategies


# ---------------------------------------------------------------------------
# Actions sub-tab + _action_summary
# ---------------------------------------------------------------------------


def test_action_summary_switch():
    s = _action_summary({
        "action_id": "a1", "type": "SWITCH", "switch_id": "SW1", "open": True,
    })
    assert "SWITCH" in s and "SW1" in s and "open" in s


def test_action_summary_terminals():
    s = _action_summary({
        "action_id": "a2", "type": "TERMINALS_CONNECTION",
        "element_id": "L1", "opening": False, "side": "ONE",
    })
    assert "TERMINALS" in s and "close" in s and "side ONE" in s


def test_action_summary_generator():
    s = _action_summary({
        "action_id": "a3", "type": "GENERATOR_ACTIVE_POWER",
        "generator_id": "G1", "is_relative": True, "active_power": -10.0,
    })
    assert "GEN P" in s and "G1" in s and "Δ-10" in s


def test_action_summary_ptc():
    s = _action_summary({
        "action_id": "a4", "type": "PHASE_TAP_CHANGER_POSITION",
        "transformer_id": "T1", "is_relative": False, "tap_position": 3,
    })
    assert "PTC" in s and "T1" in s and "=3" in s


def test_action_summary_load():
    s = _action_summary({
        "action_id": "a5", "type": "LOAD_ACTIVE_POWER",
        "load_id": "LD1", "is_relative": True, "active_power": 15.0,
    })
    assert "LOAD P" in s and "LD1" in s and "Δ15" in s


def test_action_summary_rtc():
    s = _action_summary({
        "action_id": "a6", "type": "RATIO_TAP_CHANGER_POSITION",
        "transformer_id": "T2", "is_relative": True, "tap_position": -2,
    })
    assert "RTC" in s and "T2" in s and "Δ-2" in s


def test_action_summary_shunt():
    s = _action_summary({
        "action_id": "a7", "type": "SHUNT_COMPENSATOR_POSITION",
        "shunt_id": "SH1", "section": 2,
    })
    assert "SHUNT" in s and "SH1" in s and "section=2" in s


def _ids_fixture():
    return {
        "branches": ["L1"],
        "lines": ["L1"],
        "two_windings_transformers": [],
        "three_windings_transformers": [],
        "voltage_levels": ["VL1"],
        "switches": ["SW1"],
        "generators": ["G1"],
        "loads": ["LD1"],
        "shunt_compensators": ["SH1"],
        "phase_tap_changers": ["T1"],
        "ratio_tap_changers": ["T2"],
        "connectables": ["L1", "G1"],
    }


def test_render_actions_subtab_empty_shows_info():
    net = MagicMock()
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()):
        mock_st.session_state = {}
        mock_st.form.return_value = _cm()
        mock_st.selectbox.return_value = "SWITCH"
        mock_st.text_input.return_value = ""
        mock_st.checkbox.return_value = True
        mock_st.form_submit_button.return_value = False
        _render_actions_subtab(net)
    mock_st.info.assert_called()


def test_render_actions_subtab_submit_appends_switch_action():
    net = MagicMock()
    state: dict = {}
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()):
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.container.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        # outer + inner selectbox: action type, then switch_id
        mock_st.selectbox.side_effect = ["SWITCH", "SW1"]
        mock_st.text_input.return_value = "open_sw1"
        mock_st.checkbox.return_value = True
        mock_st.form_submit_button.return_value = True
        mock_st.button.return_value = False
        _render_actions_subtab(net)
    assert state["_sa_actions"] == [
        {"action_id": "open_sw1", "type": "SWITCH", "switch_id": "SW1", "open": True},
    ]
    mock_st.rerun.assert_called()


def test_render_actions_subtab_duplicate_id_warns():
    net = MagicMock()
    state = {"_sa_actions": [
        {"action_id": "a1", "type": "SWITCH", "switch_id": "SW1", "open": True},
    ]}
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()):
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.container.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        mock_st.selectbox.side_effect = ["SWITCH", "SW1"]
        mock_st.text_input.return_value = "a1"
        mock_st.checkbox.return_value = True
        mock_st.form_submit_button.return_value = True
        mock_st.button.return_value = False
        _render_actions_subtab(net)
    assert len(state["_sa_actions"]) == 1
    mock_st.warning.assert_called()


def test_render_actions_subtab_blank_id_warns():
    net = MagicMock()
    state: dict = {}
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()):
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        mock_st.selectbox.side_effect = ["SWITCH", "SW1"]
        mock_st.text_input.return_value = ""
        mock_st.checkbox.return_value = True
        mock_st.form_submit_button.return_value = True
        _render_actions_subtab(net)
    assert state.get("_sa_actions") == []
    mock_st.warning.assert_called()


# ---------------------------------------------------------------------------
# Operator strategies sub-tab
# ---------------------------------------------------------------------------


def test_render_operator_strategies_subtab_no_inputs_shows_info():
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = {}
        _render_operator_strategies_subtab()
    mock_st.info.assert_called()


def test_render_operator_strategies_subtab_submit_appends():
    state = {
        "_sa_contingencies": [{"id": "N1_L1", "element_id": "L1"}],
        "_sa_actions": [
            {"action_id": "a1", "type": "SWITCH", "switch_id": "SW1", "open": True},
        ],
    }
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.container.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        mock_st.text_input.return_value = "strat1"
        # selectbox order: condition_type (outside form), then contingency (inside form)
        mock_st.selectbox.side_effect = ["TRUE_CONDITION", "N1_L1"]
        mock_st.multiselect.return_value = ["a1"]
        mock_st.form_submit_button.return_value = True
        mock_st.button.return_value = False
        _render_operator_strategies_subtab()
    assert state["_sa_operator_strategies"] == [{
        "operator_strategy_id": "strat1",
        "contingency_id": "N1_L1",
        "action_ids": ["a1"],
        "condition_type": "TRUE_CONDITION",
        "violation_subject_ids": [],
        "violation_types": [],
    }]
    mock_st.rerun.assert_called()


def test_render_operator_strategies_subtab_violation_condition_captures_filters():
    state = {
        "_sa_contingencies": [{"id": "N1_L1", "element_id": "L1"}],
        "_sa_actions": [
            {"action_id": "a1", "type": "SWITCH", "switch_id": "SW1", "open": True},
        ],
    }
    with patch("iidm_viewer.security_analysis.st") as mock_st, \
         patch("iidm_viewer.security_analysis._get_ids", return_value=_ids_fixture()):
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.container.return_value = _cm()
        mock_st.columns.side_effect = _mock_columns
        mock_st.text_input.return_value = "strat1"
        mock_st.selectbox.side_effect = ["ANY_VIOLATION_CONDITION", "N1_L1"]
        # multiselect order: actions, violation_subject_ids, violation_types
        mock_st.multiselect.side_effect = [["a1"], ["L1"], ["CURRENT"]]
        mock_st.form_submit_button.return_value = True
        mock_st.button.return_value = False
        _render_operator_strategies_subtab(network=MagicMock())
    s = state["_sa_operator_strategies"][0]
    assert s["condition_type"] == "ANY_VIOLATION_CONDITION"
    assert s["violation_subject_ids"] == ["L1"]
    assert s["violation_types"] == ["CURRENT"]


def test_render_operator_strategies_subtab_no_actions_warns():
    state = {
        "_sa_contingencies": [{"id": "N1_L1", "element_id": "L1"}],
        "_sa_actions": [
            {"action_id": "a1", "type": "SWITCH", "switch_id": "SW1", "open": True},
        ],
    }
    with patch("iidm_viewer.security_analysis.st") as mock_st:
        mock_st.session_state = state
        mock_st.form.return_value = _cm()
        mock_st.text_input.return_value = "strat1"
        mock_st.selectbox.side_effect = ["TRUE_CONDITION", "N1_L1"]
        mock_st.multiselect.return_value = []
        mock_st.form_submit_button.return_value = True
        _render_operator_strategies_subtab()
    assert state.get("_sa_operator_strategies") == []
    mock_st.warning.assert_called()


# ---------------------------------------------------------------------------
# state._apply_action — direct unit tests
# ---------------------------------------------------------------------------


def test_apply_action_switch_calls_pypowsybl():
    analysis = MagicMock()
    _apply_action(analysis, {
        "action_id": "a", "type": "SWITCH", "switch_id": "SW1", "open": True,
    })
    analysis.add_switch_action.assert_called_once_with("a", "SW1", True)


def test_apply_action_terminals_passes_side_enum():
    analysis = MagicMock()
    _apply_action(analysis, {
        "action_id": "a", "type": "TERMINALS_CONNECTION",
        "element_id": "L1", "opening": False, "side": "TWO",
    })
    args, kwargs = analysis.add_terminals_connection_action.call_args
    assert args == ("a", "L1")
    assert kwargs["opening"] is False
    assert kwargs["side"].name == "TWO"


def test_apply_action_generator():
    analysis = MagicMock()
    _apply_action(analysis, {
        "action_id": "a", "type": "GENERATOR_ACTIVE_POWER",
        "generator_id": "G1", "is_relative": True, "active_power": -5.0,
    })
    analysis.add_generator_active_power_action.assert_called_once_with(
        "a", "G1", True, -5.0,
    )


def test_apply_action_ptc():
    analysis = MagicMock()
    _apply_action(analysis, {
        "action_id": "a", "type": "PHASE_TAP_CHANGER_POSITION",
        "transformer_id": "T1", "is_relative": False, "tap_position": 3,
    })
    args, kwargs = analysis.add_phase_tap_changer_position_action.call_args
    assert args == ("a", "T1", False, 3)
    assert kwargs["side"].name == "NONE"


def test_apply_action_load():
    analysis = MagicMock()
    _apply_action(analysis, {
        "action_id": "a", "type": "LOAD_ACTIVE_POWER",
        "load_id": "LD1", "is_relative": False, "active_power": 20.0,
    })
    analysis.add_load_active_power_action.assert_called_once_with(
        "a", "LD1", False, 20.0,
    )


def test_apply_action_rtc():
    analysis = MagicMock()
    _apply_action(analysis, {
        "action_id": "a", "type": "RATIO_TAP_CHANGER_POSITION",
        "transformer_id": "T2", "is_relative": True, "tap_position": -1,
        "side": "ONE",
    })
    args, kwargs = analysis.add_ratio_tap_changer_position_action.call_args
    assert args == ("a", "T2", True, -1)
    assert kwargs["side"].name == "ONE"


def test_apply_action_shunt():
    analysis = MagicMock()
    _apply_action(analysis, {
        "action_id": "a", "type": "SHUNT_COMPENSATOR_POSITION",
        "shunt_id": "SH1", "section": 2,
    })
    analysis.add_shunt_compensator_position_action.assert_called_once_with(
        "a", "SH1", 2,
    )


def test_apply_action_unknown_raises():
    analysis = MagicMock()
    with pytest.raises(ValueError):
        _apply_action(analysis, {"action_id": "a", "type": "BOGUS"})


# ---------------------------------------------------------------------------
# run_security_analysis — operator strategies integration
# ---------------------------------------------------------------------------


def test_run_security_analysis_operator_strategy_round_trip(xiidm_upload):
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")[:1]
    cid = contingencies[0]["id"]
    gens = network.get_generators(attributes=[])
    gen_id = list(gens.index)[0]
    actions = [{
        "action_id": "gen_down",
        "type": "GENERATOR_ACTIVE_POWER",
        "generator_id": gen_id,
        "is_relative": True,
        "active_power": -10.0,
    }]
    strategies = [{
        "operator_strategy_id": "strat1",
        "contingency_id": cid,
        "action_ids": ["gen_down"],
        "condition_type": "TRUE_CONDITION",
    }]
    results = run_security_analysis(
        network,
        contingencies,
        actions=actions,
        operator_strategies=strategies,
    )
    assert "operator_strategies" in results
    osr = results["operator_strategies"].get("strat1")
    assert osr is not None
    assert isinstance(osr["status"], str) and osr["status"]
    assert osr["contingency_id"] == cid
    assert osr["action_ids"] == ["gen_down"]


def test_run_security_analysis_operator_strategy_violation_condition(xiidm_upload):
    """Violation-gated condition with subject/type filters: verifies that
    ``violation_subject_ids`` and ``violation_types`` reach pypowsybl without
    being rejected."""
    pytest.importorskip("pypowsybl.security")
    network = load_network(xiidm_upload)
    contingencies = build_n1_contingencies(network, "Lines")[:1]
    cid = contingencies[0]["id"]
    line_id = contingencies[0]["element_id"]
    gens = network.get_generators(attributes=[])
    gen_id = list(gens.index)[0]
    actions = [{
        "action_id": "gen_down",
        "type": "GENERATOR_ACTIVE_POWER",
        "generator_id": gen_id,
        "is_relative": True,
        "active_power": -10.0,
    }]
    strategies = [{
        "operator_strategy_id": "strat_v",
        "contingency_id": cid,
        "action_ids": ["gen_down"],
        "condition_type": "ANY_VIOLATION_CONDITION",
        "violation_subject_ids": [line_id],
        "violation_types": ["CURRENT"],
    }]
    results = run_security_analysis(
        network,
        contingencies,
        actions=actions,
        operator_strategies=strategies,
    )
    assert "operator_strategies" in results
