"""Tests for iidm_viewer.operational_limits."""
import pandas as pd
import pypowsybl.loadflow as lf
import pytest
from unittest.mock import MagicMock, patch

from iidm_viewer.state import load_network, run_loadflow
from iidm_viewer.operational_limits import (
    _compute_loading,
    _duration_label,
    _get_branch_losses,
    _get_current_flows,
    _get_filtered_element_ids,
    render_operational_limits,
)


def _load_and_run_lf(xiidm_upload):
    network = load_network(xiidm_upload)
    run_loadflow(network)
    return network


def test_operational_limits_not_empty(xiidm_upload):
    network = load_network(xiidm_upload)
    limits = network.get_operational_limits()
    assert not limits.empty
    assert len(limits) == 58  # 58 limit entries in IEEE14


def test_limits_have_expected_columns(xiidm_upload):
    network = load_network(xiidm_upload)
    limits = network.get_operational_limits()
    assert "element_type" in limits.columns
    assert "value" in limits.columns


def test_limits_cover_lines_and_transformers(xiidm_upload):
    network = load_network(xiidm_upload)
    limits = network.get_operational_limits().reset_index()
    element_types = limits["element_type"].unique()
    assert "LINE" in element_types
    assert "TWO_WINDINGS_TRANSFORMER" in element_types


def test_compute_loading_after_loadflow(xiidm_upload):
    network = _load_and_run_lf(xiidm_upload)
    limits = network.get_operational_limits().reset_index()
    loading = _compute_loading(network, limits)

    assert not loading.empty
    assert "loading_pct" in loading.columns
    assert "element_id" in loading.columns
    # All loading values should be positive
    assert (loading["loading_pct"] > 0).all()


def test_compute_loading_returns_worst_side(xiidm_upload):
    network = _load_and_run_lf(xiidm_upload)
    limits = network.get_operational_limits().reset_index()
    loading = _compute_loading(network, limits)

    # Each element should appear only once (worst side)
    assert loading["element_id"].is_unique


def test_get_current_flows_returns_both_sides(xiidm_upload):
    network = _load_and_run_lf(xiidm_upload)
    flows = _get_current_flows(network)

    assert len(flows) > 0
    for eid, flow in flows.items():
        assert "i1" in flow
        assert "i2" in flow


def test_branch_losses_populated_after_loadflow(xiidm_upload):
    """After LF, losses (p1+p2) must be finite and non-negative on IEEE14."""
    network = _load_and_run_lf(xiidm_upload)
    losses = _get_branch_losses(network)

    assert losses  # non-empty
    finite = [v for v in losses.values() if pd.notna(v)]
    assert finite, "expected at least one branch with finite losses after LF"
    # Physical sanity: losses on a passive branch can't be meaningfully negative.
    # Allow tiny numerical noise.
    assert all(v > -1e-3 for v in finite)


def test_compute_loading_includes_losses_column(xiidm_upload):
    network = _load_and_run_lf(xiidm_upload)
    limits = network.get_operational_limits().reset_index()
    loading = _compute_loading(network, limits)

    assert "losses" in loading.columns
    # At least some branches should have finite losses post-LF.
    assert loading["losses"].notna().any()


def test_loading_l1_2_1_is_near_80_percent(xiidm_upload):
    """L1-2-1 has perm=800A, post-LF current ~638A → ~80% loading."""
    network = _load_and_run_lf(xiidm_upload)
    limits = network.get_operational_limits().reset_index()
    loading = _compute_loading(network, limits)

    l1_2 = loading[loading["element_id"] == "L1-2-1"]
    assert len(l1_2) == 1
    assert 70 < l1_2.iloc[0]["loading_pct"] < 90


# ---------------------------------------------------------------------------
# Unit tests for exception / edge-case paths (mocked network)
# ---------------------------------------------------------------------------


def _mock_net(**methods):
    """Build a MagicMock network; each kwarg name → side_effect or return_value.

    Pass ``raises=True`` as the value to make the method raise RuntimeError.
    """
    net = MagicMock()
    for name, val in methods.items():
        mock_method = getattr(net, name)
        if val is True:  # sentinel → raise
            mock_method.side_effect = RuntimeError("unavailable")
        else:
            mock_method.return_value = val
    return net


def _perm_limits(*element_ids):
    """Minimal limits_reset DataFrame for _compute_loading."""
    rows = []
    for eid in element_ids:
        rows.append({"element_id": eid, "side": "ONE", "value": 400.0,
                     "acceptable_duration": -1, "element_type": "LINE"})
    return pd.DataFrame(rows)


# --- _duration_label ---


def test_duration_label_seconds():
    """Line 63: d < 60 branch returns seconds string."""
    assert _duration_label(30) == "30s"


# --- _get_branch_losses ---


def test_get_branch_losses_exception_skips_method():
    """When get_lines raises, the loop continues; both failing → empty result."""
    net = _mock_net(get_lines=True, get_2_windings_transformers=True)
    assert _get_branch_losses(net) == {}


def test_get_branch_losses_nan_p1_gives_nan_entry():
    """When p1 is NaN the element entry is NaN, not omitted."""
    df = pd.DataFrame({"p1": [float("nan")], "p2": [5.0]}, index=["L1"])
    net = _mock_net(get_lines=df,
                    get_2_windings_transformers=pd.DataFrame({"p1": [], "p2": []}))
    result = _get_branch_losses(net)
    assert "L1" in result
    assert pd.isna(result["L1"])


# --- _get_current_flows ---


def test_get_current_flows_exception_skips_method():
    """When both methods raise the result is an empty dict."""
    net = _mock_net(get_lines=True, get_2_windings_transformers=True)
    assert _get_current_flows(net) == {}


# --- _compute_loading ---


def test_compute_loading_both_methods_raise_returns_empty():
    """Lines 145-146 (except/pass) and 149 (empty return) are exercised."""
    net = _mock_net(get_lines=True, get_2_windings_transformers=True)
    result = _compute_loading(net, _perm_limits("L1"))
    assert result.empty


def test_compute_loading_valid_mocked_data():
    """When get_lines returns current data, loading_pct is correctly computed."""
    lines_df = pd.DataFrame(
        {"i1": [200.0], "i2": [150.0], "name": ["Line 1"]},
        index=pd.Index(["L1"], name="id"),
    )
    net = MagicMock()
    net.get_lines.return_value = lines_df
    net.get_2_windings_transformers.side_effect = RuntimeError("unavailable")

    with patch("iidm_viewer.operational_limits._get_branch_losses", return_value={}):
        result = _compute_loading(net, _perm_limits("L1"))

    assert not result.empty
    assert "loading_pct" in result.columns
    assert result["loading_pct"].iloc[0] == pytest.approx(50.0)


# --- _get_filtered_element_ids ---


def test_get_filtered_element_ids_exception_skips_method():
    """Both component getters raise → get_enriched_component returns empty → empty set."""
    net = _mock_net(get_lines=True, get_2_windings_transformers=True)
    with patch("iidm_viewer.operational_limits.get_enriched_component", return_value=pd.DataFrame()):
        result = _get_filtered_element_ids(net, None)
    assert result == set()


def test_get_filtered_element_ids_empty_df_skips():
    """Both component getters return empty df → empty set."""
    net = _mock_net(get_lines=pd.DataFrame(), get_2_windings_transformers=pd.DataFrame())
    with patch("iidm_viewer.operational_limits.get_enriched_component", return_value=pd.DataFrame()):
        result = _get_filtered_element_ids(net, None)
    assert result == set()


# --- render_operational_limits ---


def _limits_df_fixture():
    return pd.DataFrame(
        {"element_type": ["LINE"], "acceptable_duration": [-1],
         "value": [400.0], "side": ["ONE"], "name": ["lim1"]},
        index=pd.Index(["L1"], name="element_id"),
    )


def _loading_df_fixture():
    return pd.DataFrame({
        "element_id": ["L1"], "element_name": ["Line 1"],
        "element_type": ["LINE"], "side": ["ONE"],
        "current": [200.0], "permanent_limit": [400.0],
        "loading_pct": [50.0], "losses": [5.0],
    })


def test_render_operational_limits_no_limits_shows_info():
    """Lines 215-216: empty limits_df → st.info called once."""
    net = MagicMock()
    net.get_operational_limits.return_value = pd.DataFrame()
    with patch("iidm_viewer.operational_limits.st") as mock_st:
        render_operational_limits(net, None)
        mock_st.info.assert_called_once()


def test_render_operational_limits_no_filtered_elements():
    """Lines 267-268: when _get_filtered_element_ids returns empty set → st.info."""
    net = MagicMock()
    net.get_operational_limits.return_value = _limits_df_fixture()
    with patch("iidm_viewer.operational_limits.st") as mock_st, \
         patch("iidm_viewer.operational_limits._compute_loading", return_value=pd.DataFrame()), \
         patch("iidm_viewer.operational_limits._get_filtered_element_ids", return_value=set()):
        render_operational_limits(net, None)
    mock_st.info.assert_called()


def test_render_operational_limits_with_loading_and_id_filter():
    """Lines 237-259 (loading table) and 278 (id filter applied) are hit."""
    import pytest as _pytest
    net = MagicMock()
    net.get_operational_limits.return_value = _limits_df_fixture()
    with patch("iidm_viewer.operational_limits.st") as mock_st, \
         patch("iidm_viewer.operational_limits._compute_loading",
               return_value=_loading_df_fixture()), \
         patch("iidm_viewer.operational_limits._get_filtered_element_ids",
               return_value={"L1"}), \
         patch("iidm_viewer.operational_limits._get_current_flows", return_value={}), \
         patch("iidm_viewer.operational_limits._get_branch_losses", return_value={}):
        mock_st.slider.return_value = 0   # threshold=0 → all elements above
        mock_st.text_input.return_value = "L1"  # filter that still matches L1
        mock_st.selectbox.return_value = "L1"
        render_operational_limits(net, None)
    # The loading table and the raw limits table are both rendered
    assert mock_st.dataframe.call_count >= 1


def test_render_operational_limits_id_filter_no_match():
    """Lines 282-283: id filter removes all elements → st.info and early return."""
    net = MagicMock()
    net.get_operational_limits.return_value = _limits_df_fixture()
    with patch("iidm_viewer.operational_limits.st") as mock_st, \
         patch("iidm_viewer.operational_limits._compute_loading", return_value=pd.DataFrame()), \
         patch("iidm_viewer.operational_limits._get_filtered_element_ids",
               return_value={"L1"}), \
         patch("iidm_viewer.operational_limits._get_current_flows", return_value={}), \
         patch("iidm_viewer.operational_limits._get_branch_losses", return_value={}):
        mock_st.slider.return_value = 0
        mock_st.text_input.return_value = "ZZZZ"  # matches nothing
        render_operational_limits(net, None)
    mock_st.info.assert_called()
