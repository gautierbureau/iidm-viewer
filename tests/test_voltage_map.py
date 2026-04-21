"""Tests for iidm_viewer.voltage_map."""
import json

import pytest

from iidm_viewer.state import load_network
from iidm_viewer.voltage_map import (
    TRANSPORT_NOMINAL_V_THRESHOLD,
    _extract_voltage_map_data,
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


# ── TRANSPORT_NOMINAL_V_THRESHOLD sanity ──────────────────────────────────────

def test_threshold_is_63kv():
    assert TRANSPORT_NOMINAL_V_THRESHOLD == 63.0
