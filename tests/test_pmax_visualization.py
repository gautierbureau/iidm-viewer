"""Tests for iidm_viewer.pmax_visualization."""
import math

import pandas as pd

from iidm_viewer.state import load_network, run_loadflow
from iidm_viewer.pmax_visualization import _compute_pmax_data, _build_pangle_chart


def _load_and_run(xiidm_upload):
    network = load_network(xiidm_upload)
    run_loadflow(network)
    return network


def test_pmax_data_without_loadflow_has_zero_p_actual(xiidm_upload):
    """Without a load flow p1 is absent → p_actual_mw = 0 for all lines.

    The IEEE14 XIIDM file stores bus voltages (v_mag > 0) so Pmax can be
    computed, but line power flows are not stored, so p_actual stays 0.
    """
    network = load_network(xiidm_upload)
    df = _compute_pmax_data(network)
    # Voltages are stored → rows are produced; power flows are not → P = 0
    assert not df.empty
    assert (df["p_actual_mw"] == 0.0).all()


def test_pmax_data_after_loadflow(xiidm_upload):
    network = _load_and_run(xiidm_upload)
    df = _compute_pmax_data(network)
    assert not df.empty


def test_pmax_data_columns(xiidm_upload):
    network = _load_and_run(xiidm_upload)
    df = _compute_pmax_data(network)
    expected_cols = {
        "name", "pmax_mw", "p_actual_mw", "p_pmax_ratio",
        "delta_deg", "margin_pct", "voltage_level1_id", "voltage_level2_id",
    }
    assert expected_cols.issubset(set(df.columns))


def test_pmax_positive(xiidm_upload):
    network = _load_and_run(xiidm_upload)
    df = _compute_pmax_data(network)
    assert (df["pmax_mw"] > 0).all()


def test_ratio_between_0_and_1(xiidm_upload):
    network = _load_and_run(xiidm_upload)
    df = _compute_pmax_data(network)
    valid = df["p_pmax_ratio"].dropna()
    assert (valid >= 0).all()
    assert (valid <= 1).all()


def test_delta_deg_within_90(xiidm_upload):
    network = _load_and_run(xiidm_upload)
    df = _compute_pmax_data(network)
    valid = df["delta_deg"].dropna()
    assert (valid >= 0).all()
    assert (valid <= 90).all()


def test_margin_equals_100_minus_ratio_pct(xiidm_upload):
    network = _load_and_run(xiidm_upload)
    df = _compute_pmax_data(network)
    valid = df.dropna(subset=["p_pmax_ratio", "margin_pct"])
    expected = (1.0 - valid["p_pmax_ratio"]) * 100.0
    pd.testing.assert_series_equal(
        valid["margin_pct"].reset_index(drop=True),
        expected.reset_index(drop=True),
        check_names=False,
        rtol=1e-6,
    )


def test_sorted_by_margin_ascending(xiidm_upload):
    network = _load_and_run(xiidm_upload)
    df = _compute_pmax_data(network)
    margins = df["margin_pct"].dropna().tolist()
    assert margins == sorted(margins)


def test_sin_delta_equals_ratio(xiidm_upload):
    """sin(δ) must equal P/Pmax for every row with valid data."""
    network = _load_and_run(xiidm_upload)
    df = _compute_pmax_data(network)
    valid = df.dropna(subset=["delta_deg", "p_pmax_ratio"])
    for _, row in valid.iterrows():
        assert math.isclose(
            math.sin(math.radians(row["delta_deg"])),
            row["p_pmax_ratio"],
            abs_tol=1e-6,
        )


def test_ieee14_has_expected_line_count(xiidm_upload):
    """IEEE14 has 20 lines; all with non-zero X should appear."""
    network = _load_and_run(xiidm_upload)
    df = _compute_pmax_data(network)
    assert len(df) > 0
    assert len(df) <= 20


def test_build_pangle_chart_returns_figure(xiidm_upload):
    network = _load_and_run(xiidm_upload)
    df = _compute_pmax_data(network)
    assert not df.empty
    line_id = df.index[0]
    fig = _build_pangle_chart(line_id, df.loc[line_id])
    # Plotly figure has data traces
    assert len(fig.data) >= 1
