"""Tests for the framework-agnostic ``iidm_viewer.component_creation`` module."""
from __future__ import annotations

import pytest

from iidm_viewer.component_creation import (
    CREATABLE_BRANCHES,
    CREATABLE_COMPONENTS,
    LOCATOR_FIELDS,
    _SHUNT_LINEAR_FIELDS,
    _VALIDATORS,
    branch_side_locator_fields,
    coerce_field_values,
    create_branch_bay,
    create_component_bay,
    list_busbar_sections,
    list_node_breaker_voltage_levels,
    validate_create_branch_fields,
    validate_create_fields,
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
