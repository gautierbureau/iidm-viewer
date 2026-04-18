"""Tests for load flow execution and component updates."""
import json

import pandas as pd
import streamlit as st

from iidm_viewer.state import (
    create_extension,
    load_network,
    run_loadflow,
    update_components,
    update_extension,
)


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


def test_update_extension_changes_active_power_control(xiidm_upload):
    """activePowerControl is one of the editable extensions; droop is updatable."""
    network = load_network(xiidm_upload)
    create_extension(
        network, "activePowerControl", "B1-G",
        {"participate": True, "droop": 4.0},
    )

    changes = pd.DataFrame(
        {"droop": [7.5]}, index=pd.Index(["B1-G"], name="id")
    )
    update_extension(network, "activePowerControl", changes)

    apc = network.get_extensions("activePowerControl")
    assert apc.loc["B1-G", "droop"] == 7.5
    # Other fields should be unchanged
    assert bool(apc.loc["B1-G", "participate"]) is True


def test_update_extension_ignores_nan_cells(xiidm_upload):
    """NaN cells should be skipped so unchanged fields stay intact."""
    network = load_network(xiidm_upload)
    create_extension(
        network, "activePowerControl", "B1-G",
        {"participate": True, "droop": 4.0},
    )
    create_extension(
        network, "activePowerControl", "B2-G",
        {"participate": True, "droop": 4.0},
    )

    changes = pd.DataFrame(
        {"droop": [10.0, float("nan")], "participate": [float("nan"), False]},
        index=pd.Index(["B1-G", "B2-G"], name="id"),
    )
    update_extension(network, "activePowerControl", changes)

    apc = network.get_extensions("activePowerControl")
    assert apc.loc["B1-G", "droop"] == 10.0
    assert bool(apc.loc["B1-G", "participate"]) is True
    assert apc.loc["B2-G", "droop"] == 4.0
    assert bool(apc.loc["B2-G", "participate"]) is False


# ---------------------------------------------------------------------------
# ReportNode JSON captured in session_state
# ---------------------------------------------------------------------------

def test_run_loadflow_stores_report_json(xiidm_upload):
    network = load_network(xiidm_upload)
    run_loadflow(network)
    assert "_lf_report_json" in st.session_state
    assert st.session_state["_lf_report_json"] is not None


def test_run_loadflow_report_json_is_valid_json(xiidm_upload):
    network = load_network(xiidm_upload)
    run_loadflow(network)
    data = json.loads(st.session_state["_lf_report_json"])
    assert "version" in data
    assert "dictionaries" in data
    assert "reportRoot" in data


def test_run_loadflow_report_contains_loadflow_node(xiidm_upload):
    network = load_network(xiidm_upload)
    run_loadflow(network)
    data = json.loads(st.session_state["_lf_report_json"])
    children = data["reportRoot"].get("children", [])
    assert any("loadFlow" in c.get("messageKey", "") for c in children)


def test_run_loadflow_report_json_replaced_on_subsequent_run(xiidm_upload):
    network = load_network(xiidm_upload)
    run_loadflow(network)
    run_loadflow(network)
    # Second run must still produce valid, non-empty JSON
    data = json.loads(st.session_state["_lf_report_json"])
    assert "reportRoot" in data
