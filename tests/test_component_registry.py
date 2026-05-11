"""Tests for the framework-agnostic ``iidm_viewer.component_registry``.

This module is the shared backbone for the PySide6 and NiceGUI
prototypes' Data Explorer tabs. It must work without booting Qt or
NiceGUI — these tests only need pypowsybl and pandas.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from iidm_viewer.component_registry import (
    COMPONENT_TYPES,
    EDITABLE_COMPONENTS,
    TOPOLOGY_AFFECTING_ATTRIBUTES,
    _coerce,
    apply_bulk_edit,
    apply_cell_edit,
    editable_attributes,
    get_dataframe,
    is_editable,
)
from iidm_viewer.powsybl_worker import NetworkProxy, run


ROOT = Path(__file__).resolve().parent.parent
XIIDM = ROOT / "test_ieee14.xiidm"


@pytest.fixture(scope="module")
def ieee14_network():
    def _load():
        import pypowsybl.network as pn
        return pn.load(str(XIIDM))

    return NetworkProxy(run(_load))


def test_component_types_covers_streamlit_path():
    """If a label drifts from the Streamlit ``network_info`` copy, the
    Data Explorer tabs would lose a row. Keep them aligned.

    Skipped when streamlit isn't installed (the prototypes' own envs
    don't pull it in via their extras).
    """
    pytest.importorskip("streamlit")
    from iidm_viewer.network_info import COMPONENT_TYPES as STREAMLIT_TYPES
    assert COMPONENT_TYPES == STREAMLIT_TYPES


def test_editable_components_covers_streamlit_path():
    pytest.importorskip("streamlit")
    from iidm_viewer.state import EDITABLE_COMPONENTS as STREAMLIT_EDITS
    assert EDITABLE_COMPONENTS == STREAMLIT_EDITS


def test_is_editable_and_editable_attributes():
    assert is_editable("Generators") is True
    assert is_editable("Generators", "target_p") is True
    assert is_editable("Generators", "name") is False
    assert is_editable("Voltage Levels") is False  # MVP: VLs aren't in the registry
    assert "target_p" in editable_attributes("Generators")
    assert editable_attributes("Voltage Levels") == []


def test_topology_affecting_set_includes_connected_and_open():
    assert "connected" in TOPOLOGY_AFFECTING_ATTRIBUTES
    assert "open" in TOPOLOGY_AFFECTING_ATTRIBUTES
    assert "target_p" not in TOPOLOGY_AFFECTING_ATTRIBUTES


def test_coerce_handles_bool_int_float_and_string():
    import numpy as np

    assert _coerce("true", np.dtype("bool")) is True
    assert _coerce("closed", np.dtype("bool")) is False
    assert _coerce(False, np.dtype("bool")) is False
    assert _coerce("42", np.dtype("int64")) == 42
    assert _coerce("1.5", np.dtype("float64")) == pytest.approx(1.5)
    # Strings stay strings for non-numeric dtypes.
    assert _coerce("hello", np.dtype("O")) == "hello"
    with pytest.raises(ValueError):
        _coerce("nope", np.dtype("bool"))


def test_get_dataframe_returns_id_column_and_is_empty_for_missing(ieee14_network):
    df = get_dataframe(ieee14_network, "Generators")
    assert "id" in df.columns
    assert df.shape[0] > 0

    # IEEE14 has no HVDC lines; result is an empty DataFrame, not an exception.
    empty = get_dataframe(ieee14_network, "HVDC Lines")
    assert empty.shape[0] == 0

    # Unknown component label returns empty rather than raising.
    unknown = get_dataframe(ieee14_network, "Nonexistent Type")
    assert unknown.shape[0] == 0


def test_apply_cell_edit_updates_pypowsybl_and_returns_previous(ieee14_network):
    df_before = get_dataframe(ieee14_network, "Generators")
    gen_id = str(df_before["id"].iloc[0])
    prev_value = df_before["target_p"].iloc[0]
    new_value = prev_value + 7.5

    returned_prev = apply_cell_edit(
        ieee14_network, "Generators", gen_id, "target_p", new_value
    )
    assert returned_prev == pytest.approx(prev_value)

    df_after = get_dataframe(ieee14_network, "Generators")
    after_value = df_after[df_after["id"].astype(str) == gen_id]["target_p"].iloc[0]
    assert after_value == pytest.approx(new_value)

    # Revert so other tests aren't affected (module-scoped fixture).
    apply_cell_edit(ieee14_network, "Generators", gen_id, "target_p", prev_value)


def test_apply_cell_edit_coerces_string_inputs(ieee14_network):
    """The Qt prototype routes QLineEdit text through apply_cell_edit;
    strings should coerce cleanly to the column's dtype."""
    df = get_dataframe(ieee14_network, "Loads")
    load_id = str(df["id"].iloc[0])
    prev = df["p0"].iloc[0]
    apply_cell_edit(ieee14_network, "Loads", load_id, "p0", "123.4")
    after = get_dataframe(ieee14_network, "Loads")
    assert after[after["id"].astype(str) == load_id]["p0"].iloc[0] == pytest.approx(123.4)
    apply_cell_edit(ieee14_network, "Loads", load_id, "p0", prev)


def test_apply_cell_edit_rejects_non_editable_component(ieee14_network):
    with pytest.raises(ValueError, match="not editable"):
        apply_cell_edit(ieee14_network, "Voltage Levels", "VL1", "nominal_v", 999.0)


def test_apply_cell_edit_rejects_non_editable_attribute(ieee14_network):
    df = get_dataframe(ieee14_network, "Generators")
    gen_id = str(df["id"].iloc[0])
    with pytest.raises(ValueError, match="not editable for"):
        apply_cell_edit(ieee14_network, "Generators", gen_id, "name", "renamed")


# ---------------------------------------------------------------------------
# apply_bulk_edit
# ---------------------------------------------------------------------------
def test_apply_bulk_edit_updates_all_rows_and_returns_previous_map(ieee14_network):
    df_before = get_dataframe(ieee14_network, "Generators")
    assert df_before.shape[0] >= 3
    ids = [str(x) for x in df_before["id"].iloc[:3]]
    prev_values = {i: df_before[df_before["id"].astype(str) == i]["target_p"].iloc[0]
                   for i in ids}

    new_value = 42.5
    returned = apply_bulk_edit(
        ieee14_network, "Generators", ids, "target_p", new_value,
    )
    assert set(returned.keys()) == set(ids)
    for i in ids:
        assert returned[i] == pytest.approx(prev_values[i])

    df_after = get_dataframe(ieee14_network, "Generators")
    for i in ids:
        after = df_after[df_after["id"].astype(str) == i]["target_p"].iloc[0]
        assert after == pytest.approx(new_value)

    # Revert so other tests aren't affected.
    for i in ids:
        apply_cell_edit(ieee14_network, "Generators", i, "target_p", prev_values[i])


def test_apply_bulk_edit_coerces_once_against_column_dtype(ieee14_network):
    """Boolean attribute via a string input — all rows must end up bool."""
    df_before = get_dataframe(ieee14_network, "Loads")
    assert df_before.shape[0] >= 2
    ids = [str(x) for x in df_before["id"].iloc[:2]]
    prev = [df_before[df_before["id"].astype(str) == i]["connected"].iloc[0] for i in ids]

    apply_bulk_edit(ieee14_network, "Loads", ids, "connected", "false")
    df_after = get_dataframe(ieee14_network, "Loads")
    for i in ids:
        v = df_after[df_after["id"].astype(str) == i]["connected"].iloc[0]
        assert v is False or v is bool(False) or v == False  # noqa: E712

    # Revert.
    for i, p in zip(ids, prev):
        apply_cell_edit(ieee14_network, "Loads", i, "connected", bool(p))


def test_apply_bulk_edit_with_empty_ids_is_noop(ieee14_network):
    """Empty ``element_ids`` must short-circuit before touching pypowsybl
    so callers can pass through a no-selection state without special-casing.
    """
    assert apply_bulk_edit(
        ieee14_network, "Generators", [], "target_p", 10.0,
    ) == {}


def test_apply_bulk_edit_rejects_non_editable_component(ieee14_network):
    with pytest.raises(ValueError, match="not editable"):
        apply_bulk_edit(
            ieee14_network, "Voltage Levels", ["VL1"], "nominal_v", 999.0,
        )


def test_apply_bulk_edit_rejects_non_editable_attribute(ieee14_network):
    df = get_dataframe(ieee14_network, "Generators")
    gen_id = str(df["id"].iloc[0])
    with pytest.raises(ValueError, match="not editable for"):
        apply_bulk_edit(
            ieee14_network, "Generators", [gen_id], "name", "renamed",
        )
