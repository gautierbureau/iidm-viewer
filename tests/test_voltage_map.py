"""Tests for iidm_viewer.voltage_map."""
import json
import math

import pytest

from iidm_viewer.state import load_network
from iidm_viewer.voltage_map import (
    TRANSPORT_NOMINAL_V_THRESHOLD,
    _aggregate_per_substation_worst,
    _apply_layout,
    _build_per_substation_tooltip,
    _build_tooltip,
    _extract_voltage_map_data,
    _fan_records,
    _nan_to_none,
    _prepare_display_records,
)


# ── _nan_to_none ──────────────────────────────────────────────────────────────

def test_nan_to_none_passthrough():
    assert _nan_to_none(1.5) == 1.5
    assert _nan_to_none(0) == 0.0


def test_nan_to_none_on_nan():
    assert _nan_to_none(float("nan")) is None


def test_nan_to_none_on_none():
    assert _nan_to_none(None) is None


def test_nan_to_none_on_garbage():
    assert _nan_to_none("not-a-number") is None


# ── _prepare_display_records ──────────────────────────────────────────────────

def _sample_records():
    return [
        {"vl_id": "A", "substation_id": "S1", "nominal_v": 400.0,
         "v_mag_mean": 404.0, "v_mag_min": 400, "v_mag_max": 408,
         "bus_count": 1, "lat": 45, "lon": 2},
        {"vl_id": "B", "substation_id": "S2", "nominal_v": 225.0,
         "v_mag_mean": 220.0, "v_mag_min": 220, "v_mag_max": 220,
         "bus_count": 1, "lat": 45, "lon": 2},
        {"vl_id": "C", "substation_id": "S3", "nominal_v": 20.0,
         "v_mag_mean": 20.5, "v_mag_min": 20, "v_mag_max": 21,
         "bus_count": 1, "lat": 45, "lon": 2},
        {"vl_id": "D", "substation_id": "S4", "nominal_v": 90.0,
         "v_mag_mean": None, "v_mag_min": None, "v_mag_max": None,
         "bus_count": 0, "lat": 45, "lon": 2},
    ]


def test_prepare_filters_below_threshold():
    out = _prepare_display_records(_sample_records(), sel_nom=None, min_nominal=63.0)
    vl_ids = {r["vl_id"] for r in out}
    assert vl_ids == {"A", "B", "D"}


def test_prepare_filter_by_selected_nominal():
    out = _prepare_display_records(_sample_records(), sel_nom=225.0, min_nominal=63.0)
    assert [r["vl_id"] for r in out] == ["B"]


def test_prepare_computes_v_pu():
    out = _prepare_display_records(_sample_records(), sel_nom=400.0, min_nominal=63.0)
    assert len(out) == 1
    assert out[0]["v_pu"] == pytest.approx(404.0 / 400.0)


def test_prepare_v_pu_none_when_no_voltage():
    out = _prepare_display_records(_sample_records(), sel_nom=90.0, min_nominal=63.0)
    assert len(out) == 1
    assert out[0]["v_pu"] is None


# ── _extract_voltage_map_data — no extension ──────────────────────────────────

def test_extract_returns_none_without_substation_position(blank_network):
    assert _extract_voltage_map_data(blank_network) is None


def test_extract_returns_none_on_four_substations_network(node_breaker_network):
    # The four_substations factory does not create substationPosition entries.
    assert _extract_voltage_map_data(node_breaker_network) is None


# ── _extract_voltage_map_data — IEEE14 (has substationPosition) ───────────────

def test_extract_ieee14_returns_records(xiidm_upload):
    network = load_network(xiidm_upload)
    data = _extract_voltage_map_data(network)
    assert data is not None
    assert "records" in data and "has_lf" in data
    assert len(data["records"]) > 0


def test_extract_ieee14_records_carry_geo(xiidm_upload):
    network = load_network(xiidm_upload)
    data = _extract_voltage_map_data(network)
    for r in data["records"]:
        assert -90 <= r["lat"] <= 90
        assert -180 <= r["lon"] <= 180
        assert r["nominal_v"] > 0
        assert r["vl_id"] and r["substation_id"]


def test_extract_ieee14_is_json_serializable(xiidm_upload):
    network = load_network(xiidm_upload)
    data = _extract_voltage_map_data(network)
    # Round-trip through JSON to be safe for the Streamlit iframe payload.
    json.dumps(data["records"])


def test_extract_has_lf_is_bool(xiidm_upload):
    network = load_network(xiidm_upload)
    data = _extract_voltage_map_data(network)
    assert isinstance(data["has_lf"], bool)


# ── AppTest smoke: Voltage Analysis tab still renders without exception ───────

def test_voltage_analysis_with_map_tab(xiidm_upload):
    from streamlit.testing.v1 import AppTest
    from iidm_viewer.state import load_network as _load

    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = _load(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.run(timeout=30)
    assert not at.exception


# ── _fan_records ──────────────────────────────────────────────────────────────

def _vl(vl_id, sub, nom, v_pu=None, lat=45.0, lon=2.0):
    v_mag = None if v_pu is None else v_pu * nom
    return {
        "vl_id": vl_id, "substation_id": sub, "nominal_v": nom,
        "v_mag_mean": v_mag, "v_pu": v_pu,
        "lat": lat, "lon": lon,
    }


def test_fan_single_vl_passthrough():
    inp = [_vl("A", "S1", 400.0, 1.01)]
    out = _fan_records(inp)
    assert out == inp


def test_fan_multi_vl_distinct_coords():
    inp = [
        _vl("A1", "S1", 400.0, 1.01),
        _vl("A2", "S1", 225.0, 0.99),
        _vl("A3", "S1", 90.0,  1.00),
    ]
    out = _fan_records(inp, jitter_deg=0.05)
    assert len(out) == 3
    coords = {(round(r["lat"], 4), round(r["lon"], 4)) for r in out}
    assert len(coords) == 3
    # All should be approximately on a circle of radius 0.05 around (45, 2)
    for r in out:
        d = math.hypot(r["lat"] - 45.0, r["lon"] - 2.0)
        assert d == pytest.approx(0.05, rel=1e-6)


def test_fan_orders_by_descending_nominal():
    """Stable ordering — highest nominal goes to angle 0 (east)."""
    inp = [
        _vl("Low", "S1", 90.0,  1.00),
        _vl("Mid", "S1", 225.0, 1.00),
        _vl("Top", "S1", 400.0, 1.00),
    ]
    out = _fan_records(inp, jitter_deg=0.05)
    by_id = {r["vl_id"]: r for r in out}
    # angle=0 → lat-offset = +0.05*cos(0) = +0.05, lon-offset = +0.05*sin(0) = 0
    assert by_id["Top"]["lat"] == pytest.approx(45.05)
    assert by_id["Top"]["lon"] == pytest.approx(2.0)


def test_fan_preserves_other_fields():
    inp = [_vl("A1", "S1", 400.0, 1.01), _vl("A2", "S1", 225.0, 0.97)]
    out = _fan_records(inp)
    out_by_id = {r["vl_id"]: r for r in out}
    assert out_by_id["A1"]["v_pu"] == 1.01
    assert out_by_id["A2"]["nominal_v"] == 225.0


# ── _aggregate_per_substation_worst ───────────────────────────────────────────

def test_aggregate_worst_picks_largest_signed_deviation():
    inp = [
        _vl("A1", "S1", 400.0, 1.01),   # +1%
        _vl("A2", "S1", 225.0, 0.97),   # -3%   ← worst
        _vl("A3", "S1", 90.0,  1.005),  # +0.5%
    ]
    out = _aggregate_per_substation_worst(inp)
    assert len(out) == 1
    assert out[0]["v_pu"] == pytest.approx(0.97)
    assert out[0]["substation_id"] == "S1"
    assert out[0]["_aggregate"] is True
    assert out[0]["_worst_vl_id"] == "A2"
    assert out[0]["_worst_nominal"] == 225.0
    assert len(out[0]["_group"]) == 3


def test_aggregate_worst_one_per_substation():
    inp = [
        _vl("A", "S1", 400.0, 1.01),
        _vl("B", "S2", 400.0, 1.02),
        _vl("C", "S2", 225.0, 1.03),
    ]
    out = _aggregate_per_substation_worst(inp)
    assert len(out) == 2
    by_sub = {r["substation_id"]: r for r in out}
    assert by_sub["S1"]["v_pu"] == pytest.approx(1.01)
    assert by_sub["S2"]["v_pu"] == pytest.approx(1.03)


def test_aggregate_worst_keeps_substation_when_no_lf():
    inp = [
        _vl("A1", "S1", 400.0, None),
        _vl("A2", "S1", 225.0, None),
    ]
    out = _aggregate_per_substation_worst(inp)
    assert len(out) == 1
    assert out[0]["v_pu"] is None
    assert out[0]["_worst_vl_id"] is None


def test_aggregate_worst_partial_lf():
    """A substation with one VL solved and one missing should use the solved one."""
    inp = [
        _vl("A1", "S1", 400.0, None),
        _vl("A2", "S1", 225.0, 0.96),
    ]
    out = _aggregate_per_substation_worst(inp)
    assert out[0]["v_pu"] == pytest.approx(0.96)
    assert out[0]["_worst_vl_id"] == "A2"


# ── _apply_layout dispatch ────────────────────────────────────────────────────

def test_apply_layout_per_vl_is_identity():
    inp = [_vl("A1", "S1", 400.0, 1.01), _vl("A2", "S1", 225.0, 0.99)]
    assert _apply_layout(inp, "per_vl") == inp


def test_apply_layout_fanned_dispatches_to_fan():
    inp = [_vl("A1", "S1", 400.0, 1.01), _vl("A2", "S1", 225.0, 0.99)]
    out = _apply_layout(inp, "per_vl_fanned")
    assert len(out) == 2
    coords = {(r["lat"], r["lon"]) for r in out}
    assert len(coords) == 2  # different positions


def test_apply_layout_per_sub_worst_dispatches():
    inp = [_vl("A1", "S1", 400.0, 1.01), _vl("A2", "S1", 225.0, 0.97)]
    out = _apply_layout(inp, "per_sub_worst")
    assert len(out) == 1
    assert out[0]["_aggregate"] is True


def test_apply_layout_unknown_raises():
    with pytest.raises(ValueError):
        _apply_layout([], "nonsense")


# ── tooltips ──────────────────────────────────────────────────────────────────

def test_per_substation_tooltip_lists_all_vls():
    inp = [
        _vl("A1", "S1", 400.0, 1.01),
        _vl("A2", "S1", 225.0, 0.97),
    ]
    agg = _aggregate_per_substation_worst(inp)[0]
    html = _build_per_substation_tooltip(agg)
    assert "S1" in html
    assert "400" in html and "225" in html
    assert "Worst" in html


def test_build_tooltip_dispatches_on_aggregate_flag():
    inp = [_vl("A1", "S1", 400.0, 1.01), _vl("A2", "S1", 225.0, 0.97)]
    agg = _aggregate_per_substation_worst(inp)[0]
    assert "Substation: S1" in _build_tooltip(agg)
    # Non-aggregate gets the per-VL tooltip
    plain = _build_tooltip(_vl("A1", "S1", 400.0, 1.01))
    assert "<b>A1</b>" in plain


# ── TRANSPORT_NOMINAL_V_THRESHOLD sanity ──────────────────────────────────────

def test_threshold_is_63kv():
    assert TRANSPORT_NOMINAL_V_THRESHOLD == 63.0
