"""Tests for the script_recorder op log.

The recorder is pure session-state manipulation — no pypowsybl, no
worker thread. We exercise it directly against Streamlit's session
state and assert the resulting log shape.
"""
import pandas as pd
import streamlit as st

from iidm_viewer import script_recorder


def setup_function(_):
    st.session_state.clear()


def test_get_log_returns_empty_when_unset():
    assert script_recorder.get_log() == []
    assert script_recorder.get_source_filename() is None


def test_record_load_network_seeds_log_and_filename():
    script_recorder.record_load_network(
        "grid.xiidm",
        parameters={"iidm.import.xml.skip-validation": "true"},
        post_processors=["loadflowResultsCompletion"],
    )
    log = script_recorder.get_log()
    assert len(log) == 1
    assert log[0] == {
        "kind": "load_network",
        "parameters": {"iidm.import.xml.skip-validation": "true"},
        "post_processors": ["loadflowResultsCompletion"],
    }
    assert script_recorder.get_source_filename() == "grid.xiidm"


def test_record_load_network_clears_prior_log():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_run_loadflow({}, {})
    assert len(script_recorder.get_log()) == 2

    script_recorder.record_load_network("b.xiidm", None, None)
    log = script_recorder.get_log()
    assert len(log) == 1
    assert log[0]["kind"] == "load_network"
    assert script_recorder.get_source_filename() == "b.xiidm"


def test_record_create_empty_clears_source_filename():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_empty("blank")
    assert script_recorder.get_source_filename() is None
    log = script_recorder.get_log()
    assert log == [{"kind": "create_empty", "network_id": "blank"}]


def test_record_run_loadflow_appends_to_existing_log():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_run_loadflow(
        generic={"voltage_init_mode": "UNIFORM_VALUES"},
        provider={"slackBusSelectionMode": "MOST_MESHED"},
    )
    log = script_recorder.get_log()
    assert len(log) == 2
    assert log[1] == {
        "kind": "run_loadflow",
        "generic": {"voltage_init_mode": "UNIFORM_VALUES"},
        "provider": {"slackBusSelectionMode": "MOST_MESHED"},
    }


def test_record_run_loadflow_normalises_none_to_empty_dict():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_run_loadflow(None, None)
    log = script_recorder.get_log()
    assert log[1]["generic"] == {}
    assert log[1]["provider"] == {}


def test_clear_log_wipes_everything():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_run_loadflow({}, {})
    script_recorder.clear_log()
    assert script_recorder.get_log() == []
    assert script_recorder.get_source_filename() is None


# --------------------------------------------------------- Phase 2 helpers


def _changes(rows):
    """Build a tidy {id: {col: value}} DataFrame for the recorder."""
    return pd.DataFrame.from_dict(rows, orient="index")


def test_record_update_components_fans_into_one_op_per_cell():
    script_recorder.record_load_network("a.xiidm", None, None)
    changes = _changes({"L1": {"p0": 30.0, "q0": 15.0}, "L2": {"p0": 50.0}})
    original = _changes({"L1": {"p0": 21.7, "q0": 12.7}, "L2": {"p0": 47.8}})
    script_recorder.record_update_components(
        "Loads", "update_loads", changes, original
    )
    log = script_recorder.get_log()
    # 1 load_network + 3 cell ops.
    assert len(log) == 4
    cells = [op for op in log if op["kind"] == "update_components"]
    keys = {(c["element_id"], c["property"], c["after"]) for c in cells}
    assert keys == {("L1", "p0", 30.0), ("L1", "q0", 15.0), ("L2", "p0", 50.0)}
    # Each carries the before value pulled from original_df.
    befores = {(c["element_id"], c["property"]): c["before"] for c in cells}
    assert befores[("L1", "p0")] == 21.7
    assert befores[("L2", "p0")] == 47.8


def test_record_update_components_skips_nan_cells():
    script_recorder.record_load_network("a.xiidm", None, None)
    # q0 column has a NaN for L1 — must be skipped.
    changes = pd.DataFrame(
        {"p0": [30.0, 50.0], "q0": [float("nan"), 5.0]},
        index=pd.Index(["L1", "L2"]),
    )
    original = pd.DataFrame(
        {"p0": [21.7, 47.8], "q0": [12.7, 4.0]},
        index=pd.Index(["L1", "L2"]),
    )
    script_recorder.record_update_components(
        "Loads", "update_loads", changes, original
    )
    cells = [op for op in script_recorder.get_log() if op["kind"] == "update_components"]
    # 3 non-NaN cells: L1.p0, L2.p0, L2.q0
    assert len(cells) == 3
    assert not any(c["element_id"] == "L1" and c["property"] == "q0" for c in cells)


def test_record_update_components_empty_df_is_a_no_op():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_update_components(
        "Loads", "update_loads", pd.DataFrame(), pd.DataFrame()
    )
    assert len(script_recorder.get_log()) == 1


def test_revert_marks_prior_op_and_appends_revert_op():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_update_components(
        "Loads", "update_loads",
        _changes({"L1": {"p0": 30.0}}),
        _changes({"L1": {"p0": 21.7}}),
    )
    script_recorder.record_update_components(
        "Loads", "update_loads",
        _changes({"L1": {"p0": 21.7}}),
        pd.DataFrame(),
        is_revert=True,
    )
    log = script_recorder.get_log()
    # Original op is marked reverted, revert op was appended.
    edits = [op for op in log if op["kind"] == "update_components"]
    reverts = [op for op in log if op["kind"] == "revert_update_components"]
    assert len(edits) == 1 and edits[0]["reverted"] is True
    assert len(reverts) == 1 and reverts[0]["value"] == 21.7


def test_revert_targets_latest_non_reverted_match():
    """When a cell was edited twice, the second edit is the one that
    gets cancelled by a revert click."""
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_update_components(
        "Loads", "update_loads",
        _changes({"L1": {"p0": 30.0}}),
        _changes({"L1": {"p0": 21.7}}),
    )
    script_recorder.record_update_components(
        "Loads", "update_loads",
        _changes({"L1": {"p0": 50.0}}),
        _changes({"L1": {"p0": 30.0}}),
    )
    script_recorder.record_update_components(
        "Loads", "update_loads",
        _changes({"L1": {"p0": 30.0}}),
        pd.DataFrame(),
        is_revert=True,
    )
    edits = [op for op in script_recorder.get_log() if op["kind"] == "update_components"]
    assert len(edits) == 2
    # First edit untouched, second edit (the most recent before revert) cancelled.
    assert edits[0]["reverted"] is False
    assert edits[0]["after"] == 30.0
    assert edits[1]["reverted"] is True
    assert edits[1]["after"] == 50.0


def test_record_remove_components_appends_one_op():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_remove_components("Loads", ["L1", "L2"])
    log = script_recorder.get_log()
    assert log[-1] == {
        "kind": "remove_components", "component": "Loads",
        "ids": ["L1", "L2"], "reverted": False,
    }


def test_record_remove_components_empty_ids_is_a_no_op():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_remove_components("Loads", [])
    assert len(script_recorder.get_log()) == 1


def test_record_update_extension_fans_into_cells():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_update_extension(
        "activePowerControl",
        _changes({"G1": {"droop": 5.0}}),
        _changes({"G1": {"droop": 4.0}}),
    )
    cells = [op for op in script_recorder.get_log() if op["kind"] == "update_extension"]
    assert len(cells) == 1
    assert cells[0]["extension_name"] == "activePowerControl"
    assert cells[0]["before"] == 4.0
    assert cells[0]["after"] == 5.0


def test_revert_update_extension_marks_matching_op():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_update_extension(
        "activePowerControl",
        _changes({"G1": {"droop": 5.0}}),
        _changes({"G1": {"droop": 4.0}}),
    )
    script_recorder.record_update_extension(
        "activePowerControl",
        _changes({"G1": {"droop": 4.0}}),
        pd.DataFrame(),
        is_revert=True,
    )
    edits = [op for op in script_recorder.get_log() if op["kind"] == "update_extension"]
    reverts = [
        op for op in script_recorder.get_log() if op["kind"] == "revert_update_extension"
    ]
    assert len(edits) == 1 and edits[0]["reverted"] is True
    assert len(reverts) == 1 and reverts[0]["value"] == 4.0


def test_record_remove_extension_appends_one_op():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_remove_extension("activePowerControl", ["G1"])
    log = script_recorder.get_log()
    assert log[-1] == {
        "kind": "remove_extension", "extension_name": "activePowerControl",
        "ids": ["G1"], "reverted": False,
    }


def test_revert_with_no_matching_prior_op_still_appends_revert_marker():
    """If revert is called with no matching edit (shouldn't happen via UI
    but the recorder must not crash), the revert op is still appended."""
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_update_components(
        "Loads", "update_loads",
        _changes({"L1": {"p0": 21.7}}),
        pd.DataFrame(),
        is_revert=True,
    )
    reverts = [op for op in script_recorder.get_log() if op["kind"] == "revert_update_components"]
    assert len(reverts) == 1
