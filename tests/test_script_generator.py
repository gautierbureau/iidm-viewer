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
