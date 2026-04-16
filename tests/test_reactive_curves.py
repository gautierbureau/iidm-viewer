"""Tests for iidm_viewer.reactive_curves."""
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
