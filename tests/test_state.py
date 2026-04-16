"""Tests for session state, dataframe helpers, and filter logic."""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from iidm_viewer.state import (
    CREATABLE_BRANCHES,
    CREATABLE_COMPONENTS,
    create_branch_bay,
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


def test_creatable_components_has_expected_injection_types():
    expected = {
        "Generators": "create_generator_bay",
        "Loads": "create_load_bay",
        "Batteries": "create_battery_bay",
        "Static VAR Compensators": "create_static_var_compensator_bay",
        "VSC Converter Stations": "create_vsc_converter_station_bay",
        "LCC Converter Stations": "create_lcc_converter_station_bay",
    }
    for label, bay_fn in expected.items():
        assert label in CREATABLE_COMPONENTS, label
        spec = CREATABLE_COMPONENTS[label]
        assert spec["bay_function"] == bay_fn
        field_names = {f["name"] for f in spec["fields"]}
        assert "id" in field_names


def test_creatable_components_every_spec_has_required_id_field():
    for label, spec in CREATABLE_COMPONENTS.items():
        id_field = next(
            (f for f in spec["fields"] if f["name"] == "id"), None
        )
        assert id_field is not None, f"{label} missing id field"
        assert id_field["required"], f"{label} id must be required"


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
        create_component_bay(node_breaker_network, "Lines", {"id": "L1"})


def test_create_component_bay_rejects_missing_required(node_breaker_network):
    # Missing target_p + position_order + busbar → validation must reject
    with pytest.raises(ValueError, match="required"):
        create_component_bay(
            node_breaker_network,
            "Generators",
            {"id": "X", "min_p": 0.0, "max_p": 100.0},
        )


def test_create_component_bay_validates_minmax_p(node_breaker_network):
    with pytest.raises(ValueError, match="max_p must be"):
        create_component_bay(
            node_breaker_network,
            "Generators",
            {
                "id": "BADGEN",
                "bus_or_busbar_section_id": "S1VL1_BBS",
                "min_p": 100.0,
                "max_p": 50.0,
                "target_p": 75.0,
                "voltage_regulator_on": False,
                "position_order": 50,
            },
        )


def test_create_component_bay_validates_voltage_regulator(node_breaker_network):
    with pytest.raises(ValueError, match="target_v must be"):
        create_component_bay(
            node_breaker_network,
            "Generators",
            {
                "id": "REGGEN",
                "bus_or_busbar_section_id": "S1VL1_BBS",
                "min_p": 0.0,
                "max_p": 100.0,
                "target_p": 50.0,
                "voltage_regulator_on": True,
                "target_v": 0.0,
                "position_order": 50,
            },
        )


def test_create_component_bay_creates_load(node_breaker_network):
    create_component_bay(
        node_breaker_network,
        "Loads",
        {
            "id": "TEST_LOAD",
            "bus_or_busbar_section_id": "S1VL1_BBS",
            "p0": 12.5,
            "q0": 3.0,
            "type": "UNDEFINED",
            "position_order": 200,
            "direction": "BOTTOM",
        },
    )
    loads = node_breaker_network.get_loads()
    assert "TEST_LOAD" in loads.index
    assert loads.loc["TEST_LOAD", "p0"] == 12.5
    assert loads.loc["TEST_LOAD", "voltage_level_id"] == "S1VL1"


def test_create_component_bay_creates_battery(node_breaker_network):
    create_component_bay(
        node_breaker_network,
        "Batteries",
        {
            "id": "TEST_BAT",
            "bus_or_busbar_section_id": "S1VL1_BBS",
            "min_p": 0.0,
            "max_p": 50.0,
            "target_p": 10.0,
            "target_q": 0.0,
            "position_order": 210,
            "direction": "BOTTOM",
        },
    )
    bats = node_breaker_network.get_batteries()
    assert "TEST_BAT" in bats.index
    assert bats.loc["TEST_BAT", "max_p"] == 50.0


def test_create_component_bay_creates_svc(node_breaker_network):
    create_component_bay(
        node_breaker_network,
        "Static VAR Compensators",
        {
            "id": "TEST_SVC",
            "bus_or_busbar_section_id": "S1VL1_BBS",
            "b_min": -0.01,
            "b_max": 0.01,
            "regulation_mode": "VOLTAGE",
            "regulating": True,
            "target_v": 225.0,
            "position_order": 220,
            "direction": "BOTTOM",
        },
    )
    svcs = node_breaker_network.get_static_var_compensators()
    assert "TEST_SVC" in svcs.index
    assert svcs.loc["TEST_SVC", "regulation_mode"] == "VOLTAGE"


def test_create_component_bay_creates_vsc_station(node_breaker_network):
    create_component_bay(
        node_breaker_network,
        "VSC Converter Stations",
        {
            "id": "TEST_VSC",
            "bus_or_busbar_section_id": "S1VL1_BBS",
            "loss_factor": 1.0,
            "voltage_regulator_on": False,
            "target_q": 5.0,
            "position_order": 230,
            "direction": "BOTTOM",
        },
    )
    vsc = node_breaker_network.get_vsc_converter_stations()
    assert "TEST_VSC" in vsc.index


def test_create_component_bay_creates_lcc_station(node_breaker_network):
    create_component_bay(
        node_breaker_network,
        "LCC Converter Stations",
        {
            "id": "TEST_LCC",
            "bus_or_busbar_section_id": "S1VL1_BBS",
            "power_factor": 0.85,
            "loss_factor": 1.0,
            "position_order": 240,
            "direction": "BOTTOM",
        },
    )
    lcc = node_breaker_network.get_lcc_converter_stations()
    assert "TEST_LCC" in lcc.index
    assert lcc.loc["TEST_LCC", "power_factor"] == pytest.approx(0.85)


def test_create_component_bay_svc_voltage_mode_needs_target_v(node_breaker_network):
    with pytest.raises(ValueError, match="target_v must be"):
        create_component_bay(
            node_breaker_network,
            "Static VAR Compensators",
            {
                "id": "BAD_SVC",
                "bus_or_busbar_section_id": "S1VL1_BBS",
                "b_min": -0.01,
                "b_max": 0.01,
                "regulation_mode": "VOLTAGE",
                "regulating": True,
                "target_v": 0.0,
                "position_order": 250,
            },
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


# --- Branch creation (lines + 2-winding transformers) ---


def test_creatable_branches_has_lines_and_2wt():
    assert "Lines" in CREATABLE_BRANCHES
    assert CREATABLE_BRANCHES["Lines"]["bay_function"] == "create_line_bays"
    assert "2-Winding Transformers" in CREATABLE_BRANCHES
    assert (
        CREATABLE_BRANCHES["2-Winding Transformers"]["bay_function"]
        == "create_2_windings_transformer_bays"
    )


def test_creatable_branches_same_substation_flag():
    assert CREATABLE_BRANCHES["Lines"]["same_substation"] is False
    assert CREATABLE_BRANCHES["2-Winding Transformers"]["same_substation"] is True


def _base_line_fields():
    return {
        "id": "NEW_LINE",
        "r": 0.1, "x": 1.0,
        "g1": 0.0, "b1": 0.0, "g2": 0.0, "b2": 0.0,
        "bus_or_busbar_section_id_1": "S2VL1_BBS",
        "position_order_1": 100, "direction_1": "TOP",
        "bus_or_busbar_section_id_2": "S3VL1_BBS",
        "position_order_2": 100, "direction_2": "TOP",
    }


def _base_2wt_fields():
    return {
        "id": "NEW_2WT",
        "r": 0.5, "x": 10.0, "g": 0.0, "b": 1e-6,
        "rated_u1": 400.0, "rated_u2": 225.0,
        "bus_or_busbar_section_id_1": "S1VL2_BBS1",
        "position_order_1": 100, "direction_1": "TOP",
        "bus_or_busbar_section_id_2": "S1VL1_BBS",
        "position_order_2": 100, "direction_2": "BOTTOM",
    }


def test_create_branch_bay_creates_line(node_breaker_network):
    create_branch_bay(node_breaker_network, "Lines", _base_line_fields())
    lines = node_breaker_network.get_lines()
    assert "NEW_LINE" in lines.index
    row = lines.loc["NEW_LINE"]
    assert row["voltage_level1_id"] == "S2VL1"
    assert row["voltage_level2_id"] == "S3VL1"


def test_create_branch_bay_creates_2wt(node_breaker_network):
    create_branch_bay(
        node_breaker_network, "2-Winding Transformers", _base_2wt_fields()
    )
    tr = node_breaker_network.get_2_windings_transformers()
    assert "NEW_2WT" in tr.index
    row = tr.loc["NEW_2WT"]
    assert row["voltage_level1_id"] == "S1VL2"
    assert row["voltage_level2_id"] == "S1VL1"
    assert row["rated_u1"] == 400.0
    assert row["rated_u2"] == 225.0


def test_create_branch_bay_rejects_2wt_across_substations(node_breaker_network):
    """A 2WT can only connect two VLs of the same substation. S1VL2 and S2VL1
    live in different substations so pypowsybl must refuse — we surface it as
    a friendly error BEFORE dispatch.
    """
    fields = _base_2wt_fields()
    fields["bus_or_busbar_section_id_2"] = "S2VL1_BBS"
    with pytest.raises(ValueError, match="same substation"):
        create_branch_bay(
            node_breaker_network, "2-Winding Transformers", fields
        )


def test_create_branch_bay_rejects_missing_side_locator(node_breaker_network):
    fields = _base_line_fields()
    fields.pop("position_order_2")
    with pytest.raises(ValueError, match="Position order 2 is required"):
        create_branch_bay(node_breaker_network, "Lines", fields)


def test_create_branch_bay_rejects_missing_busbar(node_breaker_network):
    fields = _base_line_fields()
    fields["bus_or_busbar_section_id_1"] = ""
    with pytest.raises(ValueError, match="Busbar section 1"):
        create_branch_bay(node_breaker_network, "Lines", fields)


def test_create_branch_bay_rejects_unknown_component(node_breaker_network):
    with pytest.raises(ValueError, match="not a creatable branch"):
        create_branch_bay(node_breaker_network, "Generators", {})


def test_create_branch_bay_surfaces_pypowsybl_errors(node_breaker_network):
    fields = _base_line_fields()
    fields["bus_or_busbar_section_id_2"] = "DOES_NOT_EXIST"
    with pytest.raises(Exception, match="not found"):
        create_branch_bay(node_breaker_network, "Lines", fields)
