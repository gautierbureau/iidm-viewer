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
    build_injection_map_html,
    injection_color_scale,
    injection_map_caption,
    metric_unit,
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


def test_suggest_full_scale_subscalar_values_promote_to_max():
    """Every value below 1 MW falls through the ``p90 < 1`` branch
    and uses the max of the absolute values."""
    records = [
        {"inj_p_mw": 0.5, "inj_q_mvar": 0.0},
        {"inj_p_mw": 0.6, "inj_q_mvar": 0.0},
    ]
    out = _suggest_full_scale(records, "P")
    # max(|values|) = 0.6 → rounded up to 1 (1 × 10^0).
    assert out == pytest.approx(1.0)


def test_suggest_full_scale_only_zero_injections_returns_default():
    """A list of strict-zero injections has ``p90 == 0`` → falls back
    to the 500 MW default."""
    records = [{"inj_p_mw": 0.0, "inj_q_mvar": 0.0} for _ in range(3)]
    out = _suggest_full_scale(records, "P")
    assert out == 500.0


def test_suggest_full_scale_picks_max_at_10x_magnitude():
    """A value strictly above 5 × 10^n falls through the for-loop and
    lands on the explicit ``10 * magnitude`` return — make sure the
    branch is exercised."""
    # 800 → magnitude=100, candidate=1×100=100 < 800, 2×100=200 < 800,
    # 5×100=500 < 800, 10×100=1000 >= 800. Return path is the inner
    # ``return float(candidate)``, exercising the for-loop's last
    # iteration. Test sanity-checks the rounding stays "nice".
    records = [{"inj_p_mw": 800.0, "inj_q_mvar": 0.0}]
    out = _suggest_full_scale(records, "P")
    assert out == 1000.0


# ── _extract_injection_data — no extension ───────────────────────────────────

def test_extract_returns_none_without_substation_position(blank_network):
    assert _extract_injection_data(blank_network) is None


def test_extract_returns_none_on_four_substations(node_breaker_network):
    # The four_substations factory doesn't create substationPosition entries.
    assert _extract_injection_data(node_breaker_network) is None


def test_extract_recovers_when_get_generators_raises(xiidm_upload):
    """If ``get_generators`` blows up on the worker, the extractor falls
    back to an empty frame instead of propagating the error — the
    per-substation aggregation just sees zero gens."""
    from iidm_viewer.powsybl_worker import NetworkProxy, run

    raw = object.__getattribute__(load_network(xiidm_upload), "_obj")

    def _broken_get_generators(*_a, **_k):
        raise RuntimeError("simulated pypowsybl failure")

    # Wrap the network so only ``get_generators`` raises.
    class _Wrap:
        def __init__(self, inner):
            self._inner = inner
        def __getattr__(self, name):
            return getattr(self._inner, name)

    wrap = _Wrap(raw)
    wrap.get_generators = _broken_get_generators
    data = _extract_injection_data(NetworkProxy(wrap))
    assert data is not None
    # IEEE14 has substationPosition; records still populate (gen_count=0).
    assert any(r["gen_count"] == 0 for r in data["records"])


def test_extract_recovers_when_get_loads_raises(xiidm_upload):
    """Symmetric to the generator case: ``get_loads`` raising drops
    every load count to 0 but keeps the records list non-empty."""
    raw = object.__getattribute__(load_network(xiidm_upload), "_obj")

    class _Wrap:
        def __init__(self, inner):
            self._inner = inner
        def __getattr__(self, name):
            return getattr(self._inner, name)

    wrap = _Wrap(raw)
    wrap.get_loads = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x"))
    from iidm_viewer.powsybl_worker import NetworkProxy
    data = _extract_injection_data(NetworkProxy(wrap))
    assert data is not None
    assert all(r["load_count"] == 0 for r in data["records"])


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


# ── metric_unit + injection_color_scale ──────────────────────────────────────

def test_metric_unit_active_and_reactive():
    assert metric_unit("P") == "MW"
    assert metric_unit("Q") == "MVAr"


def test_injection_color_scale_centered_at_zero():
    scale = injection_color_scale(500.0)
    assert scale.center == 0.0
    assert scale.range == 500.0
    # Green = positive (exporter), red = negative (importer).
    assert scale.high_rgb == (24, 150, 58)
    assert scale.low_rgb == (199, 27, 27)


# ── build_injection_map_html (the host-agnostic Leaflet entry point) ─────────

def _ieee14_records(xiidm_upload):
    network = load_network(xiidm_upload)
    data = _extract_injection_data(network)
    assert data is not None
    return data["records"]


def test_build_injection_map_html_returns_full_document(xiidm_upload):
    html, transport = build_injection_map_html(
        _ieee14_records(xiidm_upload),
        metric="P", mode="icons", full_scale=500.0,
    )
    assert "<!DOCTYPE html>" in html
    assert "leaflet" in html.lower()
    assert transport  # non-empty


def test_build_injection_map_html_empty_when_no_records():
    html, transport = build_injection_map_html(
        [], metric="P", mode="icons", full_scale=500.0,
    )
    assert html == ""
    assert transport == []


def test_build_injection_map_html_filters_below_transport_threshold():
    """A substation with no VL ≥ 63 kV must drop out of the map."""
    records = [{
        "substation_id": "S_LV",
        "substation_name": "S_LV",
        "max_nominal_v": 20.0,
        "nominal_v_set": [20.0],
        "gen_p_mw": 5.0, "load_p_mw": -2.0, "inj_p_mw": 3.0,
        "gen_q_mvar": 0.0, "load_q_mvar": 0.0, "inj_q_mvar": 0.0,
        "gen_count": 1, "load_count": 1,
        "lat": 45.0, "lon": 2.0,
    }]
    html, transport = build_injection_map_html(
        records, metric="P", mode="icons", full_scale=500.0,
    )
    assert html == ""
    assert transport == []


def test_build_injection_map_html_supports_q_metric(xiidm_upload):
    html, transport = build_injection_map_html(
        _ieee14_records(xiidm_upload),
        metric="Q", mode="gradient", full_scale=200.0,
    )
    assert "MVAr" in html
    assert transport


# ── injection_map_caption ─────────────────────────────────────────────────────

def test_injection_map_caption_active_metric():
    records = [
        {"substation_id": "S1", "inj_p_mw": 120.0, "inj_q_mvar": 0.0,
         "max_nominal_v": 400.0},
        {"substation_id": "S2", "inj_p_mw": -80.0, "inj_q_mvar": 0.0,
         "max_nominal_v": 400.0},
        {"substation_id": "S3", "inj_p_mw": -20.0, "inj_q_mvar": 0.0,
         "max_nominal_v": 400.0},
    ]
    caption = injection_map_caption(records, "P")
    assert "3 substations" in caption
    assert "1 exporters" in caption
    assert "2 importers" in caption
    assert "MW" in caption
    # net = +120 - 100 = +20 MW
    assert "+20" in caption


def test_injection_map_caption_reactive_metric_unit_changes():
    records = [
        {"substation_id": "S1", "inj_p_mw": 0.0, "inj_q_mvar": 50.0,
         "max_nominal_v": 400.0},
    ]
    caption = injection_map_caption(records, "Q")
    assert "MVAr" in caption


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
