"""Tests for the framework-agnostic Voltage Analysis core.

Lives in :mod:`iidm_viewer.voltage_analysis_core` and is used by the
Streamlit, PySide6 and NiceGUI hosts. The Streamlit-side
:mod:`iidm_viewer.voltage_analysis` is exercised by ``tests/test_voltage_analysis.py``;
this file pins down the host-agnostic helpers.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from iidm_viewer.state import create_component_bay
from iidm_viewer.voltage_analysis_core import (
    BUS_DETAIL_COLUMNS,
    BUS_SUMMARY_COLUMNS,
    SHUNT_DISPLAY_COLUMNS,
    SVC_DISPLAY_COLUMNS,
    VoltageAnalysisData,
    build_bus_detail,
    build_bus_summary,
    build_shunt_display,
    build_svc_display,
    bus_pu_classify,
    compute_voltage_analysis,
    enrich_bus_voltages,
    enrich_shunts,
    enrich_svcs,
    has_loadflow,
    list_nominal_voltages,
    shunt_totals,
    split_shunts_by_b,
    svc_totals,
)


_SHUNT_ID = "VA_CORE_TEST_SHUNT"
_SHUNT_B_PER_SECTION = 1e-4
_SHUNT_SECTION_COUNT = 2
_SHUNT_MAX_SECTION_COUNT = 5


@pytest.fixture
def network_with_shunt(node_breaker_network):
    create_component_bay(node_breaker_network, "Shunt Compensators", {
        "id": _SHUNT_ID,
        "bus_or_busbar_section_id": "S1VL1_BBS",
        "section_count": _SHUNT_SECTION_COUNT,
        "max_section_count": _SHUNT_MAX_SECTION_COUNT,
        "g_per_section": 0.0,
        "b_per_section": _SHUNT_B_PER_SECTION,
        "target_v": 0.0,
        "target_deadband": 0.0,
        "position_order": 300,
        "direction": "BOTTOM",
    })
    return node_breaker_network


# ── compute_voltage_analysis (the one-hop fetch the prototypes use) ───────

def test_compute_voltage_analysis_returns_data_bundle(node_breaker_network):
    data = compute_voltage_analysis(node_breaker_network)
    assert isinstance(data, VoltageAnalysisData)
    assert not data.buses.empty
    # The four-substations fixture has one SVC; shunts are network-specific.
    assert not data.svcs.empty


def test_compute_voltage_analysis_buses_columns(node_breaker_network):
    data = compute_voltage_analysis(node_breaker_network)
    assert set(data.buses.columns) >= {
        "bus_id", "voltage_level_id", "nominal_v", "v_mag", "v_pu",
    }


def test_compute_voltage_analysis_svc_columns(node_breaker_network):
    data = compute_voltage_analysis(node_breaker_network)
    assert set(data.svcs.columns) >= {
        "id", "voltage_level_id", "nominal_v", "connected",
        "regulation_mode", "current_q_mvar", "q_min_mvar", "q_max_mvar",
    }


def test_compute_voltage_analysis_shunt_columns(network_with_shunt):
    data = compute_voltage_analysis(network_with_shunt)
    assert not data.shunts.empty
    assert set(data.shunts.columns) >= {
        "id", "voltage_level_id", "nominal_v", "connected",
        "section_count", "max_section_count",
        "current_q_mvar", "available_q_mvar", "total_q_mvar", "b_per_section",
    }


def test_compute_voltage_analysis_empty_network():
    """Empty network → all three frames empty (no crash)."""
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="empty")
    net = NetworkProxy(run(_make))
    data = compute_voltage_analysis(net)
    assert data.buses.empty
    assert data.shunts.empty
    assert data.svcs.empty


# ── enrich_* on hand-crafted DataFrames (no pypowsybl) ────────────────────

def _vl_lookup():
    return pd.DataFrame({
        "voltage_level_id": ["VL_400", "VL_225"],
        "nominal_v": [400.0, 225.0],
    })


def test_enrich_bus_voltages_computes_v_pu():
    buses = pd.DataFrame({
        "id": ["B1", "B2"],
        "voltage_level_id": ["VL_400", "VL_225"],
        "v_mag": [410.0, 220.0],
    }).set_index("id")
    df = enrich_bus_voltages(buses, _vl_lookup())
    assert list(df.columns) == [
        "bus_id", "voltage_level_id", "nominal_v", "v_mag", "v_pu",
    ]
    by_id = df.set_index("bus_id")
    assert by_id.loc["B1", "v_pu"] == pytest.approx(410 / 400)
    assert by_id.loc["B2", "v_pu"] == pytest.approx(220 / 225)


def test_enrich_bus_voltages_empty_returns_empty_frame_with_columns():
    df = enrich_bus_voltages(pd.DataFrame(), _vl_lookup())
    assert df.empty
    assert list(df.columns) == [
        "bus_id", "voltage_level_id", "nominal_v", "v_mag", "v_pu",
    ]


def test_enrich_shunts_load_sign_convention():
    """Q = −b × V² in pypowsybl's load-sign convention."""
    shunts = pd.DataFrame({
        "id": ["SH1"],
        "voltage_level_id": ["VL_400"],
        "connected": [True],
        "section_count": [2],
        "max_section_count": [4],
        "b": [2e-4],     # b_per_section × section_count = 1e-4 × 2
        "q": [float("nan")],
        "b_per_section": [1e-4],
    }).set_index("id")
    df = enrich_shunts(shunts, _vl_lookup())
    row = df.iloc[0]
    # Capacitive (b > 0) → Q < 0 in load sign convention.
    assert row["current_q_mvar"] == pytest.approx(-2e-4 * 400**2)
    assert row["total_q_mvar"] == pytest.approx(-1e-4 * 4 * 400**2)
    assert row["available_q_mvar"] == pytest.approx(-1e-4 * (4 - 2) * 400**2)
    assert row["b_per_section"] == pytest.approx(1e-4)


def test_enrich_shunts_disconnected_treats_all_sections_as_available():
    shunts = pd.DataFrame({
        "id": ["SH1"],
        "voltage_level_id": ["VL_400"],
        "connected": [False],
        "section_count": [2],
        "max_section_count": [4],
        "b": [2e-4],
        "q": [float("nan")],
        "b_per_section": [1e-4],
    }).set_index("id")
    df = enrich_shunts(shunts, _vl_lookup())
    row = df.iloc[0]
    assert row["current_q_mvar"] == 0.0
    # All 4 sections available (none active because disconnected).
    assert row["available_q_mvar"] == pytest.approx(-1e-4 * 4 * 400**2)


def test_enrich_shunts_uses_lf_q_when_available():
    """When the LF wrote a ``q`` value it overrides the −b·V² estimate."""
    shunts = pd.DataFrame({
        "id": ["SH1"],
        "voltage_level_id": ["VL_400"],
        "connected": [True],
        "section_count": [2],
        "max_section_count": [4],
        "b": [2e-4],
        "q": [-50.0],  # actual LF reactive injection
        "b_per_section": [1e-4],
    }).set_index("id")
    df = enrich_shunts(shunts, _vl_lookup())
    assert df.iloc[0]["current_q_mvar"] == pytest.approx(-50.0)


def test_enrich_shunts_empty_returns_empty():
    df = enrich_shunts(pd.DataFrame(), _vl_lookup())
    assert df.empty


def test_enrich_svcs_q_range_matches_b_min_max():
    svcs = pd.DataFrame({
        "id": ["SVC1"],
        "voltage_level_id": ["VL_400"],
        "connected": [True],
        "regulation_mode": ["VOLTAGE"],
        "b_min": [-1e-3],
        "b_max": [1e-3],
        "q": [float("nan")],
    }).set_index("id")
    df = enrich_svcs(svcs, _vl_lookup())
    row = df.iloc[0]
    assert row["q_min_mvar"] == pytest.approx(-1e-3 * 400**2)
    assert row["q_max_mvar"] == pytest.approx(1e-3 * 400**2)
    assert math.isnan(row["current_q_mvar"])


def test_enrich_svcs_off_mode_overrides_q_to_zero():
    svcs = pd.DataFrame({
        "id": ["SVC_OFF", "SVC_ON"],
        "voltage_level_id": ["VL_400", "VL_400"],
        "connected": [True, True],
        "regulation_mode": ["OFF", "VOLTAGE"],
        "b_min": [-1e-3, -1e-3],
        "b_max": [1e-3, 1e-3],
        "q": [42.0, -30.0],
    }).set_index("id")
    df = enrich_svcs(svcs, _vl_lookup()).set_index("id")
    assert df.loc["SVC_OFF", "current_q_mvar"] == 0.0
    assert df.loc["SVC_ON", "current_q_mvar"] == pytest.approx(-30.0)


def test_enrich_svcs_empty_returns_empty():
    df = enrich_svcs(pd.DataFrame(), _vl_lookup())
    assert df.empty


# ── Bus display helpers ───────────────────────────────────────────────────

def test_has_loadflow_detects_v_mag_presence():
    yes = pd.DataFrame({"v_mag": [400.0, float("nan")]})
    no = pd.DataFrame({"v_mag": [float("nan"), float("nan")]})
    empty = pd.DataFrame()
    assert has_loadflow(yes) is True
    assert has_loadflow(no) is False
    assert has_loadflow(empty) is False


def test_build_bus_summary_with_loadflow_includes_pu_columns():
    buses = pd.DataFrame({
        "bus_id": ["B1", "B2", "B3"],
        "voltage_level_id": ["VL_400", "VL_400", "VL_225"],
        "nominal_v": [400.0, 400.0, 225.0],
        "v_mag": [410.0, 395.0, 220.0],
        "v_pu": [1.025, 0.9875, 220 / 225],
    })
    summary = build_bus_summary(buses)
    # Sorted ascending by nominal — 225 first, then 400.
    assert list(summary["Nominal (kV)"]) == [225.0, 400.0]
    row400 = summary[summary["Nominal (kV)"] == 400.0].iloc[0]
    assert row400["Buses"] == 2
    assert row400["Min (pu)"] == pytest.approx(0.9875, abs=1e-4)
    assert row400["Max (pu)"] == pytest.approx(1.025, abs=1e-4)


def test_build_bus_summary_without_loadflow_omits_pu_columns():
    buses = pd.DataFrame({
        "bus_id": ["B1"],
        "voltage_level_id": ["VL_400"],
        "nominal_v": [400.0],
        "v_mag": [float("nan")],
        "v_pu": [float("nan")],
    })
    summary = build_bus_summary(buses)
    assert "Min (pu)" not in summary.columns
    assert summary.iloc[0]["Buses"] == 1


def test_build_bus_summary_empty_returns_empty_with_schema():
    summary = build_bus_summary(pd.DataFrame())
    assert summary.empty
    assert list(summary.columns) == BUS_SUMMARY_COLUMNS


def test_list_nominal_voltages_sorted_descending():
    buses = pd.DataFrame({"nominal_v": [225.0, 400.0, 90.0, 400.0]})
    assert list_nominal_voltages(buses) == [400.0, 225.0, 90.0]


def test_build_bus_detail_filters_and_sorts_by_pu():
    buses = pd.DataFrame({
        "bus_id": ["B1", "B2", "B3"],
        "voltage_level_id": ["VL", "VL", "VL"],
        "nominal_v": [400.0, 400.0, 225.0],
        "v_mag": [410.0, 395.0, 220.0],
        "v_pu": [1.025, 0.9875, 220 / 225],
    })
    detail = build_bus_detail(buses, 400.0)
    assert list(detail.columns) == BUS_DETAIL_COLUMNS
    # Sorted by V (pu) ascending.
    assert detail.iloc[0]["Bus"] == "B2"
    assert detail.iloc[1]["Bus"] == "B1"
    assert len(detail) == 2


def test_build_bus_detail_drops_buses_without_v_pu():
    buses = pd.DataFrame({
        "bus_id": ["B1"],
        "voltage_level_id": ["VL"],
        "nominal_v": [400.0],
        "v_mag": [float("nan")],
        "v_pu": [float("nan")],
    })
    detail = build_bus_detail(buses, 400.0)
    assert detail.empty


def test_bus_pu_classify_lo_hi_band():
    assert bus_pu_classify(0.94, 0.95, 1.05) == "warning"
    assert bus_pu_classify(0.95, 0.95, 1.05) == "safe"
    assert bus_pu_classify(1.0, 0.95, 1.05) == "safe"
    assert bus_pu_classify(1.05, 0.95, 1.05) == "safe"
    assert bus_pu_classify(1.06, 0.95, 1.05) == "warning"
    assert bus_pu_classify(None, 0.95, 1.05) == "unknown"
    assert bus_pu_classify(float("nan"), 0.95, 1.05) == "unknown"


# ── Shunt display helpers ─────────────────────────────────────────────────

def test_split_shunts_by_b_partitions_by_sign():
    shunts = pd.DataFrame({
        "id": ["CAP", "IND", "UNK"],
        "b_per_section": [1e-4, -1e-4, float("nan")],
    })
    cap, ind, unk = split_shunts_by_b(shunts)
    assert list(cap["id"]) == ["CAP"]
    assert list(ind["id"]) == ["IND"]
    assert list(unk["id"]) == ["UNK"]


def test_split_shunts_by_b_handles_empty():
    cap, ind, unk = split_shunts_by_b(pd.DataFrame())
    assert cap.empty and ind.empty and unk.empty


def test_shunt_totals_sums_connected_active_only():
    group = pd.DataFrame({
        "connected": [True, False, True],
        "current_q_mvar": [-10.0, -5.0, -3.0],
        "available_q_mvar": [-20.0, -8.0, -6.0],
        "total_q_mvar": [-30.0, -13.0, -9.0],
    })
    active, available, total = shunt_totals(group)
    assert active == pytest.approx(-13.0)  # disconnected row excluded
    assert available == pytest.approx(-34.0)
    assert total == pytest.approx(-52.0)


def test_shunt_totals_empty_group_returns_zeros():
    assert shunt_totals(pd.DataFrame()) == (0.0, 0.0, 0.0)


def test_build_shunt_display_renames_and_rounds():
    group = pd.DataFrame({
        "id": ["SH1"],
        "voltage_level_id": ["VL"],
        "nominal_v": [400.0],
        "connected": [True],
        "section_count": [2],
        "max_section_count": [4],
        "current_q_mvar": [-12.34567],
        "available_q_mvar": [-23.45678],
        "total_q_mvar": [-34.56789],
    })
    display = build_shunt_display(group)
    assert list(display.columns) == SHUNT_DISPLAY_COLUMNS
    assert display.iloc[0]["Current Q (MVAr)"] == pytest.approx(-12.346)


# ── SVC display helpers ───────────────────────────────────────────────────

def test_svc_totals_active_uses_only_connected_non_off():
    svcs = pd.DataFrame({
        "connected": [True, True, False],
        "regulation_mode": ["VOLTAGE", "OFF", "VOLTAGE"],
        "current_q_mvar": [-5.0, 0.0, -10.0],
        "q_min_mvar": [-100.0, -50.0, -200.0],
        "q_max_mvar": [100.0, 50.0, 200.0],
    })
    active, range_total = svc_totals(svcs)
    assert active == pytest.approx(-5.0)
    assert range_total == pytest.approx(200.0 + 100.0 + 400.0)


def test_svc_totals_no_lf_returns_nan_active():
    svcs = pd.DataFrame({
        "connected": [True],
        "regulation_mode": ["VOLTAGE"],
        "current_q_mvar": [float("nan")],
        "q_min_mvar": [-100.0],
        "q_max_mvar": [100.0],
    })
    active, range_total = svc_totals(svcs)
    assert math.isnan(active)
    assert range_total == pytest.approx(200.0)


def test_build_svc_display_renames_and_rounds():
    svcs = pd.DataFrame({
        "id": ["SVC1"],
        "voltage_level_id": ["VL"],
        "nominal_v": [400.0],
        "connected": [True],
        "regulation_mode": ["VOLTAGE"],
        "current_q_mvar": [-5.6789],
        "q_min_mvar": [-100.1234],
        "q_max_mvar": [100.5678],
    })
    display = build_svc_display(svcs)
    assert list(display.columns) == SVC_DISPLAY_COLUMNS
    assert display.iloc[0]["Q min (MVAr)"] == pytest.approx(-100.123)
