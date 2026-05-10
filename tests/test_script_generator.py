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

