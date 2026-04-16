"""Tests for iidm_viewer.operational_limits."""
import pandas as pd
import pypowsybl.loadflow as lf

from iidm_viewer.state import load_network, run_loadflow
from iidm_viewer.operational_limits import _compute_loading, _get_current_flows


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


def test_loading_l1_2_1_is_near_80_percent(xiidm_upload):
    """L1-2-1 has perm=800A, post-LF current ~638A → ~80% loading."""
    network = _load_and_run_lf(xiidm_upload)
    limits = network.get_operational_limits().reset_index()
    loading = _compute_loading(network, limits)

    l1_2 = loading[loading["element_id"] == "L1-2-1"]
    assert len(l1_2) == 1
    assert 70 < l1_2.iloc[0]["loading_pct"] < 90
