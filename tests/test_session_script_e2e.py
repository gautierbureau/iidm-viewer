"""End-to-end smoke test for the HMI-mirror script feature.

Records a small session via the recorder, generates a Python script,
runs it under the *same* interpreter via subprocess against the real
``test_ieee14.xiidm`` fixture, and asserts it exits cleanly. This is
the only test that actually executes a generated script — everything
else (in ``test_script_generator.py``) just compiles the source.

Skipped when pypowsybl is unavailable, since the subprocess call would
fail for reasons unrelated to the recorder/generator pipeline.
"""
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st

pypowsybl = pytest.importorskip("pypowsybl")

from iidm_viewer import script_recorder  # noqa: E402
from iidm_viewer.script_generator import generate_script  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
XIIDM = ROOT / "test_ieee14.xiidm"


def setup_function(_):
    st.session_state.clear()


def _record_minimal_session() -> None:
    """Build an op log that exercises the three most common paths:
    load_network, an edit batch, and a load-flow run."""
    script_recorder.record_load_network(
        "test_ieee14.xiidm", parameters=None, post_processors=None
    )
    script_recorder.record_update_components(
        "Loads",
        "update_loads",
        pd.DataFrame({"p0": [30.0]}, index=pd.Index(["B2-L"], name="id")),
        pd.DataFrame({"p0": [21.7]}, index=pd.Index(["B2-L"], name="id")),
    )
    script_recorder.record_run_loadflow(
        generic={"distributed_slack": True}, provider={}
    )


def test_generated_script_runs_against_real_network(tmp_path):
    _record_minimal_session()

    script = generate_script(
        script_recorder.get_log(),
        source_filename="test_ieee14.xiidm",
    )

    script_path = tmp_path / "session.py"
    script_path.write_text(script)

    # The XIIDM fixture lives at the repo root; the script takes the
    # path as its single CLI argument (argparse).
    result = subprocess.run(
        [sys.executable, str(script_path), str(XIIDM)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"Script exited {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    # The recorded run_loadflow op makes the script print the LF status.
    # IEEE-14 always converges with the default LF parameters.
    assert "Load flow: CONVERGED" in result.stdout


def test_generated_script_with_creates_runs_against_blank_network(tmp_path):
    """Drives the create_container + create_component_bay paths end-to-end.

    Starts from an empty network, builds a substation + node-breaker
    voltage level + busbar section, then creates a generator on the
    busbar via a feeder bay. Runs the script under subprocess.
    """
    script_recorder.record_create_empty("smoke")
    script_recorder.record_create_container(
        "Substations", "create_substations", {"id": "S1", "country": "FR"}
    )
    script_recorder.record_create_container(
        "Voltage Levels",
        "create_voltage_levels",
        {
            "id": "VL1",
            "substation_id": "S1",
            "topology_kind": "NODE_BREAKER",
            "nominal_v": 225.0,
        },
    )
    script_recorder.record_create_container(
        "Busbar Sections",
        "create_busbar_sections",
        {"id": "BBS1", "voltage_level_id": "VL1", "node": 0},
    )
    script_recorder.record_create_component_bay(
        "Generators",
        "create_generator_bay",
        {
            "id": "G_NEW",
            "energy_source": "OTHER",
            "min_p": 0.0,
            "max_p": 100.0,
            "target_p": 50.0,
            "voltage_regulator_on": False,
            "target_q": 0.0,
            "bus_or_busbar_section_id": "BBS1",
            "position_order": 10,
            "direction": "BOTTOM",
        },
    )

    script = generate_script(script_recorder.get_log())

    script_path = tmp_path / "session.py"
    script_path.write_text(script)

    # No CLI arg needed: create_empty path skips argparse.
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"Script exited {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
