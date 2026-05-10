"""Tests for the pure-python script generator.

These tests do not touch pypowsybl or Streamlit — they feed fixture op
logs to ``generate_script`` and assert the output. Every emitted script
is also fed through ``compile()`` to catch syntax regressions.
"""
from datetime import datetime

import pytest

from iidm_viewer.script_generator import generate_script


FIXED_TS = datetime(2026, 5, 10, 12, 0, 0)


def _compile(script: str) -> None:
    compile(script, "<generated>", "exec")


def test_empty_log_emits_runnable_stub():
    script = generate_script([], timestamp=FIXED_TS)
    _compile(script)
    # Header shows the timestamp and an explicit "empty start" marker.
    assert "2026-05-10T12:00:00" in script
    assert "Source network: <empty start>" in script
    # Body falls back to ``pass`` and main still parses argparse so the
    # script runs against any user-supplied network path.
    assert "def process(network):\n    pass" in script
    assert "argparse" in script
    assert "pn.load(args.network_path)" in script


def test_load_network_only():
    ops = [{"kind": "load_network", "parameters": {}, "post_processors": []}]
    script = generate_script(ops, source_filename="grid.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "Source network: grid.xiidm" in script
    assert "pn.load(args.network_path)" in script
    # No parameters / post_processors → no extra kwargs.
    assert "parameters=" not in script.split("def main()")[1]
    assert "post_processors=" not in script.split("def main()")[1]


def test_load_network_with_parameters_and_post_processors():
    ops = [
        {
            "kind": "load_network",
            "parameters": {"iidm.import.xml.skip-validation": "true"},
            "post_processors": ["loadflowResultsCompletion"],
        }
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "'iidm.import.xml.skip-validation': 'true'" in script
    assert "'loadflowResultsCompletion'" in script
    assert "pn.load(args.network_path, parameters=" in script
    assert "post_processors=" in script


def test_create_empty_skips_argparse():
    ops = [{"kind": "create_empty", "network_id": "blank"}]
    script = generate_script(ops, timestamp=FIXED_TS)
    _compile(script)
    assert "pn.create_empty(network_id='blank')" in script
    # Empty-start scripts don't need a CLI arg; the network is created in-place.
    assert "p.add_argument" not in script
    assert "args.network_path" not in script


def test_run_loadflow_emits_parameters_and_call():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        {
            "kind": "run_loadflow",
            "generic": {
                "voltage_init_mode": "UNIFORM_VALUES",
                "distributed_slack": True,
                "dc_power_factor": 1.0,
            },
            "provider": {},
        },
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "lf.Parameters(voltage_init_mode='UNIFORM_VALUES'" in script
    assert "distributed_slack=True" in script
    assert "dc_power_factor=1.0" in script
    assert "lf.run_ac(network, parameters=_lf_params)" in script
    # No provider params -> no provider_parameters line.
    assert "provider_parameters" not in script


def test_run_loadflow_emits_provider_parameters_when_present():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        {
            "kind": "run_loadflow",
            "generic": {},
            "provider": {"slackBusSelectionMode": "MOST_MESHED"},
        },
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "_lf_params.provider_parameters" in script
    assert "'slackBusSelectionMode': 'MOST_MESHED'" in script


def test_reverted_op_excluded_by_default():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        {"kind": "run_loadflow", "generic": {}, "provider": {}, "reverted": True},
    ]
    default = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(default)
    assert "lf.run_ac" not in default
    assert "def process(network):\n    pass" in default

    full = generate_script(
        ops, source_filename="g.xiidm", timestamp=FIXED_TS, include_reverted=True
    )
    _compile(full)
    assert "lf.run_ac(network, parameters=_lf_params)" in full


def test_run_loadflow_without_load_network_still_compiles():
    """If the log is cleared mid-session, the script must still parse and run."""
    ops = [{"kind": "run_loadflow", "generic": {}, "provider": {}}]
    script = generate_script(ops, timestamp=FIXED_TS)
    _compile(script)
    # Falls back to argparse path loading.
    assert "args.network_path" in script
    assert "lf.run_ac" in script


@pytest.mark.parametrize(
    "ops",
    [
        [],
        [{"kind": "load_network", "parameters": {}, "post_processors": []}],
        [{"kind": "create_empty", "network_id": "x"}],
        [
            {"kind": "load_network", "parameters": {}, "post_processors": []},
            {"kind": "run_loadflow", "generic": {"distributed_slack": False}, "provider": {}},
        ],
    ],
)
def test_generated_scripts_always_compile(ops):
    _compile(generate_script(ops, timestamp=FIXED_TS))


# ---------------------------------------------------------- Phase 2 op kinds


def _mk_update_op(eid, prop, before, after, component="Loads",
                  method="update_loads", reverted=False):
    return {
        "kind": "update_components",
        "component": component, "method_name": method,
        "element_id": eid, "property": prop,
        "before": before, "after": after,
        "reverted": reverted,
    }


def _mk_revert_op(eid, prop, value, component="Loads", method="update_loads"):
    return {
        "kind": "revert_update_components",
        "component": component, "method_name": method,
        "element_id": eid, "property": prop,
        "value": value,
    }


def test_single_update_emits_dataframe_call_and_imports_pandas():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        _mk_update_op("L1", "p0", 21.7, 30.0),
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "import pandas as pd" in script
    assert "network.update_loads(" in script
    assert "'L1': {'p0': 30.0}" in script


def test_no_update_ops_means_no_pandas_import():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        {"kind": "run_loadflow", "generic": {}, "provider": {}},
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "import pandas as pd" not in script


def test_adjacent_updates_with_same_method_are_batched():
    """Two cell-level ops sharing a method should fuse into one call."""
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        _mk_update_op("L1", "p0", 21.7, 30.0),
        _mk_update_op("L2", "q0", 10.0, 15.0),
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    # One update call mentioning both rows.
    assert script.count("network.update_loads(") == 1
    assert "'L1': {'p0': 30.0}" in script
    assert "'L2': {'q0': 15.0}" in script


def test_different_methods_do_not_batch():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        _mk_update_op("L1", "p0", 21.7, 30.0, component="Loads", method="update_loads"),
        _mk_update_op("G1", "target_p", 100.0, 120.0,
                      component="Generators", method="update_generators"),
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "network.update_loads(" in script
    assert "network.update_generators(" in script


def test_reverted_update_excluded_in_net_state_mode():
    """In net-state mode the original edit AND its revert disappear."""
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        _mk_update_op("L1", "p0", 21.7, 30.0, reverted=True),
        _mk_revert_op("L1", "p0", 21.7),
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "update_loads" not in script
    assert "def process(network):\n    pass" in script


def test_full_transcript_emits_revert_step():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        _mk_update_op("L1", "p0", 21.7, 30.0, reverted=True),
        _mk_revert_op("L1", "p0", 21.7),
    ]
    script = generate_script(
        ops, source_filename="g.xiidm", timestamp=FIXED_TS, include_reverted=True
    )
    _compile(script)
    # Two distinct update_loads calls: the edit and the revert.
    assert script.count("network.update_loads(") == 2
    assert "'L1': {'p0': 30.0}" in script
    assert "'L1': {'p0': 21.7}" in script
    assert "# Revert Loads" in script


def test_remove_components_emits_helper_and_dispatcher_call():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        {"kind": "remove_components", "component": "Loads",
         "ids": ["L1", "L2"], "reverted": False},
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "def _remove(network, component, ids):" in script
    assert "_remove(network, 'Loads', ['L1', 'L2'])" in script
    # Helper imports nothing extra — relies on the module-level pn import.
    assert "pn.remove_feeder_bays" in script


def test_no_remove_op_means_no_remove_helper():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        _mk_update_op("L1", "p0", 21.7, 30.0),
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "def _remove(" not in script


def test_remove_extension_uses_native_call_not_helper():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        {"kind": "remove_extension", "extension_name": "activePowerControl",
         "ids": ["G1"], "reverted": False},
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "network.remove_extensions('activePowerControl', ['G1'])" in script
    assert "def _remove(" not in script


def test_update_extension_uses_update_extensions_call():
    ops = [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        {
            "kind": "update_extension",
            "extension_name": "activePowerControl",
            "element_id": "G1", "property": "droop",
            "before": 4.0, "after": 5.0, "reverted": False,
        },
    ]
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "network.update_extensions('activePowerControl'" in script
    assert "'G1': {'droop': 5.0}" in script


# -------------------------------------------------------- Phase 3 creations


def _with_load(*ops):
    return [
        {"kind": "load_network", "parameters": {}, "post_processors": []},
        *ops,
    ]


def test_create_component_bay_generator_uses_bay_df_helper():
    ops = _with_load(
        {
            "kind": "create_component_bay",
            "component": "Generators",
            "bay_function": "create_generator_bay",
            "fields": {"id": "G_NEW", "min_p": 0.0, "max_p": 100.0,
                       "bus_or_busbar_section_id": "BBS1", "position_order": 10},
        }
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "def _bay_df(fields):" in script
    assert "pn.create_generator_bay(network, _bay_df(" in script
    assert "'G_NEW'" in script


def test_create_shunt_compensator_uses_shunt_bay_helper():
    ops = _with_load(
        {
            "kind": "create_component_bay",
            "component": "Shunt Compensators",
            "bay_function": "create_shunt_compensator_bay",
            "fields": {
                "id": "SH1", "section_count": 1, "max_section_count": 1,
                "g_per_section": 0.0, "b_per_section": 1e-5,
                "bus_or_busbar_section_id": "BBS1", "position_order": 10,
            },
        }
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "def _create_shunt_bay(network, fields):" in script
    assert "_create_shunt_bay(network, " in script
    # _bay_df is not needed when only shunt creates exist.
    assert "def _bay_df(" not in script


def test_create_branch_bay_emits_pn_call():
    ops = _with_load(
        {
            "kind": "create_branch_bay",
            "component": "Lines",
            "bay_function": "create_line_bays",
            "fields": {"id": "L1", "r": 0.1, "x": 1.0,
                       "bus_or_busbar_section_id_1": "BBS1",
                       "bus_or_busbar_section_id_2": "BBS2"},
        }
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "pn.create_line_bays(network, _bay_df(" in script


def test_create_container_emits_helper_call():
    ops = _with_load(
        {
            "kind": "create_container",
            "component": "Substations",
            "create_function": "create_substations",
            "fields": {"id": "S1", "country": "FR"},
        }
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "def _create_container(" in script
    assert "_create_container(network, 'create_substations', {'id': 'S1', 'country': 'FR'})" in script


def test_create_tap_changer_emits_helper_call_with_both_dataframes():
    ops = _with_load(
        {
            "kind": "create_tap_changer",
            "tap_changer_kind": "Ratio",
            "create_method": "create_ratio_tap_changers",
            "transformer_id": "T1",
            "main_fields": {"tap": 1, "low_tap": 0, "regulating": False, "oltc": False},
            "step_columns": ["r", "x", "g", "b", "rho"],
            "step_defaults": {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.0},
            "steps": [
                {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 0.9},
                {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.0},
                {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.1},
            ],
        }
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "def _create_tap_changer(network, method, transformer_id" in script
    assert "_create_tap_changer(" in script
    assert "'create_ratio_tap_changers'" in script
    assert "'T1'" in script


def test_create_coupling_device_emits_pn_call():
    ops = _with_load(
        {
            "kind": "create_coupling_device",
            "bbs1": "BBS1", "bbs2": "BBS2",
            "switch_prefix": "TIE",
        }
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "pn.create_coupling_device(network, bus_or_busbar_section_id_1='BBS1'" in script
    assert "switch_prefix_id='TIE'" in script


def test_create_coupling_device_without_prefix_omits_kwarg():
    ops = _with_load(
        {"kind": "create_coupling_device", "bbs1": "B1", "bbs2": "B2", "switch_prefix": None}
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "switch_prefix_id" not in script


def test_create_hvdc_line_emits_create_hvdc_lines_call():
    ops = _with_load(
        {
            "kind": "create_hvdc_line",
            "fields": {
                "id": "H1", "r": 1.0, "nominal_v": 400.0, "max_p": 1000.0,
                "target_p": 0.0, "converters_mode": "SIDE_1_RECTIFIER_SIDE_2_INVERTER",
                "converter_station1_id": "CS1", "converter_station2_id": "CS2",
            },
        }
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "network.create_hvdc_lines(_bay_df(" in script
    assert "'H1'" in script


def test_create_reactive_limits_minmax_and_curve():
    ops = _with_load(
        {
            "kind": "create_reactive_limits", "element_id": "G1", "mode": "minmax",
            "payload": [{"min_q": -100.0, "max_q": 100.0}],
        },
        {
            "kind": "create_reactive_limits", "element_id": "G2", "mode": "curve",
            "payload": [
                {"p": 0.0, "min_q": -50.0, "max_q": 50.0},
                {"p": 100.0, "min_q": -40.0, "max_q": 40.0},
            ],
        },
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "def _create_reactive_limits(" in script
    assert "_create_reactive_limits(network, 'G1', 'minmax'" in script
    assert "_create_reactive_limits(network, 'G2', 'curve'" in script


def test_create_operational_limits_emits_helper_with_group_name():
    ops = _with_load(
        {
            "kind": "create_operational_limits",
            "element_id": "L1", "side": "ONE", "limit_type": "CURRENT",
            "limits": [
                {"name": "permanent", "value": 1000.0, "acceptable_duration": -1,
                 "fictitious": False},
                {"name": "TATL_60", "value": 1200.0, "acceptable_duration": 60,
                 "fictitious": False},
            ],
            "group_name": "DEFAULT",
        }
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "def _create_operational_limits(" in script
    assert "group_name='DEFAULT'" in script
    assert "'L1'" in script


def test_create_extension_uses_index_col_in_helper():
    ops = _with_load(
        {
            "kind": "create_extension",
            "extension_name": "substationPosition",
            "target_id": "S1",
            "row": {"latitude": 48.85, "longitude": 2.35},
            "index_col": "id",
        }
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "def _create_extension(" in script
    assert "_create_extension(" in script
    assert "'substationPosition'" in script
    assert "'S1'" in script


def test_create_secondary_voltage_control_emits_helper():
    ops = _with_load(
        {
            "kind": "create_secondary_voltage_control",
            "zones": [
                {"name": "Z1", "target_v": 400.0, "bus_ids": "B1 B2"},
            ],
            "units": [
                {"unit_id": "G1", "zone_name": "Z1", "participate": True},
            ],
        }
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    assert "def _create_secondary_voltage_control(" in script
    assert "_create_secondary_voltage_control(" in script
    assert "'Z1'" in script
    assert "'G1'" in script


def test_helpers_only_emitted_when_needed():
    """A log with no creations or removals must not pull in any helper."""
    ops = _with_load(
        _mk_update_op("L1", "p0", 21.7, 30.0),
        {"kind": "run_loadflow", "generic": {}, "provider": {}},
    )
    script = generate_script(ops, source_filename="g.xiidm", timestamp=FIXED_TS)
    _compile(script)
    for helper in (
        "_remove", "_bay_df", "_create_shunt_bay", "_create_container",
        "_create_tap_changer", "_create_reactive_limits",
        "_create_operational_limits", "_create_extension",
        "_create_secondary_voltage_control",
    ):
        assert f"def {helper}" not in script, f"unexpected helper {helper}"


