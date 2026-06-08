"""Tests for ``iidm_viewer.data_view`` — the framework-agnostic helpers
the Streamlit data explorer, the PySide6 data tab and the NiceGUI
data tab all delegate to.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from iidm_viewer import network_loader
from iidm_viewer.change_log import ChangeLog
from iidm_viewer.data_view import (
    FILTERS,
    PRIORITY_ANCHOR,
    PRIORITY_COLUMNS,
    VL_FILTERABLE,
    apply_and_log_bulk_disconnect,
    apply_and_log_bulk_edit,
    apply_filter_specs,
    build_vl_lookup,
    compute_filter_widget_spec,
    dataframe_to_csv,
    delete_and_log_elements,
    enrich_with_joins,
    filter_by_voltage_level,
    get_enriched_dataframe,
    reorder_columns,
)
from iidm_viewer.powsybl_worker import NetworkProxy


ROOT = Path(__file__).resolve().parent.parent
XIIDM = ROOT / "test_ieee14.xiidm"


@pytest.fixture(scope="module")
def ieee14() -> NetworkProxy:
    return network_loader.load_from_path(str(XIIDM))


# ---------------------------------------------------------------------------
# Column ordering
# ---------------------------------------------------------------------------
def test_reorder_columns_moves_priority_after_anchor():
    df = pd.DataFrame({
        "id": [1], "name": [""], "x": [0], "target_p": [0], "connected": [True],
    })
    out = reorder_columns(df, "Generators")
    cols = list(out.columns)
    # name is the default anchor; priority columns follow.
    assert cols.index("target_p") == cols.index("name") + 1
    assert cols.index("connected") > cols.index("target_p")
    # Non-priority columns survive.
    assert "x" in cols


def test_reorder_columns_is_noop_for_unknown_component():
    df = pd.DataFrame({"id": [1], "name": [""]})
    out = reorder_columns(df, "Substations")  # not in PRIORITY_COLUMNS
    assert list(out.columns) == ["id", "name"]


def test_reorder_columns_uses_priority_anchor_for_branches():
    df = pd.DataFrame({
        "id": ["L1"], "name": [""], "i1": [0.0], "i2": [0.0],
        "connected1": [True], "connected2": [True],
    })
    out = reorder_columns(df, "Lines")
    cols = list(out.columns)
    # Anchor for Lines is "i2".
    assert cols.index("connected1") == cols.index("i2") + 1


# ---------------------------------------------------------------------------
# Filter specs
# ---------------------------------------------------------------------------
def test_compute_filter_widget_spec_bool():
    assert compute_filter_widget_spec(pd.Series([True, False]))["kind"] == "bool"


def test_compute_filter_widget_spec_range_and_constant():
    spec = compute_filter_widget_spec(pd.Series([1.0, 2.0, 3.0]))
    assert spec["kind"] == "range" and spec["min"] == 1.0 and spec["max"] == 3.0
    spec = compute_filter_widget_spec(pd.Series([5.0, 5.0, 5.0]))
    assert spec["kind"] == "range" and spec.get("state") == "constant"
    spec = compute_filter_widget_spec(pd.Series([float("nan"), float("nan")]))
    assert spec.get("state") == "empty"


def test_compute_filter_widget_spec_multiselect_and_skip():
    short = pd.Series(["FR", "BE", "FR", "DE"])
    assert compute_filter_widget_spec(short)["kind"] == "multiselect"
    long_unique = pd.Series([f"id_{i}" for i in range(50)])
    assert compute_filter_widget_spec(long_unique)["kind"] == "skip"


def test_apply_filter_specs_narrows_rows():
    df = pd.DataFrame({
        "country": ["FR", "BE", "FR"],
        "p": [10.0, 20.0, 30.0],
        "ok": [True, False, True],
    })
    # Multiselect on country
    out = apply_filter_specs(df, {"country": ["FR"]})
    assert list(out["country"]) == ["FR", "FR"]
    # Range on numeric
    out = apply_filter_specs(df, {"p": (15.0, 25.0)})
    assert list(out["p"]) == [20.0]
    # Bool as "True"/"False" strings
    out = apply_filter_specs(df, {"ok": "False"})
    assert list(out["ok"]) == [False]


def test_apply_filter_specs_noop_on_empty_or_missing_columns():
    df = pd.DataFrame({"a": [1, 2]})
    assert apply_filter_specs(df, {}).equals(df)
    assert apply_filter_specs(df, {"missing": ["foo"]}).equals(df)


# ---------------------------------------------------------------------------
# Filter by VL
# ---------------------------------------------------------------------------
def test_filter_by_voltage_level_basics():
    df = pd.DataFrame({"id": ["A", "B"], "voltage_level_id": ["VL1", "VL2"]})
    out = filter_by_voltage_level(df, "VL1")
    assert list(out["id"]) == ["A"]
    # No-op when vl_id is empty.
    assert filter_by_voltage_level(df, "").equals(df)
    assert filter_by_voltage_level(df, None).equals(df)
    # No-op when the column is missing.
    df2 = pd.DataFrame({"x": [1]})
    assert filter_by_voltage_level(df2, "VL1").equals(df2)


def test_vl_filterable_set_aligns_with_streamlit_path():
    """The streamlit data_explorer re-exports this set; keep them aligned."""
    pytest.importorskip("streamlit")
    from iidm_viewer.data_explorer import VL_FILTERABLE as STREAMLIT_VL
    assert STREAMLIT_VL == VL_FILTERABLE


# ---------------------------------------------------------------------------
# Enrichment + VL lookup
# ---------------------------------------------------------------------------
def test_enrich_with_joins_adds_country_and_nominal_v(ieee14):
    df = ieee14.get_generators().reset_index()
    lookup = build_vl_lookup(ieee14)
    enriched = enrich_with_joins(df, lookup)
    assert "country" in enriched.columns
    assert "nominal_v" in enriched.columns


def test_enrich_with_joins_handles_branch_side_columns(ieee14):
    """Lines have voltage_level1_id / voltage_level2_id — the helper
    adds matching nominal_v1 / nominal_v2 / country1 / country2 columns."""
    df = ieee14.get_lines().reset_index()
    lookup = build_vl_lookup(ieee14)
    enriched = enrich_with_joins(df, lookup)
    assert "nominal_v1" in enriched.columns
    assert "nominal_v2" in enriched.columns


def test_get_enriched_dataframe_against_ieee14(ieee14):
    df = get_enriched_dataframe(ieee14, "Generators")
    assert df.shape[0] > 0
    assert "country" in df.columns
    assert "nominal_v" in df.columns


def test_get_enriched_dataframe_is_safe_for_unknown_component(ieee14):
    out = get_enriched_dataframe(ieee14, "Nonexistent Type")
    assert out.shape == (0, 0)


# ---------------------------------------------------------------------------
# Streamlit registry drift guards
# ---------------------------------------------------------------------------
def test_filters_aligns_with_streamlit_path():
    pytest.importorskip("streamlit")
    from iidm_viewer.filters import FILTERS as STREAMLIT_FILTERS
    assert STREAMLIT_FILTERS == FILTERS


def test_priority_columns_aligns_with_streamlit_path():
    pytest.importorskip("streamlit")
    from iidm_viewer.data_explorer import (
        PRIORITY_COLUMNS as ST_PRIO, PRIORITY_ANCHOR as ST_ANCHOR,
    )
    assert ST_PRIO == PRIORITY_COLUMNS
    assert ST_ANCHOR == PRIORITY_ANCHOR


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def test_dataframe_to_csv_is_utf8_bytes():
    df = pd.DataFrame({"a": [1, 2], "b": ["é", "ø"]})
    out = dataframe_to_csv(df)
    assert isinstance(out, bytes)
    text = out.decode("utf-8")
    assert "a,b" in text
    assert "é" in text and "ø" in text


# ---------------------------------------------------------------------------
# Bulk-action orchestration helpers (PySide6 + NiceGUI consume these)
# ---------------------------------------------------------------------------
def test_apply_and_log_bulk_edit_updates_network_and_change_log(ieee14):
    """End-to-end: bulk-edit ``target_p`` on two IEEE14 generators,
    confirm the live frame carries the new value, the change log
    holds the previous-value entries, and the outcome dict reports
    the topology-affecting flag."""
    log = ChangeLog()
    gens = ieee14.get_generators()
    ids = list(gens.index[:2])
    assert len(ids) == 2

    outcome = apply_and_log_bulk_edit(
        ieee14, "Generators", ids, "target_p", 42.0,
        change_log=log,
    )
    assert set(outcome["prev_map"].keys()) == set(ids)
    assert outcome["display_value"] == 42.0
    # ``get_dataframe`` returns the id as a column with a RangeIndex.
    refreshed = outcome["refreshed_df"].set_index("id")
    for gen_id in ids:
        assert refreshed.loc[gen_id, "target_p"] == 42.0
    # target_p is *not* topology-affecting (changes power flow only).
    assert outcome["topology_affecting"] is False
    # The change log carries one entry per touched id.
    entries = log.entries("Generators")
    assert {e["element_id"] for e in entries} == set(map(str, ids))


def test_apply_and_log_bulk_edit_marks_topology_affecting_attributes(ieee14):
    """A ``connected`` edit must surface ``topology_affecting=True``
    so hosts can flush their NAD / SLD caches."""
    gens = ieee14.get_generators()
    gen_id = str(gens.index[0])
    outcome = apply_and_log_bulk_edit(
        ieee14, "Generators", [gen_id], "connected", False,
    )
    assert outcome["topology_affecting"] is True
    # Restore so the module-scoped fixture stays usable.
    apply_and_log_bulk_edit(
        ieee14, "Generators", [gen_id], "connected", True,
    )


def test_apply_and_log_bulk_disconnect_records_one_entry_per_attribute():
    """Lines / 2WTs flip two ``connected*`` attributes; confirm the
    helper records one bulk entry per touched attribute."""
    # IEEE14 is a fresh load per call — module fixture would be
    # corrupted by the disconnect.
    net = network_loader.load_from_path(str(XIIDM))
    lines = net.get_lines()
    line_id = str(lines.index[0])
    log = ChangeLog()
    outcome = apply_and_log_bulk_disconnect(
        net, "Lines", [line_id], change_log=log,
    )
    per_attr = outcome["per_attr_prev_map"]
    assert set(per_attr.keys()) == {"connected1", "connected2"}
    # One entry per attribute (not per id × attribute).
    attrs_logged = {e["property"] for e in log.entries("Lines")}
    assert attrs_logged == {"connected1", "connected2"}
    refreshed = outcome["refreshed_df"].set_index("id")
    assert bool(refreshed.loc[line_id, "connected1"]) is False
    assert bool(refreshed.loc[line_id, "connected2"]) is False


def test_delete_and_log_elements_records_removal_with_snapshot():
    """``delete_and_log_elements`` must (1) call pypowsybl's
    ``remove_elements`` (cascade-aware), (2) drop edit-log entries
    for the removed ids, (3) record the removal with the snapshot
    so the Change Log panel can render it."""
    net = network_loader.load_from_path(str(XIIDM))
    gens = net.get_generators()
    snapshot = gens.copy()
    snapshot.index = snapshot.index.astype(str)
    gen_ids = list(gens.index[:2])
    log = ChangeLog()
    # Seed an edit-log entry that must be dropped on removal.
    log.record("Generators", str(gen_ids[0]), "target_p", 0.0, 99.0)
    assert log.entries("Generators")
    removed = delete_and_log_elements(
        net, "Generators", gen_ids,
        change_log=log,
        snapshot_df=snapshot.assign(id=snapshot.index),
    )
    assert set(map(str, gen_ids)).issubset(set(map(str, removed)))
    # The stale edit-log entry is gone, the removal entry is in.
    assert not log.entries("Generators")
    removals = log.removals("Generators")
    assert {str(r["element_id"]) for r in removals} >= set(map(str, gen_ids))


def test_apply_and_log_helpers_tolerate_no_change_log(ieee14):
    """When no ChangeLog is passed the helpers still run; only the
    log-side-effect is skipped."""
    gens = ieee14.get_generators()
    gen_id = str(gens.index[0])
    # No change_log kwarg — must not raise.
    outcome = apply_and_log_bulk_edit(
        ieee14, "Generators", [gen_id], "target_p", 33.0,
    )
    assert outcome["display_value"] == 33.0


# ---------------------------------------------------------------------------
# DataExplorerViewModel + build_data_explorer_view_model
# ---------------------------------------------------------------------------


def test_build_data_explorer_view_model_basic_shape(ieee14):
    """The view-model surfaces filtered rows, editability and total count."""
    from iidm_viewer.data_view import build_data_explorer_view_model

    vm = build_data_explorer_view_model(ieee14, "Generators")

    assert vm.component == "Generators"
    assert vm.method_name == "get_generators"
    assert vm.total_count == len(vm.rows_df)  # no filter applied
    assert vm.filtered_count == vm.total_count
    assert vm.is_editable is True
    assert vm.is_removable is True
    assert "target_p" in vm.editable_cols
    assert vm.is_empty is False


def test_build_data_explorer_view_model_with_vl_filter(ieee14):
    """Filtering by a specific VL narrows the rows but keeps total_count
    at the pre-filter total."""
    from iidm_viewer.data_view import build_data_explorer_view_model

    full = build_data_explorer_view_model(ieee14, "Generators")
    target_vl = str(full.rows_df["voltage_level_id"].iloc[0])

    vm = build_data_explorer_view_model(
        ieee14, "Generators",
        selected_vl=target_vl,
        filter_by_vl=True,
    )
    assert vm.total_count == full.total_count
    assert vm.filtered_count <= full.total_count
    assert vm.filtered_count >= 1
    assert (vm.rows_df["voltage_level_id"].astype(str) == target_vl).all()


def test_build_data_explorer_view_model_with_id_substring(ieee14):
    """Case-insensitive id substring filter."""
    from iidm_viewer.data_view import build_data_explorer_view_model

    full = build_data_explorer_view_model(ieee14, "Generators")
    target_id = str(full.rows_df.index[0])
    # Lower-case half the id so we exercise case-insensitivity too.
    fragment = target_id[: max(1, len(target_id) // 2)].lower()

    vm = build_data_explorer_view_model(
        ieee14, "Generators",
        id_filter_substring=fragment,
    )
    assert vm.filtered_count >= 1
    assert vm.total_count == full.total_count
    assert all(
        fragment.lower() in str(idx).lower()
        for idx in vm.rows_df.index
    )


def test_build_data_explorer_view_model_empty_component(ieee14):
    """A component absent from the network yields an empty view-model
    without raising."""
    from iidm_viewer.data_view import build_data_explorer_view_model

    vm = build_data_explorer_view_model(ieee14, "Batteries")
    assert vm.is_empty
    assert vm.total_count == 0
    assert vm.filtered_count == 0


def test_build_data_explorer_view_model_non_editable_component(ieee14):
    """Voltage Levels are surfaced but not editable: editable_cols
    is empty, is_editable False, is_removable True (registry says so)."""
    from iidm_viewer.data_view import build_data_explorer_view_model

    vm = build_data_explorer_view_model(ieee14, "Voltage Levels")
    assert vm.is_editable is False
    assert vm.editable_cols == ()
    # Voltage Levels are in REMOVABLE_COMPONENTS.
    assert vm.is_removable is True


# ---------------------------------------------------------------------------
# compute_changes
# ---------------------------------------------------------------------------


def test_compute_changes_returns_only_changed_cells():
    from iidm_viewer.data_view import compute_changes

    base = pd.DataFrame(
        {"a": [1, 2, 3], "b": [10.0, 20.0, 30.0]},
        index=pd.Index(["G1", "G2", "G3"]),
    )
    edited = pd.DataFrame(
        {"a": [1, 2, 9], "b": [10.0, 99.0, 30.0]},
        index=pd.Index(["G1", "G2", "G3"]),
    )
    changes = compute_changes(base, edited, ["a", "b"])
    # G1 unchanged → dropped. G2 changed in b only. G3 changed in a only.
    assert set(changes.index) == {"G2", "G3"}
    assert changes.at["G2", "b"] == 99.0
    # G3 row has only the "a" change; "b" cell should be NaN (sparse).
    assert pd.isna(changes.at["G3", "b"])
    assert changes.at["G3", "a"] == 9


def test_compute_changes_empty_when_no_edits():
    from iidm_viewer.data_view import compute_changes

    base = pd.DataFrame({"a": [1, 2]}, index=pd.Index(["G1", "G2"]))
    edited = base.copy()
    assert compute_changes(base, edited, ["a"]).empty


def test_compute_changes_ignores_non_editable_columns():
    from iidm_viewer.data_view import compute_changes

    base = pd.DataFrame(
        {"a": [1, 2], "readonly": [10, 20]},
        index=pd.Index(["G1", "G2"]),
    )
    edited = pd.DataFrame(
        {"a": [1, 2], "readonly": [99, 99]},  # readonly changed but not editable
        index=pd.Index(["G1", "G2"]),
    )
    assert compute_changes(base, edited, ["a"]).empty


def test_compute_changes_treats_nan_equal_to_nan():
    """A grid that opens with NaN cells the user doesn't touch must
    not register changes — even though ``NaN != NaN`` in pandas."""
    from iidm_viewer.data_view import compute_changes
    import numpy as np

    base = pd.DataFrame(
        {"a": [1.0, np.nan]}, index=pd.Index(["G1", "G2"]),
    )
    edited = base.copy()
    assert compute_changes(base, edited, ["a"]).empty


def test_compute_changes_handles_missing_columns():
    """Editable cols that don't exist on ``base_df`` are skipped, not
    raised."""
    from iidm_viewer.data_view import compute_changes

    base = pd.DataFrame({"a": [1]}, index=pd.Index(["G1"]))
    edited = pd.DataFrame({"a": [9]}, index=pd.Index(["G1"]))
    # "missing_col" not in base.columns — filtered out.
    changes = compute_changes(base, edited, ["a", "missing_col"])
    assert changes.at["G1", "a"] == 9


def test_compute_changes_returns_empty_for_no_editable_intersection():
    from iidm_viewer.data_view import compute_changes

    base = pd.DataFrame({"a": [1]}, index=pd.Index(["G1"]))
    edited = pd.DataFrame({"a": [9]}, index=pd.Index(["G1"]))
    assert compute_changes(base, edited, ["x", "y"]).empty
