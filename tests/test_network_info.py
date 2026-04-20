"""COMPONENT_TYPES registry and Overview rendering."""
import pytest

from iidm_viewer.network_info import (
    COMPONENT_TYPES,
    _branch_losses_totals,
    _country_totals,
    _losses_by_country,
)
from iidm_viewer.state import load_network, run_loadflow


def test_component_types_are_callable_on_network(xiidm_upload):
    net = load_network(xiidm_upload)
    for label, method_name in COMPONENT_TYPES.items():
        method = getattr(net, method_name, None)
        assert callable(method), f"{label} -> {method_name} is not callable on Network"
        # invoking the method should not raise
        df = method()
        assert df is not None


def test_component_types_registry_is_ordered_and_unique():
    names = list(COMPONENT_TYPES)
    assert len(names) == len(set(names)), "duplicate labels in COMPONENT_TYPES"
    methods = list(COMPONENT_TYPES.values())
    assert len(methods) == len(set(methods)), "duplicate method mappings"


@pytest.mark.parametrize("label,expected_count", [
    ("Voltage Levels", 14),
    ("Substations", 11),
    ("Lines", 17),
    ("Generators", 5),
])
def test_ieee14_counts(xiidm_upload, label, expected_count):
    net = load_network(xiidm_upload)
    method = COMPONENT_TYPES[label]
    df = getattr(net, method)()
    assert len(df) == expected_count


def test_overview_renders_network_metrics(xiidm_upload):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = load_network(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.run(timeout=30)

    assert not at.exception
    metric_labels = {m.label for m in at.metric}
    assert {"Network ID", "Format"}.issubset(metric_labels)
    # Non-zero component counts from IEEE14 should appear as metrics.
    assert "Voltage Levels" in metric_labels
    assert "Lines" in metric_labels


def test_branch_losses_totals_before_lf(xiidm_upload):
    """Without a load flow, p1/p2 are NaN so totals report no data."""
    net = load_network(xiidm_upload)
    losses = _branch_losses_totals(net)
    assert losses.get("_has_data") is False


def test_branch_losses_totals_after_lf(xiidm_upload):
    net = load_network(xiidm_upload)
    run_loadflow(net)
    losses = _branch_losses_totals(net)
    assert losses.get("_has_data") is True
    # total == lines + transformers
    assert abs(losses["total"] - (losses["lines"] + losses["transformers"])) < 1e-9


def test_country_totals_target_values_ieee14(xiidm_upload):
    """Without a load flow, target columns populate and actuals stay NaN."""
    import pandas as pd

    net = load_network(xiidm_upload)
    df = _country_totals(net)
    assert not df.empty
    assert set(df.columns) == {
        "country",
        "generation_target_mw", "generation_actual_mw",
        "consumption_target_mw", "consumption_actual_mw",
    }
    # IEEE14 targets: ~272.4 MW generation, ~259.0 MW consumption.
    assert abs(df["generation_target_mw"].sum() - 272.4) < 1.0
    assert abs(df["consumption_target_mw"].sum() - 259.0) < 1.0
    # Actuals are NaN before a load flow.
    assert df["generation_actual_mw"].isna().all()
    assert df["consumption_actual_mw"].isna().all()


def test_country_totals_actual_values_after_lf(xiidm_upload):
    """After a load flow, actual values populate and stay close to targets."""
    net = load_network(xiidm_upload)
    run_loadflow(net)
    df = _country_totals(net)
    assert not df["generation_actual_mw"].isna().all()
    assert not df["consumption_actual_mw"].isna().all()
    # Actual generation >= target + losses (slack picks up losses); actual
    # consumption should match target closely on IEEE14.
    assert df["generation_actual_mw"].sum() > df["generation_target_mw"].sum() - 1.0
    assert abs(
        df["consumption_actual_mw"].sum() - df["consumption_target_mw"].sum()
    ) < 5.0


def test_losses_by_country_ieee14(xiidm_upload):
    """After LF, IEEE14 is single-country; per-country losses sum to total."""
    net = load_network(xiidm_upload)
    run_loadflow(net)
    by_country = _losses_by_country(net)
    assert not by_country.empty
    total = _branch_losses_totals(net)["total"]
    assert abs(by_country.sum() - total) < 1e-6


def test_losses_by_country_empty_before_lf(xiidm_upload):
    net = load_network(xiidm_upload)
    by_country = _losses_by_country(net)
    assert by_country.empty


def test_component_types_keys_match_network_methods():
    """Every registry value must name a pypowsybl Network getter."""
    import pypowsybl.network as pn

    for method_name in COMPONENT_TYPES.values():
        assert method_name.startswith("get_")
        # Method resolved on the Network class, not an instance.
        assert hasattr(pn.Network, method_name), method_name


# ---------- blank-network regression tests (float64 vs object dtype) ----------

def test_country_totals_blank_network_no_error(blank_network):
    """_country_totals must not raise on a network with no substations or VLs."""
    df = _country_totals(blank_network)
    assert df.empty or set(df.columns) >= {"country"}


def test_build_vl_country_map_blank_network_no_error(blank_network):
    """_build_vl_country_map must return an empty DataFrame (not raise) for a
    blank network whose get_voltage_levels/get_substations return empty frames
    with float64 ID columns."""
    from iidm_viewer.network_info import _build_vl_country_map
    result = _build_vl_country_map(blank_network)
    assert result.empty
    assert set(result.columns) >= {"voltage_level_id", "country"}


def test_overview_blank_network_no_exception():
    """Rendering the Overview tab with a blank (empty) network must not crash."""
    from streamlit.testing.v1 import AppTest
    from iidm_viewer.powsybl_worker import NetworkProxy, run

    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="blank")

    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = NetworkProxy(run(_make))
    at.run(timeout=30)
    assert not at.exception
