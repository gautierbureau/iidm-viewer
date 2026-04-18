"""Tests for session state, dataframe helpers, and filter logic."""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from iidm_viewer.state import (
    CREATABLE_BRANCHES,
    CREATABLE_COMPONENTS,
    CREATABLE_CONTAINERS,
    CREATABLE_EXTENSIONS,
    CREATABLE_HVDC_LINES,
    CREATABLE_TAP_CHANGERS,
    OPERATIONAL_LIMITS_TARGETS,
    REACTIVE_LIMITS_TARGETS,
    create_branch_bay,
    create_component_bay,
    create_container,
    create_coupling_device,
    create_empty_network,
    create_extension,
    create_hvdc_line,
    create_operational_limits,
    create_reactive_limits,
    create_tap_changer,
    filter_voltage_levels,
    get_voltage_levels_df,
    list_busbar_sections,
    list_converter_stations,
    list_extension_candidates,
    list_extensions_for_component,
    list_node_breaker_voltage_levels,
    list_operational_limit_candidates,
    list_reactive_limit_candidates,
    list_substations_df,
    list_two_winding_transformers,
    load_network,
    next_free_node,
    validate_create_extension_fields,
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


# --- Blank network bootstrap ---


def test_create_empty_network_returns_proxy_and_installs_session():
    from iidm_viewer.powsybl_worker import NetworkProxy
    import streamlit as st

    st.session_state.clear()
    net = create_empty_network("test_blank")
    assert isinstance(net, NetworkProxy)
    assert st.session_state["network"] is net
    assert st.session_state["selected_vl"] is None
    assert net.id == "test_blank"
    assert net.get_voltage_levels().empty
    assert net.get_substations().empty


def test_create_empty_network_supports_full_container_bootstrap():
    """A fresh blank network must accept the full substation / VL / BBS chain."""
    net = create_empty_network("bootstrap")
    create_container(net, "Substations", {"id": "S1", "country": "FR"})
    create_container(
        net,
        "Voltage Levels",
        {
            "id": "VL1",
            "substation_id": "S1",
            "topology_kind": "NODE_BREAKER",
            "nominal_v": 225.0,
        },
    )
    create_container(
        net,
        "Busbar Sections",
        {"id": "BBS1", "voltage_level_id": "VL1", "node": 0},
    )
    assert "S1" in net.get_substations().index
    assert "VL1" in net.get_voltage_levels().index
    assert "BBS1" in net.get_busbar_sections().index


def test_create_empty_network_defaults_blank_id_to_network():
    net = create_empty_network("")
    assert net.id == "network"


# --- Container creation (substations / voltage levels / busbar sections) ---


def test_creatable_containers_has_expected_types():
    expected = {
        "Substations": "create_substations",
        "Voltage Levels": "create_voltage_levels",
        "Busbar Sections": "create_busbar_sections",
    }
    for label, fn in expected.items():
        assert label in CREATABLE_CONTAINERS, label
        assert CREATABLE_CONTAINERS[label]["create_function"] == fn
        field_names = {f["name"] for f in CREATABLE_CONTAINERS[label]["fields"]}
        assert "id" in field_names


def test_list_substations_df_returns_sorted_by_display(node_breaker_network):
    df = list_substations_df(node_breaker_network)
    assert not df.empty
    assert {"id", "display"}.issubset(df.columns)
    assert list(df["display"]) == sorted(df["display"])


def test_next_free_node_suggests_unused_index(node_breaker_network):
    n = next_free_node(node_breaker_network, "S1VL1")
    bbs = node_breaker_network.get_busbar_sections(all_attributes=True)
    used_bbs = set(bbs[bbs["voltage_level_id"] == "S1VL1"]["node"].tolist())
    assert n not in used_bbs


def test_next_free_node_returns_zero_for_empty_vl(node_breaker_network):
    node_breaker_network.get_substations()  # warm up
    create_container(
        node_breaker_network,
        "Substations",
        {"id": "TEST_SUB_NEXT", "country": "FR"},
    )
    create_container(
        node_breaker_network,
        "Voltage Levels",
        {
            "id": "TEST_VL_EMPTY",
            "substation_id": "TEST_SUB_NEXT",
            "topology_kind": "NODE_BREAKER",
            "nominal_v": 225.0,
        },
    )
    assert next_free_node(node_breaker_network, "TEST_VL_EMPTY") == 0


def test_create_container_substation(node_breaker_network):
    create_container(
        node_breaker_network,
        "Substations",
        {"id": "TEST_SUB", "name": "My Sub", "country": "FR", "TSO": "RTE"},
    )
    subs = node_breaker_network.get_substations()
    assert "TEST_SUB" in subs.index
    row = subs.loc["TEST_SUB"]
    assert row["country"] == "FR"
    assert row["TSO"] == "RTE"


def test_create_container_voltage_level_attached_to_substation(node_breaker_network):
    create_container(
        node_breaker_network,
        "Substations",
        {"id": "TEST_SUB2", "country": "DE"},
    )
    create_container(
        node_breaker_network,
        "Voltage Levels",
        {
            "id": "TEST_VL",
            "substation_id": "TEST_SUB2",
            "topology_kind": "NODE_BREAKER",
            "nominal_v": 380.0,
            "low_voltage_limit": 360.0,
            "high_voltage_limit": 420.0,
        },
    )
    vls = node_breaker_network.get_voltage_levels()
    assert "TEST_VL" in vls.index
    assert vls.loc["TEST_VL", "nominal_v"] == 380.0
    assert vls.loc["TEST_VL", "substation_id"] == "TEST_SUB2"


def test_create_container_voltage_level_drops_zero_limits(node_breaker_network):
    """0 is the UI sentinel for 'unset' — it must not be sent as a real limit."""
    create_container(
        node_breaker_network,
        "Substations",
        {"id": "TEST_SUB_ZERO", "country": "FR"},
    )
    create_container(
        node_breaker_network,
        "Voltage Levels",
        {
            "id": "TEST_VL_ZERO",
            "substation_id": "TEST_SUB_ZERO",
            "topology_kind": "NODE_BREAKER",
            "nominal_v": 225.0,
            "low_voltage_limit": 0.0,
            "high_voltage_limit": 0.0,
        },
    )
    vls = node_breaker_network.get_voltage_levels(all_attributes=True)
    row = vls.loc["TEST_VL_ZERO"]
    # pypowsybl reports unset limits as NaN, not 0.
    assert pd.isna(row.get("low_voltage_limit"))
    assert pd.isna(row.get("high_voltage_limit"))


def test_create_container_busbar_section(node_breaker_network):
    free_node = next_free_node(node_breaker_network, "S1VL1")
    create_container(
        node_breaker_network,
        "Busbar Sections",
        {
            "id": "TEST_BBS",
            "voltage_level_id": "S1VL1",
            "node": free_node,
        },
    )
    bbs = node_breaker_network.get_busbar_sections()
    assert "TEST_BBS" in bbs.index
    assert bbs.loc["TEST_BBS", "voltage_level_id"] == "S1VL1"


def test_create_container_rejects_unknown_type(node_breaker_network):
    with pytest.raises(ValueError, match="not a creatable container"):
        create_container(node_breaker_network, "Generators", {"id": "x"})


def test_create_container_rejects_missing_required(node_breaker_network):
    with pytest.raises(ValueError, match="required"):
        create_container(
            node_breaker_network,
            "Voltage Levels",
            {"id": "VL_MISSING_NOMINAL", "topology_kind": "NODE_BREAKER"},
        )


def test_create_container_busbar_requires_voltage_level(node_breaker_network):
    with pytest.raises(ValueError, match="Voltage level"):
        create_container(
            node_breaker_network,
            "Busbar Sections",
            {"id": "BBS_NO_VL", "node": 0},
        )


def test_create_container_voltage_level_rejects_inverted_limits(node_breaker_network):
    with pytest.raises(ValueError, match="high_voltage_limit"):
        create_container(
            node_breaker_network,
            "Voltage Levels",
            {
                "id": "VL_BAD_LIMITS",
                "topology_kind": "NODE_BREAKER",
                "nominal_v": 225.0,
                "low_voltage_limit": 250.0,
                "high_voltage_limit": 200.0,
            },
        )


def test_create_container_surfaces_pypowsybl_errors(node_breaker_network):
    """Creating a busbar section on a non-existent VL must raise."""
    with pytest.raises(Exception):
        create_container(
            node_breaker_network,
            "Busbar Sections",
            {
                "id": "BBS_BAD_VL",
                "voltage_level_id": "DOES_NOT_EXIST",
                "node": 0,
            },
        )


# --- Shunt compensator creation ---


def _base_shunt_fields(shunt_id="TEST_SHUNT"):
    return {
        "id": shunt_id,
        "bus_or_busbar_section_id": "S1VL1_BBS",
        "section_count": 1,
        "max_section_count": 1,
        "g_per_section": 0.0,
        "b_per_section": 1e-5,
        "target_v": 0.0,
        "target_deadband": 0.0,
        "position_order": 300,
        "direction": "BOTTOM",
    }


def test_creatable_components_has_shunt_compensators():
    assert "Shunt Compensators" in CREATABLE_COMPONENTS
    spec = CREATABLE_COMPONENTS["Shunt Compensators"]
    assert spec["bay_function"] == "create_shunt_compensator_bay"
    field_names = {f["name"] for f in spec["fields"]}
    assert {
        "id", "section_count", "max_section_count",
        "g_per_section", "b_per_section",
    }.issubset(field_names)


def test_create_component_bay_creates_shunt_compensator(node_breaker_network):
    create_component_bay(
        node_breaker_network, "Shunt Compensators", _base_shunt_fields()
    )
    shunts = node_breaker_network.get_shunt_compensators()
    assert "TEST_SHUNT" in shunts.index
    row = shunts.loc["TEST_SHUNT"]
    assert row["voltage_level_id"] == "S1VL1"
    assert row["max_section_count"] == 1


def test_create_component_bay_shunt_rejects_oversized_initial_section(node_breaker_network):
    fields = _base_shunt_fields(shunt_id="BAD_SHUNT")
    fields["section_count"] = 5
    fields["max_section_count"] = 2
    with pytest.raises(ValueError, match="<= max_section_count"):
        create_component_bay(
            node_breaker_network, "Shunt Compensators", fields
        )


# --- Tap changer creation ---


def _fresh_2wt_no_tapchangers(network, twt_id="NEW_2WT_TC"):
    """Create a 2WT inside S1 so tap-changer tests can attach changers to it."""
    create_branch_bay(
        network,
        "2-Winding Transformers",
        {
            "id": twt_id,
            "r": 0.5, "x": 10.0, "g": 0.0, "b": 1e-6,
            "rated_u1": 400.0, "rated_u2": 225.0,
            "bus_or_busbar_section_id_1": "S1VL2_BBS1",
            "position_order_1": 400, "direction_1": "TOP",
            "bus_or_busbar_section_id_2": "S1VL1_BBS",
            "position_order_2": 400, "direction_2": "BOTTOM",
        },
    )
    return twt_id


def test_creatable_tap_changers_has_ratio_and_phase():
    assert set(CREATABLE_TAP_CHANGERS.keys()) == {"Ratio", "Phase"}
    assert (
        CREATABLE_TAP_CHANGERS["Ratio"]["create_method"]
        == "create_ratio_tap_changers"
    )
    assert (
        CREATABLE_TAP_CHANGERS["Phase"]["create_method"]
        == "create_phase_tap_changers"
    )


def test_list_two_winding_transformers_returns_ids(node_breaker_network):
    ids = list_two_winding_transformers(node_breaker_network)
    assert ids == sorted(ids)
    assert "TWT" in ids


def test_create_ratio_tap_changer(node_breaker_network):
    twt_id = _fresh_2wt_no_tapchangers(node_breaker_network)
    steps = [
        {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 0.95},
        {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.00},
        {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.05},
    ]
    create_tap_changer(
        node_breaker_network,
        "Ratio",
        twt_id,
        {
            "tap": 1,
            "low_tap": 0,
            "oltc": True,
            "regulating": True,
            "target_v": 225.0,
            "target_deadband": 2.0,
            "regulated_side": "ONE",
        },
        steps,
    )
    rtc = node_breaker_network.get_ratio_tap_changers()
    assert twt_id in rtc.index
    assert rtc.loc[twt_id, "tap"] == 1


def test_create_phase_tap_changer(node_breaker_network):
    twt_id = _fresh_2wt_no_tapchangers(node_breaker_network, "NEW_2WT_PTC")
    steps = [
        {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.0, "alpha": -2.0},
        {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.0, "alpha": 0.0},
        {"r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0, "rho": 1.0, "alpha": 2.0},
    ]
    create_tap_changer(
        node_breaker_network,
        "Phase",
        twt_id,
        {
            "tap": 1,
            "low_tap": 0,
            "regulation_mode": "CURRENT_LIMITER",
            "regulating": False,
            "target_deadband": 0.0,
        },
        steps,
    )
    ptc = node_breaker_network.get_phase_tap_changers()
    assert twt_id in ptc.index
    assert ptc.loc[twt_id, "tap"] == 1


def test_create_tap_changer_rejects_unknown_kind(node_breaker_network):
    with pytest.raises(ValueError, match="not creatable"):
        create_tap_changer(
            node_breaker_network, "Linear", "TWT", {"tap": 0, "low_tap": 0}, [{}]
        )


def test_create_tap_changer_rejects_empty_steps(node_breaker_network):
    twt_id = _fresh_2wt_no_tapchangers(node_breaker_network, "NEW_2WT_EMPTY")
    with pytest.raises(ValueError, match="At least one tap step"):
        create_tap_changer(
            node_breaker_network,
            "Ratio",
            twt_id,
            {"tap": 0, "low_tap": 0, "oltc": False, "regulating": False},
            [],
        )


def test_create_tap_changer_rejects_tap_out_of_range(node_breaker_network):
    twt_id = _fresh_2wt_no_tapchangers(node_breaker_network, "NEW_2WT_OOR")
    with pytest.raises(ValueError, match="Current tap"):
        create_tap_changer(
            node_breaker_network,
            "Ratio",
            twt_id,
            {"tap": 5, "low_tap": 0, "oltc": False, "regulating": False},
            [{"rho": 1.0}, {"rho": 1.0}],
        )


def test_create_tap_changer_ratio_regulating_needs_oltc(node_breaker_network):
    twt_id = _fresh_2wt_no_tapchangers(node_breaker_network, "NEW_2WT_NO_OLTC")
    with pytest.raises(ValueError, match="OLTC must be enabled"):
        create_tap_changer(
            node_breaker_network,
            "Ratio",
            twt_id,
            {
                "tap": 0, "low_tap": 0, "oltc": False, "regulating": True,
                "target_v": 225.0,
            },
            [{"rho": 1.0}],
        )


# --- Coupling device ---


def test_create_coupling_device_between_two_busbar_sections(node_breaker_network):
    before = set(node_breaker_network.get_switches().index)
    create_coupling_device(
        node_breaker_network, "S1VL2_BBS1", "S1VL2_BBS2", switch_prefix="cpl"
    )
    after = set(node_breaker_network.get_switches().index)
    # pypowsybl creates at least one new switch (a breaker) linking them.
    assert after - before, "expected new switches from coupling device"


def test_create_coupling_device_rejects_different_voltage_levels(node_breaker_network):
    with pytest.raises(ValueError, match="same voltage level"):
        create_coupling_device(
            node_breaker_network, "S1VL1_BBS", "S2VL1_BBS"
        )


def test_create_coupling_device_rejects_same_busbar_section(node_breaker_network):
    with pytest.raises(ValueError, match="must differ"):
        create_coupling_device(
            node_breaker_network, "S1VL2_BBS1", "S1VL2_BBS1"
        )


def test_create_coupling_device_rejects_unknown_busbar_section(node_breaker_network):
    with pytest.raises(ValueError, match="Unknown busbar section"):
        create_coupling_device(
            node_breaker_network, "S1VL2_BBS1", "DOES_NOT_EXIST"
        )


# --- HVDC lines ---


def _fresh_vsc_pair(network):
    """Create two standalone VSC stations so an HVDC line can connect them."""
    create_component_bay(
        network,
        "VSC Converter Stations",
        {
            "id": "NEW_VSC_A",
            "bus_or_busbar_section_id": "S3VL1_BBS",
            "loss_factor": 1.0,
            "voltage_regulator_on": False,
            "target_q": 5.0,
            "position_order": 500, "direction": "TOP",
        },
    )
    create_component_bay(
        network,
        "VSC Converter Stations",
        {
            "id": "NEW_VSC_B",
            "bus_or_busbar_section_id": "S4VL1_BBS",
            "loss_factor": 1.0,
            "voltage_regulator_on": False,
            "target_q": 5.0,
            "position_order": 510, "direction": "TOP",
        },
    )
    return "NEW_VSC_A", "NEW_VSC_B"


def test_creatable_hvdc_lines_registry_has_expected_fields():
    spec = CREATABLE_HVDC_LINES
    assert spec["create_function"] == "create_hvdc_lines"
    names = {f["name"] for f in spec["fields"]}
    assert {
        "id", "r", "nominal_v", "max_p", "target_p", "converters_mode",
    }.issubset(names)


def test_list_converter_stations_returns_vsc_and_lcc(node_breaker_network):
    stations = list_converter_stations(node_breaker_network)
    kinds = {kind for _, kind in stations}
    assert {"VSC", "LCC"}.issubset(kinds)
    ids = {sid for sid, _ in stations}
    assert {"VSC1", "VSC2", "LCC1", "LCC2"}.issubset(ids)


def test_create_hvdc_line_between_two_fresh_stations(node_breaker_network):
    a, b = _fresh_vsc_pair(node_breaker_network)
    create_hvdc_line(
        node_breaker_network,
        {
            "id": "NEW_HVDC",
            "r": 1.0,
            "nominal_v": 400.0,
            "max_p": 1000.0,
            "target_p": 500.0,
            "converters_mode": "SIDE_1_RECTIFIER_SIDE_2_INVERTER",
            "converter_station1_id": a,
            "converter_station2_id": b,
        },
    )
    hvdc = node_breaker_network.get_hvdc_lines()
    assert "NEW_HVDC" in hvdc.index
    row = hvdc.loc["NEW_HVDC"]
    assert row["converter_station1_id"] == a
    assert row["converter_station2_id"] == b
    assert row["max_p"] == 1000.0


def test_create_hvdc_line_rejects_same_station(node_breaker_network):
    with pytest.raises(ValueError, match="must differ"):
        create_hvdc_line(
            node_breaker_network,
            {
                "id": "BAD_HVDC",
                "r": 1.0, "nominal_v": 400.0,
                "max_p": 1000.0, "target_p": 0.0,
                "converters_mode": "SIDE_1_RECTIFIER_SIDE_2_INVERTER",
                "converter_station1_id": "VSC1",
                "converter_station2_id": "VSC1",
            },
        )


def test_create_hvdc_line_rejects_missing_station(node_breaker_network):
    with pytest.raises(ValueError, match="Converter station 2"):
        create_hvdc_line(
            node_breaker_network,
            {
                "id": "BAD_HVDC2",
                "r": 1.0, "nominal_v": 400.0,
                "max_p": 1000.0, "target_p": 0.0,
                "converters_mode": "SIDE_1_RECTIFIER_SIDE_2_INVERTER",
                "converter_station1_id": "VSC1",
            },
        )


def test_create_hvdc_line_rejects_target_p_gt_max_p(node_breaker_network):
    with pytest.raises(ValueError, match="<= max_p"):
        create_hvdc_line(
            node_breaker_network,
            {
                "id": "BAD_HVDC3",
                "r": 1.0, "nominal_v": 400.0,
                "max_p": 500.0, "target_p": 1000.0,
                "converters_mode": "SIDE_1_RECTIFIER_SIDE_2_INVERTER",
                "converter_station1_id": "VSC1",
                "converter_station2_id": "LCC1",
            },
        )


# --- Reactive limits ---


def test_reactive_limits_targets_has_expected_components():
    assert set(REACTIVE_LIMITS_TARGETS.keys()) == {
        "Generators", "Batteries", "VSC Converter Stations",
    }


def test_list_reactive_limit_candidates_for_generators(node_breaker_network):
    ids = list_reactive_limit_candidates(node_breaker_network, "Generators")
    assert "GH1" in ids


def test_create_reactive_limits_minmax_on_generator(node_breaker_network):
    create_reactive_limits(
        node_breaker_network, "GH1", "minmax",
        [{"min_q": -75.0, "max_q": 60.0}],
    )
    gen = node_breaker_network.get_generators(all_attributes=True).loc["GH1"]
    assert gen["min_q"] == -75.0
    assert gen["max_q"] == 60.0


def test_create_reactive_limits_curve_on_generator(node_breaker_network):
    create_reactive_limits(
        node_breaker_network, "GTH1", "curve",
        [
            {"p": 0.0, "min_q": -100.0, "max_q": 100.0},
            {"p": 200.0, "min_q": -50.0, "max_q": 50.0},
        ],
    )
    curve = node_breaker_network.get_reactive_capability_curve_points()
    assert "GTH1" in curve.index.get_level_values("id")
    rows = curve.loc["GTH1"]
    assert len(rows) == 2
    assert rows["p"].tolist() == [0.0, 200.0]


def test_create_reactive_limits_rejects_unknown_mode(node_breaker_network):
    with pytest.raises(ValueError, match="Unknown reactive-limits mode"):
        create_reactive_limits(
            node_breaker_network, "GH1", "banana",
            [{"min_q": -1.0, "max_q": 1.0}],
        )


def test_create_reactive_limits_minmax_rejects_inverted(node_breaker_network):
    with pytest.raises(ValueError, match="max_q must be"):
        create_reactive_limits(
            node_breaker_network, "GH1", "minmax",
            [{"min_q": 100.0, "max_q": -100.0}],
        )


def test_create_reactive_limits_curve_needs_two_distinct_p(node_breaker_network):
    with pytest.raises(ValueError, match="2 distinct p"):
        create_reactive_limits(
            node_breaker_network, "GH1", "curve",
            [
                {"p": 0.0, "min_q": -10.0, "max_q": 10.0},
                {"p": 0.0, "min_q": -5.0, "max_q": 5.0},
            ],
        )


def test_create_reactive_limits_rejects_empty_payload(node_breaker_network):
    with pytest.raises(ValueError, match="At least one row"):
        create_reactive_limits(node_breaker_network, "GH1", "minmax", [])


# --- Operational limits ---


def test_operational_limits_targets_has_expected_components():
    assert set(OPERATIONAL_LIMITS_TARGETS.keys()) == {
        "Lines", "2-Winding Transformers", "Dangling Lines",
    }


def test_list_operational_limit_candidates_for_lines(node_breaker_network):
    ids = list_operational_limit_candidates(node_breaker_network, "Lines")
    assert "LINE_S3S4" in ids


def test_create_operational_limits_replaces_default_group(node_breaker_network):
    """LINE_S3S4 starts with limits in DEFAULT — creating new ones replaces them."""
    create_operational_limits(
        node_breaker_network, "LINE_S3S4", "ONE", "CURRENT",
        [
            {"name": "permanent", "value": 700.0, "acceptable_duration": -1},
            {"name": "TATL_60", "value": 900.0, "acceptable_duration": 60},
        ],
    )
    ol = node_breaker_network.get_operational_limits(
        all_attributes=True
    ).reset_index()
    rows = ol[
        (ol["element_id"] == "LINE_S3S4")
        & (ol["side"] == "ONE")
        & (ol["group_name"] == "DEFAULT")
    ]
    durations = sorted(rows["acceptable_duration"].tolist())
    assert durations == [-1, 60]
    perm = rows[rows["acceptable_duration"] == -1].iloc[0]
    assert perm["value"] == 700.0


def test_create_operational_limits_rejects_zero_permanent_limits(node_breaker_network):
    with pytest.raises(ValueError, match="permanent"):
        create_operational_limits(
            node_breaker_network, "LINE_S3S4", "ONE", "CURRENT",
            [{"name": "TATL_60", "value": 900.0, "acceptable_duration": 60}],
        )


def test_create_operational_limits_rejects_multiple_permanent_limits(node_breaker_network):
    with pytest.raises(ValueError, match="permanent"):
        create_operational_limits(
            node_breaker_network, "LINE_S3S4", "ONE", "CURRENT",
            [
                {"name": "p1", "value": 900.0, "acceptable_duration": -1},
                {"name": "p2", "value": 700.0, "acceptable_duration": -1},
            ],
        )


def test_create_operational_limits_rejects_unknown_type(node_breaker_network):
    with pytest.raises(ValueError, match="Type must be one of"):
        create_operational_limits(
            node_breaker_network, "LINE_S3S4", "ONE", "BANANA",
            [{"name": "p", "value": 700.0, "acceptable_duration": -1}],
        )


def test_create_operational_limits_rejects_unknown_side(node_breaker_network):
    with pytest.raises(ValueError, match="Side must be one of"):
        create_operational_limits(
            node_breaker_network, "LINE_S3S4", "THREE", "CURRENT",
            [{"name": "p", "value": 700.0, "acceptable_duration": -1}],
        )


def test_create_operational_limits_rejects_negative_value(node_breaker_network):
    with pytest.raises(ValueError, match="non-negative"):
        create_operational_limits(
            node_breaker_network, "LINE_S3S4", "ONE", "CURRENT",
            [{"name": "p", "value": -1.0, "acceptable_duration": -1}],
        )


# --- Extensions (first-phase write support) ---


def test_creatable_extensions_registry_has_expected_entries():
    expected = {
        "substationPosition", "entsoeArea", "busbarSectionPosition", "position",
        "slackTerminal", "activePowerControl", "voltageRegulation",
        "voltagePerReactivePowerControl", "standbyAutomaton",
        "hvdcAngleDroopActivePowerControl", "hvdcOperatorActivePowerRange",
        "entsoeCategory",
    }
    assert expected <= set(CREATABLE_EXTENSIONS)
    for name, schema in CREATABLE_EXTENSIONS.items():
        assert "label" in schema and "index" in schema
        assert schema["targets"], f"{name} has empty targets"
        assert schema["fields"], f"{name} has no fields"


def test_list_extensions_for_component_includes_positions_for_injections():
    gen_exts = set(list_extensions_for_component("Generators"))
    assert {"activePowerControl", "entsoeCategory", "position"} <= gen_exts
    sub_exts = set(list_extensions_for_component("Substations"))
    assert {"substationPosition", "entsoeArea"} <= sub_exts


def test_list_extension_candidates_returns_ids(node_breaker_network):
    ids = list_extension_candidates(
        node_breaker_network, "substationPosition", "Substations"
    )
    assert "S1" in ids


def test_list_extension_candidates_unknown_component_returns_empty(node_breaker_network):
    assert list_extension_candidates(
        node_breaker_network, "substationPosition", "Loads"
    ) == []


def test_create_extension_substation_position(node_breaker_network):
    create_extension(
        node_breaker_network, "substationPosition", "S1",
        {"latitude": 48.85, "longitude": 2.35},
    )
    df = node_breaker_network.get_extensions("substationPosition")
    assert "S1" in df.index
    row = df.loc["S1"]
    assert abs(row["latitude"] - 48.85) < 1e-6
    assert abs(row["longitude"] - 2.35) < 1e-6


def test_create_extension_entsoe_area(node_breaker_network):
    create_extension(
        node_breaker_network, "entsoeArea", "S2", {"code": "FR"},
    )
    df = node_breaker_network.get_extensions("entsoeArea")
    assert df.loc["S2", "code"] == "FR"


def test_create_extension_active_power_control_on_generator(node_breaker_network):
    gen = node_breaker_network.get_generators().index[0]
    create_extension(
        node_breaker_network, "activePowerControl", gen,
        {"participate": True, "droop": 4.0, "participation_factor": 1.0,
         "min_target_p": None, "max_target_p": None},
    )
    df = node_breaker_network.get_extensions("activePowerControl")
    assert bool(df.loc[gen, "participate"]) is True
    assert abs(float(df.loc[gen, "droop"]) - 4.0) < 1e-6


def test_create_extension_slack_terminal_with_bus(node_breaker_network):
    bus = node_breaker_network.get_buses().index[0]
    vl = node_breaker_network.get_buses().loc[bus, "voltage_level_id"]
    create_extension(
        node_breaker_network, "slackTerminal", vl,
        {"bus_id": bus, "element_id": ""},
    )
    df = node_breaker_network.get_extensions("slackTerminal")
    assert vl in df.index


def test_create_extension_slack_terminal_requires_exactly_one_target(node_breaker_network):
    with pytest.raises(ValueError, match="Exactly one of bus_id or element_id"):
        create_extension(
            node_breaker_network, "slackTerminal", "S1VL1",
            {"bus_id": "", "element_id": ""},
        )
    with pytest.raises(ValueError, match="Exactly one of bus_id or element_id"):
        create_extension(
            node_breaker_network, "slackTerminal", "S1VL1",
            {"bus_id": "X", "element_id": "Y"},
        )


def test_create_extension_busbar_section_position(node_breaker_network):
    # NB: pypowsybl 1.14 has a quirk where writes to busbarSectionPosition
    # reflect the same int into both columns on read-back. We only assert
    # that the row is created.
    bbs = node_breaker_network.get_busbar_sections().index[0]
    create_extension(
        node_breaker_network, "busbarSectionPosition", bbs,
        {"busbar_index": 2, "section_index": 3},
    )
    df = node_breaker_network.get_extensions("busbarSectionPosition")
    assert bbs in df.index


def test_create_extension_hvdc_droop_control(node_breaker_network):
    hvdc = node_breaker_network.get_hvdc_lines().index[0]
    create_extension(
        node_breaker_network, "hvdcAngleDroopActivePowerControl", hvdc,
        {"droop": 0.5, "p0": 10.0, "enabled": True},
    )
    df = node_breaker_network.get_extensions("hvdcAngleDroopActivePowerControl")
    assert abs(float(df.loc[hvdc, "p0"]) - 10.0) < 1e-6


def test_create_extension_hvdc_operator_range(node_breaker_network):
    hvdc = node_breaker_network.get_hvdc_lines().index[0]
    create_extension(
        node_breaker_network, "hvdcOperatorActivePowerRange", hvdc,
        {"opr_from_cs1_to_cs2": 100.0, "opr_from_cs2_to_cs1": 80.0},
    )
    df = node_breaker_network.get_extensions("hvdcOperatorActivePowerRange")
    assert abs(float(df.loc[hvdc, "opr_from_cs1_to_cs2"]) - 100.0) < 1e-6


def test_create_extension_svc_slope_and_standby(node_breaker_network):
    svc = node_breaker_network.get_static_var_compensators().index[0]
    create_extension(
        node_breaker_network, "voltagePerReactivePowerControl", svc,
        {"slope": 0.02},
    )
    df = node_breaker_network.get_extensions("voltagePerReactivePowerControl")
    assert abs(float(df.loc[svc, "slope"]) - 0.02) < 1e-6

    create_extension(
        node_breaker_network, "standbyAutomaton", svc,
        {"standby": False, "b0": 0.0,
         "low_voltage_threshold": 390.0, "low_voltage_setpoint": 395.0,
         "high_voltage_threshold": 410.0, "high_voltage_setpoint": 405.0},
    )
    df = node_breaker_network.get_extensions("standbyAutomaton")
    assert bool(df.loc[svc, "standby"]) is False


def test_create_extension_position_on_generator(node_breaker_network):
    gen = node_breaker_network.get_generators().index[0]
    create_extension(
        node_breaker_network, "position", gen,
        {"order": 42, "feeder_name": "my_feeder",
         "direction": "BOTTOM", "side": ""},
    )
    df = node_breaker_network.get_extensions("position")
    row = df.loc[gen]
    if hasattr(row, "iloc"):
        # When a position already exists from the bundled sample, there may be
        # multiple rows; scan for the one we just inserted.
        rows = row if row.ndim == 2 else row.to_frame().T
        assert any(int(r["order"]) == 42 for _, r in rows.iterrows())
    else:
        assert int(row["order"]) == 42


def test_create_extension_entsoe_category_on_generator(node_breaker_network):
    gen = node_breaker_network.get_generators().index[0]
    create_extension(
        node_breaker_network, "entsoeCategory", gen, {"code": 3},
    )
    df = node_breaker_network.get_extensions("entsoeCategory")
    assert int(df.loc[gen, "code"]) == 3


def test_validate_create_extension_fields_flags_missing_required():
    errs = validate_create_extension_fields(
        "substationPosition", {"latitude": 48.85}
    )
    assert any("longitude" in e for e in errs)


def test_validate_create_extension_fields_active_power_bounds():
    errs = validate_create_extension_fields(
        "activePowerControl",
        {"participate": True, "droop": 1.0,
         "min_target_p": 10.0, "max_target_p": 5.0},
    )
    assert any("max_target_p" in e for e in errs)


def test_create_extension_unknown_name_raises():
    with pytest.raises(ValueError, match="Unknown extension"):
        create_extension(None, "no_such_ext", "id", {})


def test_create_extension_empty_target_raises(node_breaker_network):
    with pytest.raises(ValueError, match="Target id is required"):
        create_extension(
            node_breaker_network, "substationPosition", "",
            {"latitude": 0.0, "longitude": 0.0},
        )
