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


# ---------------------------------------------------------------------------
# PmaxViewModel
# ---------------------------------------------------------------------------


def test_pmax_view_model_defaults_are_empty():
    from iidm_viewer.pmax_visualization import PmaxViewModel

    vm = PmaxViewModel()
    assert vm.unfiltered_df.empty
    assert vm.only_vl is False
    assert vm.selected_vl is None
    assert vm.is_empty() is True
    assert vm.has_vl_subset() is False
    assert vm.rows_df().empty
    assert vm.line_ids() == []
    assert vm.display_df().empty


def test_pmax_view_model_clear_resets_data_and_filter():
    import pandas as pd
    from iidm_viewer.pmax_visualization import PmaxViewModel

    vm = PmaxViewModel()
    vm.set_data(pd.DataFrame({
        "voltage_level1_id": ["VL1"],
        "voltage_level2_id": ["VL2"],
        "x_ohm": [1.0],
    }, index=pd.Index(["L1"], name="line_id")))
    vm.set_only_vl(True)
    vm.set_selected_vl("VL1")

    vm.clear()
    assert vm.unfiltered_df.empty
    assert vm.only_vl is False
    # selected_vl survives clear -- the caller controls it.
    assert vm.selected_vl == "VL1"


def test_pmax_view_model_set_data_handles_none():
    from iidm_viewer.pmax_visualization import PmaxViewModel

    vm = PmaxViewModel()
    vm.set_data(None)
    assert vm.unfiltered_df.empty
    assert vm.is_empty() is True


def test_pmax_view_model_has_vl_subset_returns_false_without_vl():
    import pandas as pd
    from iidm_viewer.pmax_visualization import PmaxViewModel

    vm = PmaxViewModel()
    vm.set_data(pd.DataFrame({
        "voltage_level1_id": ["VL1"],
        "voltage_level2_id": ["VL2"],
    }, index=pd.Index(["L1"], name="line_id")))
    # No selected_vl -- no subset.
    assert vm.has_vl_subset() is False


def test_pmax_view_model_has_vl_subset_returns_false_when_empty():
    from iidm_viewer.pmax_visualization import PmaxViewModel

    vm = PmaxViewModel()
    vm.set_selected_vl("VL1")
    assert vm.has_vl_subset() is False


def test_pmax_view_model_has_vl_subset_returns_true_when_vl_present():
    import pandas as pd
    from iidm_viewer.pmax_visualization import PmaxViewModel

    df = pd.DataFrame({
        "voltage_level1_id": ["VL1", "VL3"],
        "voltage_level2_id": ["VL2", "VL4"],
    }, index=pd.Index(["L1", "L2"], name="line_id"))
    vm = PmaxViewModel()
    vm.set_data(df)
    vm.set_selected_vl("VL1")
    assert vm.has_vl_subset() is True
    vm.set_selected_vl("VL_NOT_PRESENT")
    assert vm.has_vl_subset() is False


def test_pmax_view_model_rows_df_returns_unfiltered_when_only_vl_false():
    import pandas as pd
    from iidm_viewer.pmax_visualization import PmaxViewModel

    df = pd.DataFrame({
        "voltage_level1_id": ["VL1", "VL3"],
        "voltage_level2_id": ["VL2", "VL4"],
    }, index=pd.Index(["L1", "L2"], name="line_id"))
    vm = PmaxViewModel()
    vm.set_data(df)
    vm.set_selected_vl("VL1")
    # only_vl=False -> rows_df returns the whole frame.
    pd.testing.assert_frame_equal(vm.rows_df(), df)


def test_pmax_view_model_rows_df_returns_vl_subset_when_toggle_on():
    import pandas as pd
    from iidm_viewer.pmax_visualization import (
        PmaxViewModel,
        filter_by_vl,
    )

    df = pd.DataFrame({
        "voltage_level1_id": ["VL1", "VL3"],
        "voltage_level2_id": ["VL2", "VL4"],
    }, index=pd.Index(["L1", "L2"], name="line_id"))
    vm = PmaxViewModel()
    vm.set_data(df)
    vm.set_selected_vl("VL1")
    vm.set_only_vl(True)

    pd.testing.assert_frame_equal(vm.rows_df(), filter_by_vl(df, "VL1"))


def test_pmax_view_model_line_ids_reflect_rows_df():
    import pandas as pd
    from iidm_viewer.pmax_visualization import PmaxViewModel

    df = pd.DataFrame({
        "voltage_level1_id": ["VL1", "VL3"],
        "voltage_level2_id": ["VL2", "VL4"],
    }, index=pd.Index(["L1", "L2"], name="line_id"))
    vm = PmaxViewModel()
    vm.set_data(df)
    assert vm.line_ids() == ["L1", "L2"]

    vm.set_selected_vl("VL1")
    vm.set_only_vl(True)
    assert vm.line_ids() == ["L1"]


def test_pmax_view_model_rows_df_falls_back_when_subset_empty():
    """When ``only_vl`` is on but the VL slice is empty (e.g. the
    user selected an unrelated VL), ``rows_df`` returns the
    unfiltered frame so the table still shows something."""
    import pandas as pd
    from iidm_viewer.pmax_visualization import PmaxViewModel

    df = pd.DataFrame({
        "voltage_level1_id": ["VL1"],
        "voltage_level2_id": ["VL2"],
    }, index=pd.Index(["L1"], name="line_id"))
    vm = PmaxViewModel()
    vm.set_data(df)
    vm.set_selected_vl("VL_NOT_IN_NETWORK")
    vm.set_only_vl(True)
    pd.testing.assert_frame_equal(vm.rows_df(), df)
