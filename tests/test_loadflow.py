"""Tests for load flow execution and component updates."""
import pandas as pd

from iidm_viewer.state import load_network, run_loadflow, update_components


def test_run_loadflow_converges(xiidm_upload):
    network = load_network(xiidm_upload)
    results = run_loadflow(network)
    assert results[0].status.name == "CONVERGED"


def test_loadflow_updates_flows(xiidm_upload):
    network = load_network(xiidm_upload)
    run_loadflow(network)
    lines = network.get_lines(attributes=["p1", "i1"])
    # After loadflow, L1-2-1 should have non-zero flow
    assert abs(lines.loc["L1-2-1", "p1"]) > 0
    assert abs(lines.loc["L1-2-1", "i1"]) > 0


def test_update_loads_changes_values(xiidm_upload):
    network = load_network(xiidm_upload)
    # Change B2-L p0 from 21.7 to 30.0
    changes = pd.DataFrame({"p0": [30.0]}, index=pd.Index(["B2-L"], name="id"))
    update_components(network, "Loads", changes)

    loads = network.get_loads(attributes=["p0"])
    assert loads.loc["B2-L", "p0"] == 30.0


def test_update_generators_changes_target_p(xiidm_upload):
    network = load_network(xiidm_upload)
    changes = pd.DataFrame(
        {"target_p": [250.0]}, index=pd.Index(["B1-G"], name="id")
    )
    update_components(network, "Generators", changes)

    gens = network.get_generators(attributes=["target_p"])
    assert gens.loc["B1-G", "target_p"] == 250.0


def test_update_with_nan_columns(xiidm_upload):
    """NaN cells should be ignored — only non-NaN values get updated."""
    network = load_network(xiidm_upload)
    changes = pd.DataFrame(
        {"p0": [30.0, float("nan")], "q0": [float("nan"), 5.0]},
        index=pd.Index(["B2-L", "B3-L"], name="id"),
    )
    update_components(network, "Loads", changes)

    loads = network.get_loads(attributes=["p0", "q0"])
    assert loads.loc["B2-L", "p0"] == 30.0
    assert loads.loc["B3-L", "q0"] == 5.0
