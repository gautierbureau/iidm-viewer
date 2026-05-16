"""Tests for the framework-agnostic ``iidm_viewer.loadflow`` module.

Streamlit's ``state.run_loadflow`` and the prototypes' AppState
methods all delegate here.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from iidm_viewer import network_loader
from iidm_viewer.loadflow import (
    GENERIC_PARAMETERS,
    LoadFlowResult,
    get_provider_parameters_df,
    run_ac,
)
from iidm_viewer.powsybl_worker import NetworkProxy


ROOT = Path(__file__).resolve().parent.parent
XIIDM = ROOT / "test_ieee14.xiidm"


@pytest.fixture(scope="module")
def ieee14() -> NetworkProxy:
    return network_loader.load_from_path(str(XIIDM))


def test_generic_parameters_schema_matches_streamlit():
    """Drift guard: the legacy private _GENERIC_PARAMS in lf_parameters
    must point at the same list object."""
    pytest.importorskip("streamlit")
    from iidm_viewer.lf_parameters import _GENERIC_PARAMS
    assert _GENERIC_PARAMS is GENERIC_PARAMETERS


def test_generic_parameters_carry_expected_entries():
    names = {p[0] for p in GENERIC_PARAMETERS}
    assert "voltage_init_mode" in names
    assert "balance_type" in names
    assert "distributed_slack" in names
    # Every entry has at least (name, type, default, description).
    for entry in GENERIC_PARAMETERS:
        assert len(entry) >= 4


def test_get_provider_parameters_df_returns_dataframe():
    df = get_provider_parameters_df()
    assert isinstance(df, pd.DataFrame)
    # OpenLoadFlow always carries a few provider params; expect non-empty.
    assert df.shape[0] > 0


def test_run_ac_converges_on_ieee14(ieee14):
    result = run_ac(ieee14)
    assert isinstance(result, LoadFlowResult)
    assert result.status in {"CONVERGED", "MAX_ITERATION_REACHED", "FAILED"}
    # IEEE14 with default settings is well-conditioned.
    assert result.converged is True
    # report_json is a non-empty JSON document.
    assert isinstance(result.report_json, str)
    assert result.report_json.strip().startswith("{")


def test_run_ac_accepts_generic_params(ieee14):
    """Passing a known-bool param flips it through to pypowsybl."""
    result = run_ac(ieee14, generic_params={"distributed_slack": False})
    assert isinstance(result, LoadFlowResult)


def test_loadflow_result_status_for_empty_results():
    result = LoadFlowResult([], "{}")
    assert result.status == "UNKNOWN"
    assert result.converged is False
