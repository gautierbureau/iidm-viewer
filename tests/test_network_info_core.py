"""Tests for the framework-agnostic Overview core.

Lives in :mod:`iidm_viewer.network_info_core` and is used by the
Streamlit, PySide6 and NiceGUI hosts. The Streamlit-side
:mod:`iidm_viewer.network_info` is exercised by ``tests/test_network_info.py``;
this file pins down the host-agnostic helpers.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from iidm_viewer.network_info_core import (
    COUNTRY_TOTALS_DISPLAY_COLUMNS,
    COUNTRY_TOTALS_RAW_COLUMNS,
    LOSSES_BY_COUNTRY_COLUMNS,
    OverviewData,
    OverviewMetadata,
    build_country_totals_display,
    build_losses_by_country_display,
    build_metadata,
    compute_overview_data,
    country_totals_has_lf,
)
from iidm_viewer.state import load_network, run_loadflow


# ── compute_overview_data — the one-hop fetch the prototypes use ──────────

def test_compute_overview_returns_bundle(xiidm_upload):
    """IEEE14 fixture exercises every section without a load flow."""
    net = load_network(xiidm_upload)
    data = compute_overview_data(net)
    assert isinstance(data, OverviewData)
    assert isinstance(data.metadata, OverviewMetadata)
    assert not data.country_totals.empty
    assert data.losses.get("has_data") is False  # pre-LF
    assert data.losses_by_country.empty
    assert data.component_counts  # at least one component type populated


def test_compute_overview_after_loadflow_populates_actuals(xiidm_upload):
    """Post-LF: country actuals + losses + per-country losses populate."""
    net = load_network(xiidm_upload)
    run_loadflow(net)
    data = compute_overview_data(net)
    assert data.losses.get("has_data") is True
    assert data.losses["total"] == pytest.approx(
        data.losses["lines"] + data.losses["transformers"],
    )
    assert not data.losses_by_country.empty
    actual_col = data.country_totals["generation_actual_mw"]
    assert not actual_col.isna().all()


def test_compute_overview_metadata_ieee14(xiidm_upload):
    """Metadata round-trips the network id, format and case date."""
    net = load_network(xiidm_upload)
    data = compute_overview_data(net)
    meta = data.metadata
    assert meta.network_id  # IEEE14 carries an id
    assert meta.source_format  # "CIM1" / "XIIDM" etc.
    # case_date is either a parsable date string or empty.
    assert isinstance(meta.case_date, str)


def test_compute_overview_blank_network_does_not_raise():
    """Blank networks have no VLs / generators / loads — the helpers
    return empty frames instead of raising."""
    from iidm_viewer.powsybl_worker import NetworkProxy, run

    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="empty")

    net = NetworkProxy(run(_make))
    data = compute_overview_data(net)
    assert data.country_totals.empty
    assert data.losses_by_country.empty
    # No components → empty counts dict (not an error).
    assert data.component_counts == {}
    # Metadata still snapshots the empty network's id.
    assert data.metadata.network_id == "empty"


# ── build_metadata pure path (passes a raw Network through the proxy) ─────

def test_build_metadata_string_fields(xiidm_upload):
    net = load_network(xiidm_upload)
    raw = object.__getattribute__(net, "_obj")
    meta = build_metadata(raw)
    assert isinstance(meta.network_id, str)
    assert isinstance(meta.name, str)
    assert isinstance(meta.source_format, str)
    assert isinstance(meta.case_date, str)


# ── Display helpers (rename + round) ──────────────────────────────────────

def _country_totals_frame(with_actuals: bool) -> pd.DataFrame:
    nan = float("nan")
    return pd.DataFrame({
        "country": ["FR", "BE"],
        "generation_target_mw": [100.0, 50.5678],
        "generation_actual_mw": [101.234, 50.6] if with_actuals else [nan, nan],
        "consumption_target_mw": [80.0, 40.0],
        "consumption_actual_mw": [80.5, 40.1234] if with_actuals else [nan, nan],
    })


def test_build_country_totals_display_renames_and_rounds():
    raw = _country_totals_frame(with_actuals=True)
    display = build_country_totals_display(raw)
    assert list(display.columns) == COUNTRY_TOTALS_DISPLAY_COLUMNS
    by_country = display.set_index("Country")
    assert by_country.loc["FR", "Gen target (MW)"] == 100.0
    assert by_country.loc["BE", "Gen target (MW)"] == pytest.approx(50.57, abs=1e-3)
    assert by_country.loc["BE", "Load actual (MW)"] == pytest.approx(40.12, abs=1e-3)


def test_build_country_totals_display_empty_returns_schema_only():
    display = build_country_totals_display(
        pd.DataFrame(columns=COUNTRY_TOTALS_RAW_COLUMNS),
    )
    assert display.empty
    assert list(display.columns) == COUNTRY_TOTALS_DISPLAY_COLUMNS


def test_country_totals_has_lf_detects_actuals():
    """Returns True iff at least one actual cell carries a value."""
    assert country_totals_has_lf(_country_totals_frame(with_actuals=True)) is True
    assert country_totals_has_lf(_country_totals_frame(with_actuals=False)) is False
    assert country_totals_has_lf(pd.DataFrame()) is False


def test_build_losses_by_country_display_rounds():
    series = pd.Series({"FR": 12.34567, "BE": 1.23456})
    display = build_losses_by_country_display(series)
    assert list(display.columns) == LOSSES_BY_COUNTRY_COLUMNS
    by_country = display.set_index("Country")
    assert by_country.loc["FR", "Losses (MW)"] == pytest.approx(12.35, abs=1e-3)


def test_build_losses_by_country_display_empty_returns_schema_only():
    display = build_losses_by_country_display(pd.Series(dtype=float))
    assert display.empty
    assert list(display.columns) == LOSSES_BY_COUNTRY_COLUMNS
