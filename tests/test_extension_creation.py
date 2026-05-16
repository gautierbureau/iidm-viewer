"""Tests for the framework-agnostic
:mod:`iidm_viewer.extension_creation` helpers.

Registry shape + pure validator branches + end-to-end ``create_extension``
runs against IEEE14, plus a Streamlit drift guard.
"""
from __future__ import annotations

import pandas as pd
import pytest

from iidm_viewer.extension_creation import (
    CREATABLE_EXTENSIONS,
    create_extension,
    list_extension_candidates,
    list_extensions_for_component,
    validate_create_extension_fields,
)
from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_registry_contains_expected_entries():
    for name in (
        "substationPosition", "entsoeArea", "busbarSectionPosition",
        "position", "slackTerminal", "activePowerControl",
        "voltageRegulation", "voltagePerReactivePowerControl",
        "standbyAutomaton", "hvdcAngleDroopActivePowerControl",
        "hvdcOperatorActivePowerRange", "entsoeCategory",
    ):
        assert name in CREATABLE_EXTENSIONS, f"missing entry: {name}"


def test_each_entry_has_label_index_targets_fields():
    for name, schema in CREATABLE_EXTENSIONS.items():
        assert "label" in schema, f"{name} missing label"
        assert "index" in schema, f"{name} missing index"
        assert isinstance(schema.get("targets"), dict) and schema["targets"], (
            f"{name} missing or empty targets map"
        )
        assert isinstance(schema.get("fields"), list) and schema["fields"], (
            f"{name} missing or empty fields list"
        )


def test_field_kinds_are_in_the_known_set():
    known = {"float", "int", "bool", "str", "choice"}
    for name, schema in CREATABLE_EXTENSIONS.items():
        for f in schema["fields"]:
            assert f["kind"] in known, f"{name}/{f['name']} has unknown kind {f['kind']}"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_list_extensions_for_component_filters_by_target_map():
    # Substations are a target for substationPosition + entsoeArea but
    # not for the generator-only entsoeCategory.
    names = list_extensions_for_component("Substations")
    assert "substationPosition" in names
    assert "entsoeArea" in names
    assert "entsoeCategory" not in names


def test_list_extensions_for_component_unknown_returns_empty():
    assert list_extensions_for_component("Bogus") == []


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
def test_validate_unknown_extension_returns_error():
    errors = validate_create_extension_fields("ghost", {})
    assert errors == ["Unknown extension: 'ghost'"]


def test_validate_required_field_missing_substation_position():
    errors = validate_create_extension_fields("substationPosition", {})
    assert any("latitude is required" in e for e in errors)
    assert any("longitude is required" in e for e in errors)


def test_validate_slack_terminal_requires_exactly_one_of_bus_or_element():
    # Both filled → reject.
    errors = validate_create_extension_fields(
        "slackTerminal", {"bus_id": "B1", "element_id": "GEN1"},
    )
    assert any("Exactly one of bus_id or element_id" in e for e in errors)
    # Neither filled → reject.
    errors = validate_create_extension_fields(
        "slackTerminal", {"bus_id": "", "element_id": ""},
    )
    assert any("Exactly one of bus_id or element_id" in e for e in errors)
    # Exactly one filled → accept.
    errors = validate_create_extension_fields(
        "slackTerminal", {"bus_id": "B1", "element_id": ""},
    )
    assert not any("Exactly one of" in e for e in errors)


def test_validate_busbar_position_rejects_negative_indices():
    errors = validate_create_extension_fields(
        "busbarSectionPosition",
        {"busbar_index": -1, "section_index": 1},
    )
    assert any("busbar_index must be >= 0" in e for e in errors)


def test_validate_active_power_control_min_max_band():
    errors = validate_create_extension_fields(
        "activePowerControl",
        {
            "participate": True, "droop": 4.0,
            "min_target_p": 50.0, "max_target_p": 10.0,
        },
    )
    assert any("max_target_p must be >= min_target_p" in e for e in errors)
    # Equal bounds are allowed.
    assert not validate_create_extension_fields(
        "activePowerControl",
        {
            "participate": True, "droop": 4.0,
            "min_target_p": 50.0, "max_target_p": 50.0,
        },
    )


# ---------------------------------------------------------------------------
# End-to-end against IEEE14
# ---------------------------------------------------------------------------
def test_list_extension_candidates_returns_generators():
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    ids = list_extension_candidates(net, "activePowerControl", "Generators")
    # IEEE14 carries B1-G..B8-G generators.
    assert "B1-G" in ids


def test_create_extension_active_power_control_round_trip():
    """Apply ``activePowerControl`` to one IEEE14 generator, then look
    it up via ``get_extensions`` to confirm pypowsybl persisted it."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    create_extension(
        net, "activePowerControl", "B1-G",
        {
            "participate": True, "droop": 5.0,
            "participation_factor": 2.0,
            "min_target_p": None, "max_target_p": None,
        },
    )
    df = net.get_extensions("activePowerControl")
    assert df is not None
    assert "B1-G" in df.index


def test_create_extension_unknown_name_raises():
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    with pytest.raises(ValueError, match="Unknown extension"):
        create_extension(net, "ghost", "B1-G", {})


def test_create_extension_missing_target_raises():
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    with pytest.raises(ValueError, match="Target id is required"):
        create_extension(net, "activePowerControl", "", {
            "participate": True, "droop": 4.0,
        })


def test_create_extension_position_choice_kind_round_trips():
    """Verify the ``choice`` kind survives — ``direction`` is enum-like
    in the registry and pypowsybl rejects unknown values."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    create_extension(
        net, "position", "B1-G",
        {
            "order": 7, "feeder_name": "B1-G",
            "direction": "BOTTOM", "side": "",
        },
    )
    df = net.get_extensions("position")
    assert df is not None
    assert "B1-G" in df.index


def test_create_extension_validates_before_dispatching():
    """An invalid payload raises ``ValueError`` from the validator
    *before* pypowsybl sees it."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    with pytest.raises(ValueError, match="Exactly one of"):
        create_extension(
            net, "slackTerminal", "VL1",
            {"bus_id": "B1", "element_id": "GEN1"},
        )


# ---------------------------------------------------------------------------
# Streamlit drift guard
# ---------------------------------------------------------------------------
def test_streamlit_state_re_exports_shared_extension_creation_helpers():
    pytest.importorskip("streamlit")
    from iidm_viewer.extension_creation import (
        CREATABLE_EXTENSIONS as SHARED_REG,
        list_extension_candidates as SHARED_CANDIDATES,
        list_extensions_for_component as SHARED_FOR_COMPONENT,
        validate_create_extension_fields as SHARED_VALIDATE,
    )
    from iidm_viewer.state import (
        CREATABLE_EXTENSIONS as ST_REG,
        list_extension_candidates as ST_CANDIDATES,
        list_extensions_for_component as ST_FOR_COMPONENT,
        validate_create_extension_fields as ST_VALIDATE,
    )
    assert ST_REG is SHARED_REG
    assert ST_CANDIDATES is SHARED_CANDIDATES
    assert ST_FOR_COMPONENT is SHARED_FOR_COMPONENT
    assert ST_VALIDATE is SHARED_VALIDATE
