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


# ----------------------------------------------------------- Phase 3 creates


def test_record_create_component_bay_drops_blanks():
    """Empty strings and None must be filtered out before storage."""
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_component_bay(
        "Generators",
        "create_generator_bay",
        {"id": "G1", "min_p": 0.0, "max_p": 100.0,
         "name": "", "target_q": None, "energy_source": "OTHER"},
    )
    op = script_recorder.get_log()[-1]
    assert op["kind"] == "create_component_bay"
    assert op["component"] == "Generators"
    assert op["bay_function"] == "create_generator_bay"
    assert "name" not in op["fields"]  # blank dropped
    assert "target_q" not in op["fields"]  # None dropped
    assert op["fields"]["id"] == "G1"


def test_record_create_branch_bay_appends_one_op():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_branch_bay(
        "Lines",
        "create_line_bays",
        {"id": "L1", "r": 0.1, "x": 1.0},
    )
    op = script_recorder.get_log()[-1]
    assert op["kind"] == "create_branch_bay"
    assert op["bay_function"] == "create_line_bays"


def test_record_create_container_appends_one_op():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_container(
        "Substations", "create_substations", {"id": "S1", "country": "FR"}
    )
    op = script_recorder.get_log()[-1]
    assert op["kind"] == "create_container"
    assert op["create_function"] == "create_substations"


def test_record_create_tap_changer_captures_steps_and_defaults():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_tap_changer(
        "Ratio", "create_ratio_tap_changers", "T1",
        main_fields={"tap": 1, "low_tap": 0, "regulating": False, "oltc": False},
        step_columns=["r", "x", "g", "b", "rho"],
        step_defaults={"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.0},
        steps=[{"rho": 0.9}, {"rho": 1.0}, {"rho": 1.1}],
    )
    op = script_recorder.get_log()[-1]
    assert op["kind"] == "create_tap_changer"
    assert op["tap_changer_kind"] == "Ratio"
    assert op["transformer_id"] == "T1"
    assert len(op["steps"]) == 3
    assert op["step_defaults"]["rho"] == 1.0


def test_record_create_coupling_device_handles_empty_prefix():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_coupling_device("B1", "B2", "")
    op = script_recorder.get_log()[-1]
    assert op["kind"] == "create_coupling_device"
    assert op["switch_prefix"] is None  # empty -> None


def test_record_create_hvdc_line_appends_one_op():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_hvdc_line(
        {"id": "H1", "r": 1.0, "nominal_v": 400.0, "max_p": 1000.0,
         "target_p": 0.0, "converters_mode": "SIDE_1_RECTIFIER_SIDE_2_INVERTER",
         "converter_station1_id": "CS1", "converter_station2_id": "CS2"}
    )
    op = script_recorder.get_log()[-1]
    assert op["kind"] == "create_hvdc_line"
    assert op["fields"]["id"] == "H1"


def test_record_create_reactive_limits_minmax_and_curve():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_reactive_limits(
        "G1", "minmax", [{"min_q": -100.0, "max_q": 100.0}]
    )
    script_recorder.record_create_reactive_limits(
        "G2", "curve",
        [{"p": 0.0, "min_q": -50.0, "max_q": 50.0},
         {"p": 100.0, "min_q": -40.0, "max_q": 40.0}],
    )
    log = script_recorder.get_log()
    assert log[-2]["mode"] == "minmax"
    assert log[-1]["mode"] == "curve"
    assert len(log[-1]["payload"]) == 2


def test_record_create_operational_limits_keeps_group_name():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_operational_limits(
        "L1", "ONE", "CURRENT",
        [{"name": "permanent", "value": 1000.0, "acceptable_duration": -1}],
        group_name="DEFAULT",
    )
    op = script_recorder.get_log()[-1]
    assert op["kind"] == "create_operational_limits"
    assert op["group_name"] == "DEFAULT"
    assert op["element_id"] == "L1"


def test_record_create_extension_keeps_index_col():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_extension(
        "slackTerminal", "VL1", {"bus_id": "B1"}, "voltage_level_id"
    )
    op = script_recorder.get_log()[-1]
    assert op["kind"] == "create_extension"
    assert op["index_col"] == "voltage_level_id"
    assert op["target_id"] == "VL1"


def test_record_create_secondary_voltage_control_captures_both_lists():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_create_secondary_voltage_control(
        [{"name": "Z1", "target_v": 400.0, "bus_ids": "B1 B2"}],
        [{"unit_id": "G1", "zone_name": "Z1", "participate": True}],
    )
    op = script_recorder.get_log()[-1]
    assert op["kind"] == "create_secondary_voltage_control"
    assert len(op["zones"]) == 1
    assert len(op["units"]) == 1


# ----------------------------------------------------------- Phase 4 SA


def test_record_run_security_analysis_captures_every_kwarg():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_run_security_analysis(
        contingencies=[{"id": "N1", "element_id": "L1"}],
        monitored_elements=[{"contingency_context_type": "ALL"}],
        limit_reductions=[{"limit_type": "CURRENT", "permanent": True,
                           "temporary": False, "value": 0.9}],
        actions=[{"action_id": "A1", "type": "SWITCH",
                  "switch_id": "BR1", "open": True}],
        operator_strategies=[{
            "operator_strategy_id": "OS1", "contingency_id": "N1",
            "action_ids": ["A1"], "condition_type": "TRUE_CONDITION",
        }],
        contingencies_json_paths=["/tmp/c.json"],
        actions_json_paths=["/tmp/a.json"],
        operator_strategies_json_paths=["/tmp/o.json"],
        lf_generic={"distributed_slack": True},
        lf_provider={"slackBusSelectionMode": "MOST_MESHED"},
    )
    op = script_recorder.get_log()[-1]
    assert op["kind"] == "run_security_analysis"
    assert len(op["contingencies"]) == 1
    assert len(op["monitored_elements"]) == 1
    assert len(op["limit_reductions"]) == 1
    assert len(op["actions"]) == 1
    assert len(op["operator_strategies"]) == 1
    assert op["contingencies_json_paths"] == ["/tmp/c.json"]
    assert op["actions_json_paths"] == ["/tmp/a.json"]
    assert op["operator_strategies_json_paths"] == ["/tmp/o.json"]
    assert op["lf_generic"]["distributed_slack"] is True
    assert op["lf_provider"]["slackBusSelectionMode"] == "MOST_MESHED"


def test_record_run_security_analysis_normalises_none_to_empty():
    script_recorder.record_load_network("a.xiidm", None, None)
    script_recorder.record_run_security_analysis(
        contingencies=[],
        monitored_elements=None,
        limit_reductions=None,
        actions=None,
        operator_strategies=None,
        contingencies_json_paths=None,
        actions_json_paths=None,
        operator_strategies_json_paths=None,
        lf_generic=None,
        lf_provider=None,
    )
    op = script_recorder.get_log()[-1]
    for key in (
        "contingencies", "monitored_elements", "limit_reductions",
        "actions", "operator_strategies",
        "contingencies_json_paths", "actions_json_paths",
        "operator_strategies_json_paths",
    ):
        assert op[key] == []
    assert op["lf_generic"] == {}
    assert op["lf_provider"] == {}


def test_record_run_security_analysis_deep_copies_payload():
    """Mutating the input dicts after recording must not affect the log."""
    script_recorder.record_load_network("a.xiidm", None, None)
    contingencies = [{"id": "N1", "element_id": "L1"}]
    script_recorder.record_run_security_analysis(
        contingencies=contingencies,
        monitored_elements=None,
        limit_reductions=None,
        actions=None,
        operator_strategies=None,
        contingencies_json_paths=None,
        actions_json_paths=None,
        operator_strategies_json_paths=None,
        lf_generic=None,
        lf_provider=None,
    )
    contingencies[0]["id"] = "MUTATED"
    op = script_recorder.get_log()[-1]
    assert op["contingencies"][0]["id"] == "N1"
