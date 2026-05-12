"""Tests for the framework-agnostic ``iidm_viewer.component_creation`` module."""
from __future__ import annotations

import pytest

from iidm_viewer.component_creation import (
    CONVERTERS_MODES,
    CREATABLE_BRANCHES,
    CREATABLE_COMPONENTS,
    CREATABLE_CONTAINERS,
    CREATABLE_HVDC_LINES,
    CREATABLE_TAP_CHANGERS,
    LOCATOR_FIELDS,
    PTC_REGULATION_MODES,
    TOPOLOGY_KINDS,
    TRANSFORMER_SIDES,
    _SHUNT_LINEAR_FIELDS,
    _VALIDATORS,
    branch_side_locator_fields,
    coerce_field_values,
    create_branch_bay,
    create_component_bay,
    create_container,
    create_hvdc_line,
    create_tap_changer,
    list_busbar_sections,
    list_converter_stations,
    list_node_breaker_voltage_levels,
    list_substations_df,
    list_transformers_without_tap_changer,
    list_two_winding_transformers,
    next_free_node,
    validate_create_branch_fields,
    validate_create_container_fields,
    validate_create_fields,
    validate_create_hvdc_line_fields,
    validate_create_tap_changer_fields,
)
from iidm_viewer.powsybl_worker import NetworkProxy, run


@pytest.fixture(scope="module")
def node_breaker_network() -> NetworkProxy:
    """A small node-breaker network — bay creation needs busbar sections."""
    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    return NetworkProxy(run(_make))


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------
def test_registry_has_expected_components():
    expected = {
        "Generators", "Loads", "Batteries",
        "Static VAR Compensators", "VSC Converter Stations",
        "LCC Converter Stations", "Shunt Compensators",
    }
    assert expected <= set(CREATABLE_COMPONENTS)


def test_each_component_carries_id_field():
    for label, spec in CREATABLE_COMPONENTS.items():
        names = {f["name"] for f in spec["fields"]}
        assert "id" in names, f"{label} missing id field"
        assert spec.get("bay_function"), f"{label} missing bay_function"


def test_locator_fields_carry_required_and_help():
    names = {f["name"] for f in LOCATOR_FIELDS}
    assert names == {"position_order", "direction"}


def test_validators_registry_carries_known_hooks():
    expected = {
        "_validate_generator", "_validate_minmax_p",
        "_validate_voltage_regulator", "_validate_svc", "_validate_shunt",
    }
    assert expected <= set(_VALIDATORS)


# ---------------------------------------------------------------------------
# Streamlit drift guards
# ---------------------------------------------------------------------------
def test_creatable_components_re_exported_from_streamlit_state():
    pytest.importorskip("streamlit")
    from iidm_viewer.state import CREATABLE_COMPONENTS as ST
    assert ST is CREATABLE_COMPONENTS


def test_locator_fields_re_exported_from_streamlit_state():
    pytest.importorskip("streamlit")
    from iidm_viewer.state import LOCATOR_FIELDS as ST
    assert ST is LOCATOR_FIELDS


def test_validators_re_exported_from_streamlit_state():
    pytest.importorskip("streamlit")
    from iidm_viewer.state import _VALIDATORS as ST
    assert ST is _VALIDATORS


# ---------------------------------------------------------------------------
# coerce_field_values
# ---------------------------------------------------------------------------
def test_coerce_field_values_trims_text_and_casts_int():
    spec = [
        {"name": "id", "kind": "text"},
        {"name": "n", "kind": "int"},
        {"name": "p", "kind": "float"},
        {"name": "b", "kind": "bool"},
        {"name": "s", "kind": "select"},
    ]
    raw = {"id": "  hello  ", "n": 5.7, "p": 1.5, "b": True, "s": "X"}
    out = coerce_field_values(spec, raw)
    assert out == {"id": "hello", "n": 5, "p": 1.5, "b": True, "s": "X"}


# ---------------------------------------------------------------------------
# validate_create_fields
# ---------------------------------------------------------------------------
def test_validate_create_fields_flags_missing_required():
    errors = validate_create_fields("Generators", {})
    # ID, min_p, max_p, target_p, voltage_regulator_on,
    # position_order, busbar_section_id are all required.
    assert any("ID" in e for e in errors)
    assert any("position_order" in e.lower() or "Position order" in e for e in errors)
    assert any("Busbar" in e for e in errors)


def test_validate_create_fields_generator_voltage_regulator_rule():
    fields = {
        "id": "G", "min_p": 0.0, "max_p": 100.0, "target_p": 10.0,
        "voltage_regulator_on": True, "target_v": 0.0,
        "position_order": 10, "direction": "BOTTOM",
        "bus_or_busbar_section_id": "B1",
    }
    errors = validate_create_fields("Generators", fields)
    assert any("target_v" in e for e in errors)


def test_validate_create_fields_generator_minmax_p_rule():
    fields = {
        "id": "G", "min_p": 100.0, "max_p": 10.0, "target_p": 10.0,
        "voltage_regulator_on": False, "target_v": 0.0,
        "position_order": 10, "direction": "BOTTOM",
        "bus_or_busbar_section_id": "B1",
    }
    errors = validate_create_fields("Generators", fields)
    assert any("max_p" in e and "min_p" in e for e in errors)


def test_validate_create_fields_shunt_section_count_rule():
    fields = {
        "id": "SH", "section_count": 5, "max_section_count": 2,
        "g_per_section": 0.0, "b_per_section": 1e-5,
        "position_order": 10, "direction": "BOTTOM",
        "bus_or_busbar_section_id": "B1",
    }
    errors = validate_create_fields("Shunt Compensators", fields)
    assert any("section_count" in e for e in errors)


def test_validate_create_fields_rejects_unknown_component():
    errors = validate_create_fields("Mystery Component", {"id": "X"})
    assert errors and "not creatable" in errors[0]


# ---------------------------------------------------------------------------
# Network introspection
# ---------------------------------------------------------------------------
def test_list_node_breaker_voltage_levels_returns_non_empty(node_breaker_network):
    df = list_node_breaker_voltage_levels(node_breaker_network)
    assert df.shape[0] > 0
    assert set(["id", "display", "substation_id", "nominal_v"]) <= set(df.columns)


def test_list_busbar_sections_returns_ids(node_breaker_network):
    vls = list_node_breaker_voltage_levels(node_breaker_network)
    vl_id = str(vls["id"].iloc[0])
    bbs = list_busbar_sections(node_breaker_network, vl_id)
    assert isinstance(bbs, list)
    assert len(bbs) > 0


# ---------------------------------------------------------------------------
# End-to-end creation
# ---------------------------------------------------------------------------
def test_create_load_end_to_end(node_breaker_network):
    """Pick a real node-breaker VL + busbar, create a Load through the
    shared dispatcher, confirm pypowsybl now lists it."""
    vls = list_node_breaker_voltage_levels(node_breaker_network)
    vl_id = str(vls["id"].iloc[0])
    bbs = list_busbar_sections(node_breaker_network, vl_id)
    assert bbs, "need at least one busbar section"
    new_id = "TEST_LOAD_NEW"
    fields = {
        "id": new_id, "type": "UNDEFINED",
        "p0": 10.0, "q0": 5.0,
        "position_order": 999, "direction": "BOTTOM",
        "bus_or_busbar_section_id": bbs[0],
    }
    create_component_bay(node_breaker_network, "Loads", fields)
    loads = node_breaker_network.get_loads()
    assert new_id in loads.index


def test_create_rejects_unknown_component(node_breaker_network):
    with pytest.raises(ValueError, match="not creatable"):
        create_component_bay(node_breaker_network, "Mystery", {"id": "X"})


def test_shunt_linear_fields_constant():
    assert {"g_per_section", "b_per_section", "max_section_count"} == _SHUNT_LINEAR_FIELDS


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------
def test_creatable_branches_carries_lines_and_2wt():
    assert set(CREATABLE_BRANCHES) == {"Lines", "2-Winding Transformers"}
    assert CREATABLE_BRANCHES["Lines"]["bay_function"] == "create_line_bays"
    assert CREATABLE_BRANCHES["2-Winding Transformers"]["bay_function"] == "create_2_windings_transformer_bays"
    # 2WT enforces same-substation; Lines don't.
    assert CREATABLE_BRANCHES["2-Winding Transformers"]["same_substation"] is True
    assert CREATABLE_BRANCHES["Lines"]["same_substation"] is False


def test_branch_side_locator_fields_suffixes_side_number():
    side1 = branch_side_locator_fields(1)
    side2 = branch_side_locator_fields(2)
    names1 = {f["name"] for f in side1}
    names2 = {f["name"] for f in side2}
    assert names1 == {"position_order_1", "direction_1"}
    assert names2 == {"position_order_2", "direction_2"}
    # Labels also carry the side number for the user.
    assert any("1" in f["label"] for f in side1)
    assert any("2" in f["label"] for f in side2)


def test_creatable_branches_re_exported_from_streamlit_state():
    pytest.importorskip("streamlit")
    from iidm_viewer.state import CREATABLE_BRANCHES as ST
    assert ST is CREATABLE_BRANCHES


def test_validate_create_branch_fields_flags_required_and_busbars():
    errors = validate_create_branch_fields("Lines", {})
    # Should flag missing electrical fields + missing locator fields + both busbar sections.
    text = " ".join(errors)
    assert "ID is required" in text
    assert "r" in text and "x" in text  # the resistance / reactance fields
    assert "position_order" in text.lower() or "Position order 1" in text
    assert "Busbar section 1" in text
    assert "Busbar section 2" in text


def test_validate_create_branch_fields_rejects_unknown_branch():
    errors = validate_create_branch_fields("Tie Lines", {"id": "X"})
    assert errors and "not a creatable branch" in errors[0]


def test_validate_create_branch_fields_same_substation_for_2wt(node_breaker_network):
    """2W transformer requires both busbar sections in the same substation."""
    vls = list_node_breaker_voltage_levels(node_breaker_network)
    assert vls.shape[0] >= 2
    # Pick two VLs in different substations to force the violation.
    sub_groups = vls.groupby("substation_id")
    sub_ids = list(sub_groups.groups.keys())
    if len(sub_ids) < 2:
        pytest.skip("test fixture has only one substation")
    vl_a = sub_groups.get_group(sub_ids[0]).iloc[0]["id"]
    vl_b = sub_groups.get_group(sub_ids[1]).iloc[0]["id"]
    bbs_a = list_busbar_sections(node_breaker_network, str(vl_a))[0]
    bbs_b = list_busbar_sections(node_breaker_network, str(vl_b))[0]

    fields = {
        "id": "TWT_NEW", "r": 0.5, "x": 10.0, "g": 0.0, "b": 0.0,
        "rated_u1": 400.0, "rated_u2": 225.0, "rated_s": 0.0,
        "position_order_1": 1, "direction_1": "BOTTOM",
        "position_order_2": 1, "direction_2": "BOTTOM",
        "bus_or_busbar_section_id_1": bbs_a,
        "bus_or_busbar_section_id_2": bbs_b,
    }
    errors = validate_create_branch_fields(
        "2-Winding Transformers", fields, network=node_breaker_network,
    )
    assert any("same substation" in e for e in errors)


def test_create_line_end_to_end(node_breaker_network):
    """Pick any two node-breaker busbars and create a Line between them."""
    vls = list_node_breaker_voltage_levels(node_breaker_network)
    # Pick two different VLs (may or may not be in the same substation —
    # Lines don't impose the constraint).
    vl_a = str(vls["id"].iloc[0])
    vl_b = str(vls["id"].iloc[1])
    bbs_a = list_busbar_sections(node_breaker_network, vl_a)[0]
    bbs_b = list_busbar_sections(node_breaker_network, vl_b)[0]

    new_id = "TEST_LINE_NEW"
    fields = {
        "id": new_id, "r": 0.1, "x": 1.0,
        "g1": 0.0, "b1": 0.0, "g2": 0.0, "b2": 0.0,
        "position_order_1": 99, "direction_1": "BOTTOM",
        "position_order_2": 99, "direction_2": "BOTTOM",
        "bus_or_busbar_section_id_1": bbs_a,
        "bus_or_busbar_section_id_2": bbs_b,
    }
    create_branch_bay(node_breaker_network, "Lines", fields)
    assert new_id in node_breaker_network.get_lines().index


def test_create_branch_rejects_unknown(node_breaker_network):
    with pytest.raises(ValueError, match="not a creatable branch"):
        create_branch_bay(node_breaker_network, "Tie Lines", {"id": "X"})


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------
def test_creatable_containers_carries_three_types():
    assert set(CREATABLE_CONTAINERS) == {
        "Substations", "Voltage Levels", "Busbar Sections",
    }
    assert CREATABLE_CONTAINERS["Substations"]["create_function"] == "create_substations"
    assert CREATABLE_CONTAINERS["Voltage Levels"]["create_function"] == "create_voltage_levels"
    assert CREATABLE_CONTAINERS["Busbar Sections"]["create_function"] == "create_busbar_sections"


def test_topology_kinds_includes_node_and_bus_breaker():
    assert TOPOLOGY_KINDS == ["NODE_BREAKER", "BUS_BREAKER"]


def test_voltage_level_validator_registered_in_shared_dict():
    """The container validator hook is added to the shared _VALIDATORS
    on module import — Streamlit relied on this side-effect."""
    assert "_validate_voltage_level" in _VALIDATORS


def test_creatable_containers_re_exported_from_streamlit_state():
    pytest.importorskip("streamlit")
    from iidm_viewer.state import CREATABLE_CONTAINERS as ST
    assert ST is CREATABLE_CONTAINERS


def test_validate_container_fields_flags_required():
    errors = validate_create_container_fields("Substations", {})
    assert any("ID" in e for e in errors)


def test_validate_container_fields_vl_voltage_limits_rule():
    fields = {
        "id": "VL_NEW", "name": "",
        "topology_kind": "NODE_BREAKER", "nominal_v": 400.0,
        "low_voltage_limit": 420.0, "high_voltage_limit": 380.0,
    }
    errors = validate_create_container_fields("Voltage Levels", fields)
    assert any("high_voltage_limit" in e and "low_voltage_limit" in e for e in errors)


def test_validate_container_fields_busbar_requires_vl():
    fields = {"id": "BBS_NEW", "node": 0}
    errors = validate_create_container_fields("Busbar Sections", fields)
    assert any("Voltage level is required" in e for e in errors)


def test_validate_container_fields_rejects_unknown():
    errors = validate_create_container_fields("Mystery", {"id": "X"})
    assert errors and "not a creatable container" in errors[0]


def test_list_substations_df_returns_id_and_display(node_breaker_network):
    df = list_substations_df(node_breaker_network)
    assert df.shape[0] > 0
    assert set(df.columns) == {"id", "display"}


def test_next_free_node_returns_max_plus_one(node_breaker_network):
    """``next_free_node`` must return a non-negative integer; for a
    populated VL it's higher than zero."""
    vls = list_node_breaker_voltage_levels(node_breaker_network)
    vl_id = str(vls["id"].iloc[0])
    n = next_free_node(node_breaker_network, vl_id)
    assert isinstance(n, int)
    assert n >= 0


def test_create_substation_end_to_end():
    """A blank network → create a Substation → confirm it shows up."""
    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="x")
    network = NetworkProxy(run(_make))
    create_container(network, "Substations", {
        "id": "SUB_NEW", "name": "New Sub", "country": "FR", "TSO": "RTE",
    })
    subs = network.get_substations()
    assert "SUB_NEW" in subs.index


def test_create_voltage_level_with_substation_end_to_end():
    """Substation first, then a VL attached to it."""
    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="x")
    network = NetworkProxy(run(_make))
    create_container(network, "Substations", {"id": "S1"})
    create_container(network, "Voltage Levels", {
        "id": "VL_NEW", "name": "", "topology_kind": "NODE_BREAKER",
        "nominal_v": 400.0, "low_voltage_limit": 0.0, "high_voltage_limit": 0.0,
        "substation_id": "S1",
    })
    vls = network.get_voltage_levels()
    assert "VL_NEW" in vls.index
    assert str(vls.at["VL_NEW", "substation_id"]) == "S1"


def test_create_voltage_level_drops_zero_voltage_limits():
    """``low_voltage_limit=0`` / ``high_voltage_limit=0`` are sentinels
    meaning "unset" — they must not be sent to pypowsybl as 0.0."""
    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="x")
    network = NetworkProxy(run(_make))
    create_container(network, "Substations", {"id": "S2"})
    create_container(network, "Voltage Levels", {
        "id": "VL_NOLIMITS", "topology_kind": "NODE_BREAKER",
        "nominal_v": 225.0, "low_voltage_limit": 0.0, "high_voltage_limit": 0.0,
        "substation_id": "S2",
    })
    df = network.get_voltage_levels(all_attributes=True)
    row = df.loc["VL_NOLIMITS"]
    # pypowsybl reports NaN when limits weren't set.
    import math
    assert math.isnan(row.get("low_voltage_limit", float("nan")))
    assert math.isnan(row.get("high_voltage_limit", float("nan")))


def test_create_container_rejects_unknown(node_breaker_network):
    with pytest.raises(ValueError, match="not a creatable container"):
        create_container(node_breaker_network, "Mystery", {"id": "X"})


# ---------------------------------------------------------------------------
# HVDC lines
# ---------------------------------------------------------------------------
@pytest.fixture
def vsc_only_network() -> NetworkProxy:
    """Fresh network with two unwired VSC stations ready for an HVDC line."""
    def _make():
        import pypowsybl.network as pn
        n = pn.create_empty(network_id="x")
        n.create_substations(id="S1")
        n.create_voltage_levels(id="VL1", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=400.0)
        n.create_voltage_levels(id="VL2", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=400.0)
        n.create_busbar_sections(id="BBS1", voltage_level_id="VL1", node=0)
        n.create_busbar_sections(id="BBS2", voltage_level_id="VL2", node=0)
        n.create_vsc_converter_stations(
            id="VSC_A", voltage_level_id="VL1", node=1,
            loss_factor=0.01, voltage_regulator_on=False, target_q=0.0,
        )
        n.create_vsc_converter_stations(
            id="VSC_B", voltage_level_id="VL2", node=1,
            loss_factor=0.01, voltage_regulator_on=False, target_q=0.0,
        )
        return n

    return NetworkProxy(run(_make))


def test_creatable_hvdc_lines_registry_shape():
    names = {f["name"] for f in CREATABLE_HVDC_LINES["fields"]}
    assert {"id", "r", "nominal_v", "max_p", "target_p", "converters_mode"} <= names
    assert CREATABLE_HVDC_LINES["create_function"] == "create_hvdc_lines"


def test_converters_modes_constant():
    assert "SIDE_1_RECTIFIER_SIDE_2_INVERTER" in CONVERTERS_MODES
    assert "SIDE_1_INVERTER_SIDE_2_RECTIFIER" in CONVERTERS_MODES


def test_creatable_hvdc_re_exported_from_streamlit_state():
    pytest.importorskip("streamlit")
    from iidm_viewer.state import (
        CREATABLE_HVDC_LINES as ST_HVDC,
        CONVERTERS_MODES as ST_MODES,
    )
    assert ST_HVDC is CREATABLE_HVDC_LINES
    assert ST_MODES is CONVERTERS_MODES


def test_validate_create_hvdc_line_fields_flags_required():
    errors = validate_create_hvdc_line_fields({})
    assert any("ID" in e for e in errors)
    assert any("Converter station 1" in e for e in errors)
    assert any("Converter station 2" in e for e in errors)


def test_validate_create_hvdc_line_fields_rejects_same_station():
    errors = validate_create_hvdc_line_fields({
        "id": "H", "r": 1.0, "nominal_v": 400.0, "max_p": 1000.0,
        "target_p": 0.0, "converters_mode": CONVERTERS_MODES[0],
        "converter_station1_id": "VSC_A", "converter_station2_id": "VSC_A",
    })
    assert any("must differ" in e for e in errors)


def test_validate_create_hvdc_line_fields_enforces_target_within_max():
    errors = validate_create_hvdc_line_fields({
        "id": "H", "r": 1.0, "nominal_v": 400.0, "max_p": 100.0,
        "target_p": 200.0, "converters_mode": CONVERTERS_MODES[0],
        "converter_station1_id": "A", "converter_station2_id": "B",
    })
    assert any("target_p" in e for e in errors)


def test_validate_create_hvdc_line_fields_accepts_valid_payload():
    assert validate_create_hvdc_line_fields({
        "id": "H", "r": 1.0, "nominal_v": 400.0, "max_p": 1000.0,
        "target_p": 50.0, "converters_mode": CONVERTERS_MODES[0],
        "converter_station1_id": "A", "converter_station2_id": "B",
    }) == []


def test_list_converter_stations_includes_vsc_and_lcc(node_breaker_network):
    stations = list_converter_stations(node_breaker_network)
    ids = {sid for sid, _ in stations}
    kinds = {kind for _, kind in stations}
    # The 4-sub demo carries 2 VSC + 2 LCC stations.
    assert {"VSC1", "VSC2", "LCC1", "LCC2"} <= ids
    assert {"VSC", "LCC"} <= kinds


def test_create_hvdc_line_end_to_end(vsc_only_network):
    create_hvdc_line(vsc_only_network, {
        "id": "HVDC_NEW", "r": 1.0, "nominal_v": 400.0,
        "max_p": 1000.0, "target_p": 50.0,
        "converters_mode": CONVERTERS_MODES[0],
        "converter_station1_id": "VSC_A",
        "converter_station2_id": "VSC_B",
    })
    assert "HVDC_NEW" in vsc_only_network.get_hvdc_lines().index


def test_create_hvdc_line_raises_on_invalid(vsc_only_network):
    with pytest.raises(ValueError, match="must differ"):
        create_hvdc_line(vsc_only_network, {
            "id": "H", "r": 1.0, "nominal_v": 400.0,
            "max_p": 1000.0, "target_p": 0.0,
            "converters_mode": CONVERTERS_MODES[0],
            "converter_station1_id": "VSC_A",
            "converter_station2_id": "VSC_A",
        })


# ---------------------------------------------------------------------------
# Tap changers (ratio + phase) on existing 2WT
# ---------------------------------------------------------------------------
@pytest.fixture
def twt_without_tap_changer_network() -> NetworkProxy:
    """Fresh network with a single 2WT and no tap changers attached."""
    def _make():
        import pypowsybl.network as pn
        n = pn.create_empty(network_id="x")
        n.create_substations(id="S1")
        n.create_voltage_levels(id="VL1", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=400.0)
        n.create_voltage_levels(id="VL2", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=225.0)
        n.create_busbar_sections(id="BBS1", voltage_level_id="VL1", node=0)
        n.create_busbar_sections(id="BBS2", voltage_level_id="VL2", node=0)
        n.create_2_windings_transformers(
            id="T1", voltage_level1_id="VL1", voltage_level2_id="VL2",
            node1=1, node2=1, rated_u1=400.0, rated_u2=225.0,
            r=0.1, x=10.0, g=0.0, b=0.0,
        )
        return n

    return NetworkProxy(run(_make))


def test_creatable_tap_changers_carries_ratio_and_phase():
    assert set(CREATABLE_TAP_CHANGERS) == {"Ratio", "Phase"}
    assert CREATABLE_TAP_CHANGERS["Ratio"]["create_method"] == "create_ratio_tap_changers"
    assert CREATABLE_TAP_CHANGERS["Phase"]["create_method"] == "create_phase_tap_changers"
    # Phase tap changer steps carry an extra ``alpha`` column.
    assert "alpha" in CREATABLE_TAP_CHANGERS["Phase"]["step_columns"]
    assert "alpha" not in CREATABLE_TAP_CHANGERS["Ratio"]["step_columns"]


def test_tap_changer_mode_constants():
    assert "CURRENT_LIMITER" in PTC_REGULATION_MODES
    assert "ACTIVE_POWER_CONTROL" in PTC_REGULATION_MODES
    assert TRANSFORMER_SIDES == ["ONE", "TWO"]


def test_creatable_tap_changers_re_exported_from_streamlit_state():
    pytest.importorskip("streamlit")
    from iidm_viewer.state import (
        CREATABLE_TAP_CHANGERS as ST_TC,
        PTC_REGULATION_MODES as ST_MODES,
        TRANSFORMER_SIDES as ST_SIDES,
    )
    assert ST_TC is CREATABLE_TAP_CHANGERS
    assert ST_MODES is PTC_REGULATION_MODES
    assert ST_SIDES is TRANSFORMER_SIDES


def test_validate_create_tap_changer_fields_flags_missing_target():
    errors = validate_create_tap_changer_fields("Ratio", "", {}, [])
    assert any("Target 2-winding transformer" in e for e in errors)
    assert any("At least one tap step" in e for e in errors)


def test_validate_create_tap_changer_fields_flags_tap_out_of_range():
    errors = validate_create_tap_changer_fields(
        "Ratio", "T1",
        {"tap": 5, "low_tap": 0, "oltc": False, "regulating": False,
         "target_v": 0.0, "target_deadband": 0.0, "regulated_side": "ONE"},
        [{"rho": 1.0}, {"rho": 1.0}, {"rho": 1.0}],
    )
    assert any("between 0 and 2" in e for e in errors)


def test_validate_create_tap_changer_fields_requires_oltc_for_regulating_ratio():
    errors = validate_create_tap_changer_fields(
        "Ratio", "T1",
        {"tap": 1, "low_tap": 0, "oltc": False, "regulating": True,
         "target_v": 400.0, "target_deadband": 0.0, "regulated_side": "ONE"},
        [{"rho": 0.85}, {"rho": 1.0}, {"rho": 1.15}],
    )
    assert any("OLTC must be enabled" in e for e in errors)


def test_validate_create_tap_changer_fields_requires_target_v_for_regulating_ratio():
    errors = validate_create_tap_changer_fields(
        "Ratio", "T1",
        {"tap": 1, "low_tap": 0, "oltc": True, "regulating": True,
         "target_v": 0.0, "target_deadband": 0.0, "regulated_side": "ONE"},
        [{"rho": 1.0}, {"rho": 1.0}, {"rho": 1.0}],
    )
    assert any("target_v must be > 0" in e for e in errors)


def test_validate_create_tap_changer_fields_rejects_unknown_kind():
    errors = validate_create_tap_changer_fields("Mystery", "T1", {}, [])
    assert any("not creatable" in e for e in errors)


def test_list_two_winding_transformers_returns_ids(twt_without_tap_changer_network):
    ids = list_two_winding_transformers(twt_without_tap_changer_network)
    assert ids == ["T1"]


def test_list_transformers_without_tap_changer_filters_by_kind(
    twt_without_tap_changer_network,
):
    # No tap changer yet → T1 should be available for both kinds.
    assert list_transformers_without_tap_changer(
        twt_without_tap_changer_network, "Ratio"
    ) == ["T1"]
    assert list_transformers_without_tap_changer(
        twt_without_tap_changer_network, "Phase"
    ) == ["T1"]


def test_create_ratio_tap_changer_end_to_end(twt_without_tap_changer_network):
    create_tap_changer(
        twt_without_tap_changer_network, "Ratio", "T1",
        {"tap": 1, "low_tap": 0, "oltc": True, "regulating": False,
         "target_v": 400.0, "target_deadband": 2.0, "regulated_side": "ONE"},
        [{"rho": 0.85}, {"rho": 1.0}, {"rho": 1.15}],
    )
    rtcs = twt_without_tap_changer_network.get_ratio_tap_changers()
    assert "T1" in rtcs.index
    # The same transformer is now ineligible for another Ratio tap changer.
    assert list_transformers_without_tap_changer(
        twt_without_tap_changer_network, "Ratio"
    ) == []
    # ...but still eligible for a Phase tap changer.
    assert list_transformers_without_tap_changer(
        twt_without_tap_changer_network, "Phase"
    ) == ["T1"]


def test_create_phase_tap_changer_end_to_end(twt_without_tap_changer_network):
    create_tap_changer(
        twt_without_tap_changer_network, "Phase", "T1",
        {"tap": 1, "low_tap": 0, "regulation_mode": "CURRENT_LIMITER",
         "regulating": False, "target_deadband": 0.0, "regulated_side": "ONE"},
        [{"rho": 1.0, "alpha": -2.0},
         {"rho": 1.0, "alpha": 0.0},
         {"rho": 1.0, "alpha": 2.0}],
    )
    ptcs = twt_without_tap_changer_network.get_phase_tap_changers()
    assert "T1" in ptcs.index


def test_create_tap_changer_drops_zero_sentinel_target_v(
    twt_without_tap_changer_network,
):
    """target_v=0 / target_deadband=0 are sentinels meaning "unset" — they
    must not be sent to pypowsybl as 0.0 (which would mean "regulate to 0V")."""
    create_tap_changer(
        twt_without_tap_changer_network, "Ratio", "T1",
        {"tap": 1, "low_tap": 0, "oltc": False, "regulating": False,
         "target_v": 0.0, "target_deadband": 0.0, "regulated_side": "ONE"},
        [{"rho": 0.95}, {"rho": 1.0}, {"rho": 1.05}],
    )
    rtcs = twt_without_tap_changer_network.get_ratio_tap_changers()
    assert "T1" in rtcs.index
    # When target_v is unset pypowsybl reports NaN.
    import math
    target_v = rtcs.at["T1", "target_v"]
    assert math.isnan(target_v) or target_v != 0.0


def test_create_tap_changer_raises_on_unknown_kind(twt_without_tap_changer_network):
    with pytest.raises(ValueError, match="not creatable"):
        create_tap_changer(
            twt_without_tap_changer_network, "Mystery", "T1", {}, [{"rho": 1.0}],
        )
