"""Tests for iidm_viewer.reactive_curves."""
import pandas as pd

from iidm_viewer.reactive_curves import (
    _add_bus_voltage_columns,
    _vl_to_step_up_transformer_table,
    classify_targets,
)
from iidm_viewer.state import load_network


def test_reactive_curves_exist(xiidm_upload):
    network = load_network(xiidm_upload)
    curves = network.get_reactive_capability_curve_points()
    assert not curves.empty


def test_b1g_has_8_point_curve(xiidm_upload):
    network = load_network(xiidm_upload)
    curves = network.get_reactive_capability_curve_points()
    b1g = curves.loc["B1-G"]
    assert len(b1g) == 8


def test_b2g_has_5_point_curve(xiidm_upload):
    network = load_network(xiidm_upload)
    curves = network.get_reactive_capability_curve_points()
    b2g = curves.loc["B2-G"]
    assert len(b2g) == 5


def test_curve_columns(xiidm_upload):
    network = load_network(xiidm_upload)
    curves = network.get_reactive_capability_curve_points()
    assert "p" in curves.columns
    assert "min_q" in curves.columns
    assert "max_q" in curves.columns


def test_curve_max_q_ge_min_q(xiidm_upload):
    """max_q should always be >= min_q at every point."""
    network = load_network(xiidm_upload)
    curves = network.get_reactive_capability_curve_points()
    assert (curves["max_q"] >= curves["min_q"]).all()


def test_only_b1g_and_b2g_have_curves(xiidm_upload):
    """Only B1-G and B2-G have reactiveCapabilityCurve in IEEE14."""
    network = load_network(xiidm_upload)
    curves = network.get_reactive_capability_curve_points()
    gen_ids = curves.index.get_level_values("id").unique().tolist()
    assert sorted(gen_ids) == ["B1-G", "B2-G"]


def _make_gens_df(rows):
    df = pd.DataFrame(rows).set_index("id")
    return df


def _make_curves_df(rows):
    df = pd.DataFrame(rows).set_index(["id", "num"])
    return df


def test_classify_pq_inside_outside_edge():
    # All PQ — the setpoint (target_p, target_q) is what the LF honours,
    # so classification uses that point directly.
    gens = _make_gens_df([
        {"id": "G_in",   "target_p": 50.0, "target_q":   0.0,
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": False},
        {"id": "G_out_q", "target_p": 50.0, "target_q":  60.0,
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": False},
        {"id": "G_out_p", "target_p": 150.0, "target_q":  0.0,
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": False},
        {"id": "G_edge",  "target_p": 50.0, "target_q":  50.0,
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": False},
    ])
    out = classify_targets(gens, pd.DataFrame())

    assert out.loc["G_in", "status"] == "inside"
    assert out.loc["G_in", "distance"] == -50.0
    assert out.loc["G_in", "violation"] == 0.0
    assert out.loc["G_out_q", "status"] == "outside"
    assert out.loc["G_out_q", "distance"] == 10.0
    assert out.loc["G_out_q", "violation"] == 10.0
    assert out.loc["G_out_p", "status"] == "outside"
    assert out.loc["G_out_p", "distance"] == 50.0
    assert out.loc["G_out_p", "violation"] == 50.0
    assert out.loc["G_edge", "status"] == "edge"
    assert abs(out.loc["G_edge", "distance"]) < 1e-9
    assert out.loc["G_edge", "violation"] == 0.0

    for gen_id in ("G_in", "G_out_q", "G_out_p", "G_edge"):
        assert out.loc[gen_id, "regulation"] == "PQ"
    # lf_action is the LF ground-truth PV→PQ switch flag; no PV here.
    assert (out["lf_action"] == "").all()


def test_classify_pv_uses_operating_q_from_lf():
    # PV gens: classify against (target_p, -q_lf) rather than target_q.
    # target_q on these rows is intentionally garbage to ensure it is ignored.
    gens = _make_gens_df([
        {"id": "G_pv_in", "target_p": 50.0, "target_q": 999.0,
         "q": -10.0,
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": True},
        {"id": "G_pv_sat", "target_p": 50.0, "target_q": 999.0,
         "q": -50.0,  # -q = 50 = max_q → LF clamped at the limit
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": True},
        {"id": "G_pv_near", "target_p": 50.0, "target_q": 999.0,
         "q": -48.0,  # -q = 48, within 5 MVar of max_q=50
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": True},
        {"id": "G_pv_no_lf", "target_p": 50.0, "target_q": 999.0,
         "q": float("nan"),  # no LF run → can't classify a PV gen
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": True},
    ])
    out = classify_targets(gens, pd.DataFrame())

    assert out.loc["G_pv_in", "status"] == "inside"
    assert out.loc["G_pv_sat", "status"] == "saturated"
    assert out.loc["G_pv_near", "status"] == "near_saturation"
    assert out.loc["G_pv_no_lf", "status"] == "needs_lf"

    for gen_id in ("G_pv_in", "G_pv_sat", "G_pv_near", "G_pv_no_lf"):
        assert out.loc[gen_id, "regulation"] == "PV"

    # lf_action is the ground-truth list of PV gens the LF switched to PQ.
    assert out.loc["G_pv_sat", "lf_action"] == "PV→PQ"
    assert out.loc["G_pv_in", "lf_action"] == ""
    assert out.loc["G_pv_near", "lf_action"] == ""
    assert out.loc["G_pv_no_lf", "lf_action"] == ""
    assert out.index[out["lf_action"] == "PV→PQ"].tolist() == ["G_pv_sat"]


def test_classify_regulation_unknown_when_no_target_q_and_off():
    gens = _make_gens_df([
        {"id": "G", "target_p": 50.0, "target_q": float("nan"),
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": False},
    ])
    out = classify_targets(gens, pd.DataFrame())
    assert out.loc["G", "regulation"] == "?"


def test_classify_uses_curve_p_range_when_present():
    # Curve points define a polygon with p in [10, 90]; min_p/max_p are wider
    # so the curve must take precedence.
    gens = _make_gens_df([
        {"id": "G", "target_p": 95.0, "target_q": 0.0,
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0},
    ])
    curves = _make_curves_df([
        {"id": "G", "num": 0, "p": 10.0, "min_q": -50.0, "max_q": 50.0},
        {"id": "G", "num": 1, "p": 90.0, "min_q": -50.0, "max_q": 50.0},
    ])
    out = classify_targets(gens, curves)
    assert out.loc["G", "status"] == "outside"
    # Target is 5 MW past the right edge of the rectangle [10,90]×[-50,50]
    # at q=0, so the perpendicular foot lies inside the edge → distance = 5.
    assert out.loc["G", "distance"] == 5.0
    assert out.loc["G", "violation"] == 5.0
    assert out.loc["G", "p_lo"] == 10.0
    assert out.loc["G", "p_hi"] == 90.0


def test_classify_missing_target_is_na():
    gens = _make_gens_df([
        {"id": "G", "target_p": float("nan"), "target_q": 0.0,
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0},
    ])
    out = classify_targets(gens, pd.DataFrame())
    assert out.loc["G", "status"] == "n/a"


def test_classify_real_network(xiidm_upload):
    network = load_network(xiidm_upload)
    curves = network.get_reactive_capability_curve_points()
    gens = network.get_generators(all_attributes=True)
    out = classify_targets(gens, curves)
    valid_statuses = {
        "inside", "edge", "outside", "n/a",
        "saturated", "near_saturation", "needs_lf",
    }
    assert set(out["status"].unique()).issubset(valid_statuses)
    # Sign of distance must agree with the status classification.
    inside = out["status"] == "inside"
    outside = out["status"] == "outside"
    near_sat = out["status"] == "near_saturation"
    assert (out.loc[inside, "distance"] <= 0).all()
    assert (out.loc[outside, "distance"] > 0).all()
    # Near-saturation: PV, strictly inside the polygon but close to the edge.
    assert (out.loc[near_sat, "distance"] < 0).all()


def test_step_up_transformer_picks_highest_other_side():
    # Two 2WTs on VL_LV: one to VL_MV (nominal_v=63), one to VL_HV (225).
    # The step-up choice for VL_LV must be the one with the higher
    # other-side nominal voltage (XF_HV).
    twts = pd.DataFrame([
        {"id": "XF_MV", "voltage_level1_id": "VL_LV",
         "voltage_level2_id": "VL_MV", "connected1": True,
         "connected2": True, "nominal_v1": 11.0, "nominal_v2": 63.0},
        {"id": "XF_HV", "voltage_level1_id": "VL_LV",
         "voltage_level2_id": "VL_HV", "connected1": True,
         "connected2": False, "nominal_v1": 11.0, "nominal_v2": 225.0},
    ]).set_index("id")
    out = _vl_to_step_up_transformer_table(twts)
    assert out.loc["VL_LV", "step_up_transformer_id"] == "XF_HV"
    # XF_HV has connected2=False → step-up is not fully connected.
    assert bool(out.loc["VL_LV", "step_up_transformer_connected"]) is False
    # Mirror entries for the high-side VLs should also be present.
    assert out.loc["VL_HV", "step_up_transformer_id"] == "XF_HV"
    assert out.loc["VL_MV", "step_up_transformer_id"] == "XF_MV"


def test_step_up_transformer_empty_inputs_dont_crash():
    out = _vl_to_step_up_transformer_table(pd.DataFrame())
    assert out.empty
    # Missing required columns should also return empty, not crash.
    bad = pd.DataFrame([{"id": "X", "voltage_level1_id": "A"}]).set_index("id")
    out = _vl_to_step_up_transformer_table(bad)
    assert out.empty


def test_add_bus_voltage_columns_gap_sign():
    # G_ok regulates successfully (gap = 0). G_low has its bus at 235 kV
    # against target 240 → gap = +5 kV (LF wanted more Q production but
    # clamped). G_high has its bus at 245 kV against target 240 → gap = -5.
    # G_missing's bus has no entry in the LF voltage frame → NaN.
    gens = pd.DataFrame([
        {"id": "G_ok",      "bus_id": "B1", "target_v": 24.0},
        {"id": "G_low",     "bus_id": "B2", "target_v": 240.0},
        {"id": "G_high",    "bus_id": "B3", "target_v": 240.0},
        {"id": "G_missing", "bus_id": "B4", "target_v": 24.0},
    ]).set_index("id")
    buses = pd.DataFrame([
        {"bus_id": "B1", "v_mag": 24.0},
        {"bus_id": "B2", "v_mag": 235.0},
        {"bus_id": "B3", "v_mag": 245.0},
    ])
    out = _add_bus_voltage_columns(gens, buses)
    assert out.loc["G_ok", "v_bus"] == 24.0
    assert out.loc["G_ok", "v_target_gap"] == 0.0
    assert out.loc["G_low", "v_target_gap"] == 5.0
    assert out.loc["G_high", "v_target_gap"] == -5.0
    assert pd.isna(out.loc["G_missing", "v_bus"])
    assert pd.isna(out.loc["G_missing", "v_target_gap"])


def test_add_bus_voltage_columns_empty_inputs():
    gens = pd.DataFrame([
        {"id": "G", "bus_id": "B1", "target_v": 24.0},
    ]).set_index("id")
    out = _add_bus_voltage_columns(gens, pd.DataFrame())
    # No voltages available → columns added as NaN, not raised.
    assert pd.isna(out.loc["G", "v_bus"])
    assert pd.isna(out.loc["G", "v_target_gap"])
    # Missing input columns → function is a no-op (no v_bus/gap added).
    no_target_v = pd.DataFrame([
        {"id": "G", "bus_id": "B1"},
    ]).set_index("id")
    out2 = _add_bus_voltage_columns(no_target_v, pd.DataFrame())
    assert "v_bus" not in out2.columns
    assert "v_target_gap" not in out2.columns


def test_classify_distance_diagonal_corner():
    # Target sits at (110, 60) outside the rectangle [0,100]×[-50,50] in both
    # axes; the closest point is the corner (100, 50) → distance √(10²+10²).
    # The axial violation is the worst single-axis overshoot (10), so the two
    # columns disagree by design here.
    gens = _make_gens_df([
        {"id": "G", "target_p": 110.0, "target_q": 60.0,
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": False},
    ])
    out = classify_targets(gens, pd.DataFrame())
    assert out.loc["G", "status"] == "outside"
    expected = (10.0 ** 2 + 10.0 ** 2) ** 0.5
    assert abs(out.loc["G", "distance"] - expected) < 1e-9
    assert out.loc["G", "violation"] == 10.0


# ---------------------------------------------------------------------------
# Shared/Streamlit split — guards the Phase-1 refactor so future work
# doesn't accidentally re-couple the shared module to Streamlit.
# ---------------------------------------------------------------------------
def test_reactive_curves_shared_module_has_no_streamlit_dependency():
    """``iidm_viewer.reactive_curves`` is the framework-agnostic core
    consumed by Streamlit (via ``reactive_curves_tab``), PySide6 and
    NiceGUI. The shared module must not import streamlit or plotly —
    that's the contract that lets the non-Streamlit hosts boot.
    """
    import inspect
    import iidm_viewer.reactive_curves as rc

    src = inspect.getsource(rc)
    assert "import streamlit" not in src
    assert "from streamlit" not in src
    assert "import plotly" not in src
    assert "from plotly" not in src


def test_reactive_curves_shared_module_exposes_public_api():
    """The non-underscored public API consumed by hosts."""
    from iidm_viewer import reactive_curves as rc

    expected_callables = (
        "classify_targets",
        "polygon_vertices",
        "signed_distance_to_polygon",
        "vl_to_step_up_transformer_table",
        "add_bus_voltage_columns",
        "augment_gens_with_step_up_transformer",
        "augment_gens_with_bus_voltage",
        "compute_target_v_q_sensitivities",
        "compute_target_v_q_sensitivity",
    )
    for name in expected_callables:
        assert callable(getattr(rc, name, None)), f"missing public callable: {name}"
    # Tunables live here so every host renders the same colors / band.
    assert isinstance(rc.STATUS_DIAMOND_COLOR, dict)
    assert "inside" in rc.STATUS_DIAMOND_COLOR
    assert isinstance(rc.TARGET_TOLERANCE, float)
    assert isinstance(rc.NEAR_SATURATION_THRESHOLD, float)


def test_reactive_curves_tab_module_lives_in_a_separate_file():
    """The Streamlit-only UI must live in ``reactive_curves_tab`` so
    PySide6 / NiceGUI can import the shared core without dragging
    streamlit / plotly in. Pin both: the tab module exposes
    ``render_reactive_curves`` and ``app.py`` imports from there."""
    import inspect

    from iidm_viewer import app, reactive_curves_tab

    assert hasattr(reactive_curves_tab, "render_reactive_curves")
    app_src = inspect.getsource(app)
    assert "from iidm_viewer.reactive_curves_tab import render_reactive_curves" in app_src
    # The old import path must be gone — otherwise we'd re-couple the
    # shared module to Streamlit through a transitive import.
    assert "from iidm_viewer.reactive_curves import render_reactive_curves" not in app_src
