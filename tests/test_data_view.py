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
from iidm_viewer.data_view import (
    FILTERS,
    PRIORITY_ANCHOR,
    PRIORITY_COLUMNS,
    VL_FILTERABLE,
    apply_filter_specs,
    build_vl_lookup,
    compute_filter_widget_spec,
    dataframe_to_csv,
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
