"""COMPONENT_TYPES registry and Overview rendering."""
import pytest

from iidm_viewer.network_info import (
    COMPONENT_TYPES,
    _branch_losses_totals,
    _country_totals,
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


def test_country_totals_ieee14(xiidm_upload):
    """IEEE14 totals: generation target_p sum and load p0 sum, aggregated by country."""
    net = load_network(xiidm_upload)
    df = _country_totals(net)
    assert not df.empty
    assert set(df.columns) == {"country", "generation_mw", "consumption_mw"}
    # IEEE14 totals (target_p / p0): 272.4 MW generation, 259.0 MW consumption.
    assert abs(df["generation_mw"].sum() - 272.4) < 1.0
    assert abs(df["consumption_mw"].sum() - 259.0) < 1.0


def test_component_types_keys_match_network_methods():
    """Every registry value must name a pypowsybl Network getter."""
    import pypowsybl.network as pn

    for method_name in COMPONENT_TYPES.values():
        assert method_name.startswith("get_")
        # Method resolved on the Network class, not an instance.
        assert hasattr(pn.Network, method_name), method_name
