"""Tests for session state, dataframe helpers, and filter logic."""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from iidm_viewer.state import (
    CREATABLE_COMPONENTS,
    create_component_bay,
    filter_voltage_levels,
    get_voltage_levels_df,
    list_busbar_sections,
    list_node_breaker_voltage_levels,
    load_network,
)


def _vls_df(rows):
    df = pd.DataFrame(rows)
    df["display"] = df.apply(lambda r: r["name"] or r["id"], axis=1)
    return df


def test_filter_voltage_levels_returns_input_when_no_text():
    df = _vls_df([{"id": "VL1", "name": ""}, {"id": "VL2", "name": "Alpha"}])
    assert filter_voltage_levels(df, "").equals(df)
    assert filter_voltage_levels(df, None).equals(df)


def test_filter_voltage_levels_matches_substring():
    df = _vls_df([
        {"id": "VL1", "name": "Alpha"},
        {"id": "VL2", "name": "Beta"},
        {"id": "VL3", "name": "Alphabet"},
    ])
    out = filter_voltage_levels(df, "Alpha")
    assert set(out["id"]) == {"VL1", "VL3"}


def test_filter_voltage_levels_is_case_insensitive():
    df = _vls_df([
        {"id": "VL1", "name": "Alpha"},
        {"id": "VL2", "name": "Beta"},
    ])
    out = filter_voltage_levels(df, "alpha")
    assert list(out["id"]) == ["VL1"]


def test_filter_voltage_levels_uses_literal_not_regex():
    df = _vls_df([
        {"id": "VL1", "name": "A.B"},
        {"id": "VL2", "name": "AXB"},
    ])
    out = filter_voltage_levels(df, ".")
    assert list(out["id"]) == ["VL1"], "dot should match literally, not as regex"


def test_filter_voltage_levels_falls_back_to_id_when_name_blank():
    df = _vls_df([
        {"id": "VL_BLANK", "name": ""},
        {"id": "VL_OTHER", "name": "named"},
    ])
    out = filter_voltage_levels(df, "BLANK")
    assert list(out["id"]) == ["VL_BLANK"]


def test_get_voltage_levels_df_columns_and_sort(xiidm_upload):
    net = load_network(xiidm_upload)
    df = get_voltage_levels_df(net)
    assert {"id", "name", "substation_id", "nominal_v", "display"}.issubset(df.columns)
    # sorted ascending by display
    assert list(df["display"]) == sorted(df["display"])


def test_init_state_populates_defaults():
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    assert at.session_state["network"] is None
    assert at.session_state["selected_vl"] is None
    assert at.session_state["nad_depth"] == 1
    assert at.session_state["component_type"] == "Voltage Levels"


def test_init_state_does_not_overwrite_existing_keys():
    at = AppTest.from_file("iidm_viewer/app.py")
    at.session_state["nad_depth"] = 7
    at.run(timeout=30)
    assert at.session_state["nad_depth"] == 7


def test_load_network_returns_proxy(xiidm_upload):
    from iidm_viewer.powsybl_worker import NetworkProxy

    net = load_network(xiidm_upload)
    assert isinstance(net, NetworkProxy)


def test_get_network_returns_none_without_upload():
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    assert at.session_state["network"] is None


def test_filter_voltage_levels_no_matches_returns_empty():
    df = _vls_df([{"id": "VL1", "name": "Alpha"}])
    assert filter_voltage_levels(df, "ZZZ").empty


def test_get_voltage_levels_df_display_prefers_name_over_id(xiidm_upload):
    """If a VL has a non-empty name, display is the name; otherwise the id."""
    net = load_network(xiidm_upload)
    df = get_voltage_levels_df(net)
    for _, row in df.iterrows():
        expected = row["name"] if row["name"] else row["id"]
        assert row["display"] == expected


# --- Component creation (feeder-bay) ---


def test_creatable_components_has_generator_entry():
    assert "Generators" in CREATABLE_COMPONENTS
    spec = CREATABLE_COMPONENTS["Generators"]
    assert spec["bay_function"] == "create_generator_bay"
    assert "id" in spec["required"]
    assert "bus_or_busbar_section_id" in spec["required"]


def test_list_node_breaker_voltage_levels_ieee14_is_empty(xiidm_upload):
    """IEEE14 is bus-breaker so the helper must return an empty frame."""
    net = load_network(xiidm_upload)
    df = list_node_breaker_voltage_levels(net)
    assert df.empty


def test_list_node_breaker_voltage_levels_returns_nb_vls(node_breaker_network):
    df = list_node_breaker_voltage_levels(node_breaker_network)
    assert not df.empty
    assert {"id", "display", "nominal_v"}.issubset(df.columns)
    assert "S1VL1" in set(df["id"])


def test_list_busbar_sections_filters_by_vl(node_breaker_network):
    bbs = list_busbar_sections(node_breaker_network, "S1VL2")
    assert bbs == ["S1VL2_BBS1", "S1VL2_BBS2"]


def test_list_busbar_sections_empty_for_unknown_vl(node_breaker_network):
    assert list_busbar_sections(node_breaker_network, "NOPE") == []


def test_create_component_bay_creates_generator_with_switches(node_breaker_network):
    fields = {
        "id": "TEST_GEN",
        "bus_or_busbar_section_id": "S1VL1_BBS",
        "min_p": 0.0,
        "max_p": 100.0,
        "target_p": 50.0,
        "target_q": 0.0,
        "voltage_regulator_on": False,
        "energy_source": "HYDRO",
        "position_order": 100,
        "direction": "BOTTOM",
    }
    create_component_bay(node_breaker_network, "Generators", fields)

    gens = node_breaker_network.get_generators()
    assert "TEST_GEN" in gens.index
    row = gens.loc["TEST_GEN"]
    assert row["voltage_level_id"] == "S1VL1"
    assert row["target_p"] == 50.0
    assert row["energy_source"] == "HYDRO"

    # Feeder-bay: breaker + disconnector on the new generator
    switches = node_breaker_network.get_switches()
    owned = [sid for sid in switches.index if sid.startswith("TEST_GEN_")]
    kinds = set(switches.loc[owned, "kind"])
    assert "BREAKER" in kinds
    assert "DISCONNECTOR" in kinds


def test_create_component_bay_rejects_unknown_component(node_breaker_network):
    with pytest.raises(ValueError, match="not creatable"):
        create_component_bay(node_breaker_network, "Loads", {"id": "L1"})


def test_create_component_bay_rejects_missing_required(node_breaker_network):
    with pytest.raises(ValueError, match="Missing required"):
        create_component_bay(
            node_breaker_network,
            "Generators",
            {"id": "X", "min_p": 0.0, "max_p": 100.0},
        )


def test_create_component_bay_surfaces_pypowsybl_errors(node_breaker_network):
    """An unknown busbar section id should propagate pypowsybl's error."""
    fields = {
        "id": "NO_BBS_GEN",
        "bus_or_busbar_section_id": "DOES_NOT_EXIST",
        "min_p": 0.0,
        "max_p": 100.0,
        "target_p": 50.0,
        "voltage_regulator_on": False,
        "position_order": 10,
    }
    with pytest.raises(Exception, match="not found"):
        create_component_bay(node_breaker_network, "Generators", fields)
