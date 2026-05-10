"""Tests for the script_recorder op log.

The recorder is pure session-state manipulation — no pypowsybl, no
worker thread. We exercise it directly against Streamlit's session
state and assert the resulting log shape.
"""
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
