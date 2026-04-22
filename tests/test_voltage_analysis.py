import pandas as pd
import pytest
from iidm_viewer.state import create_component_bay
from iidm_viewer.voltage_analysis import (
    _bus_voltages,
    _shunt_compensation,
    _svc_compensation,
)

_SHUNT_ID = "VA_TEST_SHUNT"
_SHUNT_B_PER_SECTION = 1e-4
_SHUNT_SECTION_COUNT = 2
_SHUNT_MAX_SECTION_COUNT = 5


@pytest.fixture
def network_with_shunt(node_breaker_network):
    """node_breaker_network with one LINEAR shunt compensator added to S1VL1."""
    create_component_bay(node_breaker_network, "Shunt Compensators", {
        "id": _SHUNT_ID,
        "bus_or_busbar_section_id": "S1VL1_BBS",
        "section_count": _SHUNT_SECTION_COUNT,
        "max_section_count": _SHUNT_MAX_SECTION_COUNT,
        "g_per_section": 0.0,
        "b_per_section": _SHUNT_B_PER_SECTION,
        "target_v": 0.0,
        "target_deadband": 0.0,
        "position_order": 300,
        "direction": "BOTTOM",
    })
    return node_breaker_network


# ── _bus_voltages ──────────────────────────────────────────────────────────────

def test_bus_voltages_returns_expected_columns(node_breaker_network):
    df = _bus_voltages(node_breaker_network)
    assert not df.empty
    assert set(df.columns) >= {"bus_id", "voltage_level_id", "nominal_v", "v_mag", "v_pu"}


def test_bus_voltages_nominal_v_populated(node_breaker_network):
    df = _bus_voltages(node_breaker_network)
    assert df["nominal_v"].notna().all(), "every bus must have a nominal_v from its VL"


def test_bus_voltages_v_pu_equals_v_mag_over_nominal(node_breaker_network):
    df = _bus_voltages(node_breaker_network)
    valid = df.dropna(subset=["v_mag", "v_pu"])
    if valid.empty:
        pytest.skip("network has no solved voltages (no load flow)")
    expected = valid["v_mag"] / valid["nominal_v"]
    assert (abs(valid["v_pu"] - expected) < 1e-9).all()


def test_bus_voltages_voltage_level_ids_are_strings(node_breaker_network):
    df = _bus_voltages(node_breaker_network)
    # pandas 2.x may use StringDtype instead of object for string columns
    assert df["voltage_level_id"].dtype == object or pd.api.types.is_string_dtype(df["voltage_level_id"])


# ── _shunt_compensation — empty network ────────────────────────────────────────

def test_shunt_compensation_empty_when_no_shunts():
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="empty")
    net = NetworkProxy(run(_make))
    df = _shunt_compensation(net)
    assert df.empty


# ── _shunt_compensation — network with shunt ───────────────────────────────────

def test_shunt_compensation_returns_expected_columns(network_with_shunt):
    df = _shunt_compensation(network_with_shunt)
    assert not df.empty
    assert set(df.columns) >= {
        "id", "voltage_level_id", "nominal_v", "connected",
        "section_count", "max_section_count",
        "current_q_mvar", "available_q_mvar", "total_q_mvar", "b_per_section",
    }


def test_shunt_compensation_b_per_section_positive_for_capacitive(network_with_shunt):
    df = _shunt_compensation(network_with_shunt)
    row = df[df["id"] == _SHUNT_ID].iloc[0]
    assert row["b_per_section"] == pytest.approx(_SHUNT_B_PER_SECTION, rel=1e-6)


def test_shunt_compensation_current_q_estimated_without_lf(network_with_shunt):
    df = _shunt_compensation(network_with_shunt)
    row = df[df["id"] == _SHUNT_ID].iloc[0]
    # No LF → estimated as −b × V² (pypowsybl load-sign: Q < 0 for capacitors)
    # b = b_per_section × section_count = 1e-4 × 2 = 2e-4
    b_total = _SHUNT_B_PER_SECTION * _SHUNT_SECTION_COUNT
    expected = -b_total * row["nominal_v"] ** 2
    assert abs(row["current_q_mvar"] - expected) < 1e-6


def test_shunt_compensation_available_q_covers_remaining_sections(network_with_shunt):
    df = _shunt_compensation(network_with_shunt)
    row = df[df["id"] == _SHUNT_ID].iloc[0]
    # available = −bps × remaining × V² (load-sign convention)
    bps = _SHUNT_B_PER_SECTION
    remaining = _SHUNT_MAX_SECTION_COUNT - _SHUNT_SECTION_COUNT
    expected = -bps * remaining * row["nominal_v"] ** 2
    assert abs(row["available_q_mvar"] - expected) < 1e-6


def test_shunt_compensation_total_q_is_max_capacity(network_with_shunt):
    df = _shunt_compensation(network_with_shunt)
    row = df[df["id"] == _SHUNT_ID].iloc[0]
    # total = −bps × max_section_count × V² (load-sign convention)
    bps = _SHUNT_B_PER_SECTION
    expected = -bps * _SHUNT_MAX_SECTION_COUNT * row["nominal_v"] ** 2
    assert abs(row["total_q_mvar"] - expected) < 1e-6


def test_shunt_compensation_current_plus_available_equals_total(network_with_shunt):
    df = _shunt_compensation(network_with_shunt)
    row = df[df["id"] == _SHUNT_ID].iloc[0]
    # current + available = total when connected and no LF override
    assert abs(row["current_q_mvar"] + row["available_q_mvar"] - row["total_q_mvar"]) < 1e-6


def test_shunt_compensation_nominal_v_matches_voltage_level(network_with_shunt):
    df = _shunt_compensation(network_with_shunt)
    row = df[df["id"] == _SHUNT_ID].iloc[0]
    vls = network_with_shunt.get_voltage_levels(attributes=["nominal_v"])
    expected_nom_v = float(vls.loc[row["voltage_level_id"], "nominal_v"])
    assert abs(row["nominal_v"] - expected_nom_v) < 1e-9


# ── _svc_compensation ──────────────────────────────────────────────────────────

def test_svc_compensation_returns_one_row(node_breaker_network):
    df = _svc_compensation(node_breaker_network)
    assert len(df) == 1, "four_substations network has exactly one SVC"


def test_svc_compensation_returns_expected_columns(node_breaker_network):
    df = _svc_compensation(node_breaker_network)
    assert set(df.columns) >= {
        "id", "voltage_level_id", "nominal_v", "connected",
        "regulation_mode", "current_q_mvar", "q_min_mvar", "q_max_mvar",
    }


def test_svc_q_range_matches_b_min_b_max(node_breaker_network):
    df = _svc_compensation(node_breaker_network)
    row = df.iloc[0]
    svcs = node_breaker_network.get_static_var_compensators()
    svc = svcs.loc[row["id"]]
    vls = node_breaker_network.get_voltage_levels(attributes=["nominal_v"])
    nom_v = float(vls.loc[svc["voltage_level_id"], "nominal_v"])
    assert abs(row["q_min_mvar"] - svc["b_min"] * nom_v ** 2) < 1e-6
    assert abs(row["q_max_mvar"] - svc["b_max"] * nom_v ** 2) < 1e-6


def test_svc_q_min_less_than_or_equal_q_max(node_breaker_network):
    df = _svc_compensation(node_breaker_network)
    assert (df["q_min_mvar"] <= df["q_max_mvar"]).all()


def test_svc_nominal_v_populated(node_breaker_network):
    df = _svc_compensation(node_breaker_network)
    assert df["nominal_v"].notna().all()


# ── AppTest smoke ──────────────────────────────────────────────────────────────

def test_voltage_analysis_tab_no_exception(xiidm_upload):
    from streamlit.testing.v1 import AppTest
    from iidm_viewer.state import load_network

    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = load_network(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.run(timeout=30)
    assert not at.exception
