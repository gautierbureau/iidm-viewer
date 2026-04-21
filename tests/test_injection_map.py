"""Tests for iidm_viewer.injection_map."""
import math

import pandas as pd
import pytest

from iidm_viewer.injection_map import (
    TRANSPORT_NOMINAL_V_THRESHOLD,
    _extract_injection_data,
    _filter_transport,
    _grid_inj_series,
    _radius_for,
    _suggest_full_scale,
)
from iidm_viewer.state import load_network


# ── _grid_inj_series ─────────────────────────────────────────────────────────

def test_grid_inj_series_generators_realized():
    # Generator terminal p is in LOAD convention (negative for generation).
    # Grid injection must be -p: positive for a running generator.
    df = pd.DataFrame({
        "p": [-100.0, -50.0],
        "target_p": [120.0, 60.0],
        "connected": [True, True],
    })
    out = _grid_inj_series(df, "p", "target_p", flip_scheduled=False)
    assert list(out) == [100.0, 50.0]


def test_grid_inj_series_generators_fallback_to_target_p():
    df = pd.DataFrame({
        "p": [float("nan"), float("nan")],
        "target_p": [120.0, 60.0],
        "connected": [True, True],
    })
    out = _grid_inj_series(df, "p", "target_p", flip_scheduled=False)
    # target_p is already in generation convention (injection-signed)
    assert list(out) == [120.0, 60.0]


def test_grid_inj_series_loads_realized():
    # Load terminal p is in LOAD convention (positive for consumption).
    # Grid injection must be -p: negative for a load.
    df = pd.DataFrame({
        "p": [80.0, 30.0],
        "p0": [100.0, 40.0],
        "connected": [True, True],
    })
    out = _grid_inj_series(df, "p", "p0", flip_scheduled=True)
    assert list(out) == [-80.0, -30.0]


def test_grid_inj_series_loads_fallback_to_p0():
    df = pd.DataFrame({
        "p": [float("nan"), float("nan")],
        "p0": [100.0, 40.0],
        "connected": [True, True],
    })
    out = _grid_inj_series(df, "p", "p0", flip_scheduled=True)
    # p0 is load-convention — must be flipped to get grid injection
    assert list(out) == [-100.0, -40.0]


def test_grid_inj_series_disconnected_is_zero():
    df = pd.DataFrame({
        "p": [-100.0, -50.0],
        "target_p": [120.0, 60.0],
        "connected": [True, False],
    })
    out = _grid_inj_series(df, "p", "target_p", flip_scheduled=False)
    assert list(out) == [100.0, 0.0]


def test_grid_inj_series_empty_df():
    out = _grid_inj_series(pd.DataFrame(), "p", "target_p", flip_scheduled=False)
    assert out.empty


def test_grid_inj_series_missing_columns():
    df = pd.DataFrame({"connected": [True, True]})
    out = _grid_inj_series(df, "p", "target_p", flip_scheduled=False)
    # All NaN → fillna(0) → zeroes
    assert list(out) == [0.0, 0.0]


# ── _filter_transport ────────────────────────────────────────────────────────

def test_filter_transport_drops_sub_below_threshold():
    records = [
        {"substation_id": "S1", "max_nominal_v": 400.0},
        {"substation_id": "S2", "max_nominal_v": 20.0},
        {"substation_id": "S3", "max_nominal_v": 63.0},
    ]
    out = _filter_transport(records)
    ids = {r["substation_id"] for r in out}
    assert ids == {"S1", "S3"}


# ── _radius_for ──────────────────────────────────────────────────────────────

def test_radius_for_zero():
    assert _radius_for(0.0, 500.0) == pytest.approx(4.0)


def test_radius_for_full_scale_equals_max():
    assert _radius_for(500.0, 500.0) == pytest.approx(18.0)


def test_radius_for_half_scale_uses_sqrt():
    # sqrt(0.5) ≈ 0.7071
    r = _radius_for(250.0, 500.0, min_r=4.0, max_r=18.0)
    expected = 4.0 + (18.0 - 4.0) * math.sqrt(0.5)
    assert r == pytest.approx(expected)


def test_radius_for_negative_same_as_positive():
    assert _radius_for(-300.0, 500.0) == _radius_for(300.0, 500.0)


def test_radius_for_out_of_scale_clamped():
    assert _radius_for(10000.0, 500.0) == pytest.approx(18.0)


def test_radius_for_none_or_nan():
    assert _radius_for(None, 500.0) == 4.0
    assert _radius_for(float("nan"), 500.0) == 4.0


# ── _suggest_full_scale ──────────────────────────────────────────────────────

def test_suggest_full_scale_empty_returns_default():
    assert _suggest_full_scale([], "P") == 500.0


def test_suggest_full_scale_rounds_to_nice_step():
    records = [{"inj_p_mw": v, "inj_q_mvar": 0.0} for v in [3, 5, 7, 8, 12]]
    out = _suggest_full_scale(records, "P")
    assert out in (1, 2, 5, 10, 20, 50, 100)


def test_suggest_full_scale_scales_with_magnitude():
    records = [{"inj_p_mw": v, "inj_q_mvar": 0.0} for v in [300, 500, 800, 1200]]
    out = _suggest_full_scale(records, "P")
    assert out >= 1000


# ── _extract_injection_data — no extension ───────────────────────────────────

def test_extract_returns_none_without_substation_position(blank_network):
    assert _extract_injection_data(blank_network) is None


def test_extract_returns_none_on_four_substations(node_breaker_network):
    # The four_substations factory doesn't create substationPosition entries.
    assert _extract_injection_data(node_breaker_network) is None


# ── _extract_injection_data — IEEE14 ─────────────────────────────────────────

def test_extract_ieee14_returns_records(xiidm_upload):
    network = load_network(xiidm_upload)
    data = _extract_injection_data(network)
    assert data is not None
    assert set(data.keys()) == {"records", "has_lf_p", "has_lf_q"}
    assert len(data["records"]) > 0


def test_extract_ieee14_record_shape(xiidm_upload):
    network = load_network(xiidm_upload)
    data = _extract_injection_data(network)
    for r in data["records"]:
        assert -90 <= r["lat"] <= 90
        assert -180 <= r["lon"] <= 180
        assert r["substation_id"]
        assert "inj_p_mw" in r and "inj_q_mvar" in r
        assert r["inj_p_mw"] == pytest.approx(r["gen_p_mw"] + r["load_p_mw"])
        assert r["inj_q_mvar"] == pytest.approx(r["gen_q_mvar"] + r["load_q_mvar"])
        assert r["max_nominal_v"] > 0


def test_extract_ieee14_has_flags_are_bool(xiidm_upload):
    network = load_network(xiidm_upload)
    data = _extract_injection_data(network)
    assert isinstance(data["has_lf_p"], bool)
    assert isinstance(data["has_lf_q"], bool)


# ── TRANSPORT_NOMINAL_V_THRESHOLD sanity ─────────────────────────────────────

def test_threshold_is_63kv():
    assert TRANSPORT_NOMINAL_V_THRESHOLD == 63.0


# ── AppTest smoke: app still renders with the Injection Map tab wired ────────

def test_app_renders_with_injection_tab(xiidm_upload):
    from streamlit.testing.v1 import AppTest
    from iidm_viewer.state import load_network as _load

    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = _load(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.run(timeout=30)
    assert not at.exception
