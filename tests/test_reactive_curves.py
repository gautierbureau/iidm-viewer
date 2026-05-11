"""Tests for iidm_viewer.reactive_curves."""
import pandas as pd

from iidm_viewer.reactive_curves import classify_targets
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


def test_classify_minmax_inside_outside_edge():
    gens = _make_gens_df([
        {"id": "G_in",   "target_p": 50.0, "target_q":   0.0,
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": True},
        {"id": "G_out_q", "target_p": 50.0, "target_q":  60.0,
         "min_p": 0.0, "max_p": 100.0,
         "min_q": -50.0, "max_q": 50.0,
         "min_q_at_target_p": -50.0, "max_q_at_target_p": 50.0,
         "voltage_regulator_on": True},
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
    # G_in sits at the center of [0,100]×[-50,50]: closest edge is 50 away.
    assert out.loc["G_in", "distance"] == -50.0
    assert out.loc["G_out_q", "status"] == "outside"
    # G_out_q is 10 MVar above the top edge.
    assert out.loc["G_out_q", "distance"] == 10.0
    assert out.loc["G_out_p", "status"] == "outside"
    # G_out_p sits 50 MW past the right edge (which spans the full Q range
    # at that P, so the closest point is the perpendicular foot at (100, 0)).
    assert out.loc["G_out_p", "distance"] == 50.0
    assert out.loc["G_edge", "status"] == "edge"
    assert abs(out.loc["G_edge", "distance"]) < 1e-9

    assert out.loc["G_in", "regulation"] == "PV"
    assert out.loc["G_out_q", "regulation"] == "PV"
    assert out.loc["G_out_p", "regulation"] == "PQ"
    assert out.loc["G_edge", "regulation"] == "PQ"

    # lf_action flags only the PV-outside subset (those the LF will switch).
    assert out.loc["G_in", "lf_action"] == ""
    assert out.loc["G_out_q", "lf_action"] == "PV→PQ"
    assert out.loc["G_out_p", "lf_action"] == ""  # outside but already PQ
    assert out.loc["G_edge", "lf_action"] == ""   # only edge, not outside
    switchers = out.index[out["lf_action"] == "PV→PQ"].tolist()
    assert switchers == ["G_out_q"]


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
    assert set(out["status"].unique()).issubset({"inside", "edge", "outside", "n/a"})
    # Sign of distance must agree with the status classification.
    inside = out["status"] == "inside"
    outside = out["status"] == "outside"
    assert (out.loc[inside, "distance"] <= 0).all()
    assert (out.loc[outside, "distance"] > 0).all()


def test_classify_distance_diagonal_corner():
    # Target sits at (110, 60) outside the rectangle [0,100]×[-50,50] in both
    # axes; the closest point is the corner (100, 50) → distance √(10²+10²).
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
