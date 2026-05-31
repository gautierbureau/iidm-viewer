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


# ── Exception paths (raise from pypowsybl getters) ────────────────────────


class _StubNetwork:
    """Minimal network stub. Each ``get_*`` callable can be configured
    to raise (to exercise the ``except Exception: continue`` branches)
    or return a pre-baked DataFrame. Defaults to raising on every call.
    """

    def __init__(self, **overrides):
        self.id = "stub"
        self.name = ""
        self.source_format = ""
        self.case_date = None
        for k, v in overrides.items():
            setattr(self, k, v)

    def _raise(self, *_a, **_k):
        raise RuntimeError("simulated pypowsybl failure")


def test_branch_losses_totals_handles_getter_exceptions():
    """Both ``get_lines`` and ``get_2_windings_transformers`` raising
    must yield ``has_data=False`` instead of propagating."""
    from iidm_viewer.network_info_core import branch_losses_totals
    stub = _StubNetwork()
    stub.get_lines = stub._raise
    stub.get_2_windings_transformers = stub._raise
    losses = branch_losses_totals(stub)
    assert losses["has_data"] is False
    assert losses["lines"] == 0.0
    assert losses["transformers"] == 0.0


def test_build_vl_country_map_handles_voltage_levels_exception():
    """``get_voltage_levels`` raising returns an empty 2-column frame."""
    from iidm_viewer.network_info_core import build_vl_country_map
    stub = _StubNetwork()
    stub.get_voltage_levels = stub._raise
    df = build_vl_country_map(stub)
    assert df.empty
    assert list(df.columns) == ["voltage_level_id", "country"]


def test_build_vl_country_map_handles_substations_exception():
    """``get_voltage_levels`` succeeding but ``get_substations`` raising
    still returns the empty 2-column frame."""
    from iidm_viewer.network_info_core import build_vl_country_map
    stub = _StubNetwork()
    stub.get_voltage_levels = lambda **_k: pd.DataFrame(
        {"id": ["VL1"], "substation_id": ["S1"]},
    ).set_index("id")
    stub.get_substations = stub._raise
    df = build_vl_country_map(stub)
    assert df.empty
    assert list(df.columns) == ["voltage_level_id", "country"]


def test_losses_by_country_handles_getter_exceptions():
    """A network where ``get_voltage_levels`` raises → no VL map → empty
    Series. Catches the early-exit branch."""
    from iidm_viewer.network_info_core import losses_by_country
    stub = _StubNetwork()
    stub.get_voltage_levels = stub._raise
    out = losses_by_country(stub)
    assert out.empty


def test_country_totals_handles_generator_and_load_exceptions(xiidm_upload):
    """A network whose VL country map is fine but whose ``get_generators``
    + ``get_loads`` raise → frame is built from empty inputs and ends up
    empty (no countries to enumerate)."""
    from iidm_viewer.network_info_core import country_totals
    net = load_network(xiidm_upload)
    raw = object.__getattribute__(net, "_obj")
    # Wrap raw so the VL lookup still works but the two heavy getters raise.
    class _Wrap:
        def __init__(self, inner):
            self._inner = inner
        def __getattr__(self, name):
            return getattr(self._inner, name)

    wrap = _Wrap(raw)
    wrap.get_generators = lambda **_k: (_ for _ in ()).throw(RuntimeError("x"))
    wrap.get_loads = lambda **_k: (_ for _ in ()).throw(RuntimeError("x"))
    df = country_totals(wrap)
    # IEEE14 has a VL country map → no countries because generators/loads
    # failed → empty frame.
    assert df.empty


def test_build_component_counts_skips_failing_methods():
    """``getattr(network, method)()`` raising for a label drops it from
    the result instead of bubbling up."""
    from iidm_viewer.network_info_core import build_component_counts
    stub = _StubNetwork()
    # Every COMPONENT_TYPES method raises on the stub by default.
    counts = build_component_counts(stub)
    assert counts == {}


def test_build_component_counts_drops_zero_count_entries():
    """Methods returning an empty frame don't surface in the counts."""
    from iidm_viewer.network_info_core import build_component_counts
    # Build a fake network that returns an empty frame for every method.
    stub = _StubNetwork()
    def _empty(*_a, **_k):
        return pd.DataFrame()

    # Patch every method named in COMPONENT_TYPES to return empty.
    from iidm_viewer.component_registry import COMPONENT_TYPES
    for method in COMPONENT_TYPES.values():
        setattr(stub, method, _empty)
    counts = build_component_counts(stub)
    assert counts == {}


def test_build_metadata_handles_naive_case_date():
    """A case_date without ``.date()`` falls back to ``str(case_date_obj)``."""
    class _NaiveDate:
        def __repr__(self):  # pragma: no cover - debug
            return "_NaiveDate()"
        def __str__(self):
            return "2024-01-02 raw"

    stub = _StubNetwork(case_date=_NaiveDate())
    meta = build_metadata(stub)
    assert meta.case_date == "2024-01-02 raw"


def test_build_metadata_none_case_date():
    """A None case_date stays empty."""
    stub = _StubNetwork(case_date=None)
    meta = build_metadata(stub)
    assert meta.case_date == ""


def test_losses_by_country_splits_cross_border_branches():
    """A branch whose ends live in different countries contributes half
    to each country's losses."""
    from iidm_viewer.network_info_core import losses_by_country
    stub = _StubNetwork()
    stub.get_voltage_levels = lambda **_k: pd.DataFrame(
        {"id": ["VL_FR", "VL_BE"], "substation_id": ["S_FR", "S_BE"]},
    ).set_index("id")
    stub.get_substations = lambda **_k: pd.DataFrame(
        {"id": ["S_FR", "S_BE"], "country": ["FR", "BE"]},
    ).set_index("id")
    # One cross-border line, p1 + p2 = 10 MW losses.
    stub.get_lines = lambda **_k: pd.DataFrame({
        "voltage_level1_id": ["VL_FR"],
        "voltage_level2_id": ["VL_BE"],
        "p1": [55.0], "p2": [-45.0],
    })
    stub.get_2_windings_transformers = lambda **_k: pd.DataFrame()
    out = losses_by_country(stub)
    # Each country gets half the loss (5 MW each).
    assert out["FR"] == pytest.approx(5.0)
    assert out["BE"] == pytest.approx(5.0)


def test_losses_by_country_treats_missing_country_as_dash():
    """A VL whose substation has no country falls back to ``"—"``."""
    from iidm_viewer.network_info_core import losses_by_country
    stub = _StubNetwork()
    stub.get_voltage_levels = lambda **_k: pd.DataFrame(
        {"id": ["VL1"], "substation_id": ["S1"]},
    ).set_index("id")
    stub.get_substations = lambda **_k: pd.DataFrame(
        {"id": ["S1"], "country": [None]},
    ).set_index("id")
    stub.get_lines = lambda **_k: pd.DataFrame({
        "voltage_level1_id": ["VL1"],
        "voltage_level2_id": ["VL1"],
        "p1": [3.0], "p2": [2.0],
    })
    stub.get_2_windings_transformers = lambda **_k: pd.DataFrame()
    out = losses_by_country(stub)
    assert out["—"] == pytest.approx(5.0)


def test_losses_by_country_skips_nan_branch_flows():
    """A branch whose p1 or p2 is NaN doesn't count toward any country."""
    from iidm_viewer.network_info_core import losses_by_country
    stub = _StubNetwork()
    stub.get_voltage_levels = lambda **_k: pd.DataFrame(
        {"id": ["VL1"], "substation_id": ["S1"]},
    ).set_index("id")
    stub.get_substations = lambda **_k: pd.DataFrame(
        {"id": ["S1"], "country": ["FR"]},
    ).set_index("id")
    stub.get_lines = lambda **_k: pd.DataFrame({
        "voltage_level1_id": ["VL1", "VL1"],
        "voltage_level2_id": ["VL1", "VL1"],
        "p1": [float("nan"), 4.0], "p2": [10.0, 2.0],
    })
    stub.get_2_windings_transformers = lambda **_k: pd.DataFrame()
    out = losses_by_country(stub)
    # First branch dropped (p1 NaN); second contributes 6 MW.
    assert out["FR"] == pytest.approx(6.0)
