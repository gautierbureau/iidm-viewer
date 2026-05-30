"""Framework-agnostic core for the Voltage Analysis tab.

Three sections share this module:

* **Bus voltages** — buses grouped by ``nominal_v``: summary stats
  (min/max/mean pu, kV ranges) + per-nominal drill-down with
  out-of-band cell colouring.
* **Shunt compensators** — capacitive / inductive / unknown grouping,
  with active / available / total Q (MVAr) totals derived from
  ``b_per_section`` and the section count (load-sign convention:
  ``Q = −b × V²``).
* **Static VAR compensators** — Q operating point + Q min / max
  envelope derived from ``b_min`` / ``b_max``.

This module is the framework-agnostic core each UI host (Streamlit
:mod:`iidm_viewer.voltage_analysis`, PySide6
:mod:`iidm_viewer.qt.voltage_analysis_tab`, NiceGUI
``_build_voltage_analysis``) composes into its own widget tree. No
streamlit / Qt / NiceGUI imports here.

Public API:

* :func:`compute_voltage_analysis` — single worker hop that fetches
  the four DataFrames non-Streamlit hosts need (buses, VLs, shunts,
  SVCs) and returns a :class:`VoltageAnalysisData` bundle.
* :func:`enrich_bus_voltages` / :func:`enrich_shunts` /
  :func:`enrich_svcs` — pure math on raw frames. The Streamlit host
  uses its own per-session caches and routes through these for the
  numeric work.
* :func:`build_bus_summary` — one row per nominal voltage with
  bus count + min/max/mean pu + kV range.
* :func:`build_bus_detail` — single-nominal drill-down with ``V (pu)``
  / ``V (kV)`` columns sorted by per-unit voltage.
* :func:`bus_pu_classify` — host-agnostic ``"safe"`` / ``"warning"``
  classifier for the lo/hi pu threshold colouring.
* :func:`split_shunts_by_b` — capacitive (b > 0), inductive (b < 0),
  unknown (b = 0 / NaN) partition.
* :func:`shunt_totals` / :func:`build_shunt_display` — group-level
  totals + display DataFrame for one group of shunt compensators.
* :func:`svc_totals` / :func:`build_svc_display` — same for SVCs.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Display schemas (shared across hosts)
# ---------------------------------------------------------------------------
BUS_SUMMARY_COLUMNS: list[str] = [
    "Nominal (kV)", "Buses",
    "Min (pu)", "Max (pu)", "Mean (pu)",
    "Min (kV)", "Max (kV)",
]
BUS_DETAIL_COLUMNS: list[str] = ["Bus", "Voltage Level", "V (kV)", "V (pu)"]

SHUNT_DISPLAY_COLUMNS: list[str] = [
    "ID", "Voltage Level", "Nominal (kV)", "Connected",
    "Active sections", "Max sections",
    "Current Q (MVAr)", "Available Q (MVAr)", "Total capacity (MVAr)",
]
SVC_DISPLAY_COLUMNS: list[str] = [
    "ID", "Voltage Level", "Nominal (kV)", "Connected",
    "Regulation mode", "Current Q (MVAr)", "Q min (MVAr)", "Q max (MVAr)",
]


# ---------------------------------------------------------------------------
# Data bundle
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VoltageAnalysisData:
    """Bundle returned by :func:`compute_voltage_analysis` — three
    DataFrames the non-Streamlit hosts consume directly.

    All three may be empty (network with no buses / no shunts / no
    SVCs).
    """
    buses: pd.DataFrame
    shunts: pd.DataFrame
    svcs: pd.DataFrame


# ---------------------------------------------------------------------------
# Fetch + compute (one worker hop, used by PySide6 + NiceGUI)
# ---------------------------------------------------------------------------
def _fetch_inputs(
    network: NetworkProxy,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """One worker hop returning ``(buses, vls, shunts, svcs)`` raw frames."""
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        buses = raw.get_buses(all_attributes=True)
        vls = raw.get_voltage_levels(attributes=["nominal_v"])
        shunts = raw.get_shunt_compensators(all_attributes=True)
        svcs = raw.get_static_var_compensators(all_attributes=True)
        return buses, vls, shunts, svcs

    return run(_gather)


def _vl_lookup(vls: pd.DataFrame) -> pd.DataFrame:
    """Normalise the voltage-levels frame into a ``(voltage_level_id,
    nominal_v)`` lookup. Empty input → empty lookup."""
    if vls.empty:
        return pd.DataFrame(columns=["voltage_level_id", "nominal_v"])
    df = vls.reset_index()
    df["id"] = df["id"].astype(str)
    return df.rename(columns={"id": "voltage_level_id"})[
        ["voltage_level_id", "nominal_v"]
    ]


def compute_voltage_analysis(network: NetworkProxy) -> VoltageAnalysisData:
    """Fetch + enrich all three sections in one worker hop.

    Used by the PySide6 + NiceGUI tabs. The Streamlit tab keeps its
    own per-section cached fetches (see :mod:`iidm_viewer.caches`)
    and routes the enrichment through :func:`enrich_bus_voltages`,
    :func:`enrich_shunts` and :func:`enrich_svcs` directly.
    """
    buses, vls, shunts, svcs = _fetch_inputs(network)
    lookup = _vl_lookup(vls)
    return VoltageAnalysisData(
        buses=enrich_bus_voltages(buses, lookup),
        shunts=enrich_shunts(shunts, lookup),
        svcs=enrich_svcs(svcs, lookup),
    )


# ---------------------------------------------------------------------------
# Pure enrichers (operate on raw pypowsybl frames)
# ---------------------------------------------------------------------------
def enrich_bus_voltages(
    buses: pd.DataFrame, vl_lookup: pd.DataFrame,
) -> pd.DataFrame:
    """Return ``bus_id, voltage_level_id, nominal_v, v_mag, v_pu``.

    ``v_pu`` is ``v_mag / nominal_v``; both are NaN when no LF has run.
    Matches :func:`iidm_viewer.caches.get_bus_voltages` semantics so the
    Streamlit host stays bit-identical even though the cache layering
    is different.
    """
    if buses.empty:
        return pd.DataFrame(
            columns=["bus_id", "voltage_level_id", "nominal_v", "v_mag", "v_pu"]
        )
    df = buses.reset_index()
    df["voltage_level_id"] = df["voltage_level_id"].astype(str)
    df = df.merge(vl_lookup, on="voltage_level_id", how="left")
    df = df.rename(columns={"id": "bus_id"})
    df["v_pu"] = df["v_mag"] / df["nominal_v"]
    return df[["bus_id", "voltage_level_id", "nominal_v", "v_mag", "v_pu"]]


def enrich_shunts(
    shunts: pd.DataFrame, vl_lookup: pd.DataFrame,
) -> pd.DataFrame:
    """Return one row per shunt compensator with reactive power columns.

    Output columns: ``id, voltage_level_id, connected, section_count,
    max_section_count, nominal_v, q, current_q_mvar, available_q_mvar,
    total_q_mvar, b_per_section``.

    Math (pypowsybl load-sign convention — ``Q = −b × V²``):

    * ``current_q_mvar`` — ``q`` from the LF when available, else
      ``−b × V²`` from the running susceptance. Forced to 0 for
      disconnected shunts.
    * ``available_q_mvar`` — ``−b_per_section × (max − active) × V²``,
      where ``active`` is ``section_count`` when connected and 0 when
      disconnected (a disconnected shunt has 0 active sections, so all
      sections count as available).
    * ``total_q_mvar`` — ``−b_per_section × max_section_count × V²``.
    * ``b_per_section`` — derived from ``b / section_count``; falls
      back to the raw column pypowsybl exposes for LINEAR models when
      ``section_count == 0``.
    """
    if shunts.empty:
        return pd.DataFrame()
    df = shunts.reset_index()
    df["voltage_level_id"] = df["voltage_level_id"].astype(str)
    df = df.merge(vl_lookup, on="voltage_level_id", how="left")

    v2 = df["nominal_v"] ** 2

    # b is the total current susceptance (b_per_section × section_count).
    # Derive b_per_section from it; fall back to the raw column that
    # pypowsybl exposes for LINEAR models (available even when
    # section_count == 0).
    sc = df["section_count"].replace(0, float("nan"))
    bps = df["b"] / sc
    if "b_per_section" in df.columns:
        bps = bps.fillna(df["b_per_section"])

    has_q = df["q"].notna()
    q_estimate = df["q"].where(has_q, -df["b"] * v2)
    df["current_q_mvar"] = q_estimate.where(df["connected"], other=0.0)

    df["total_q_mvar"] = -bps * df["max_section_count"] * v2

    active_sections = df["section_count"].where(df["connected"], other=0)
    remaining = (df["max_section_count"] - active_sections).clip(lower=0)
    df["available_q_mvar"] = -bps * remaining * v2
    df["b_per_section"] = bps

    return df[[
        "id", "voltage_level_id", "connected", "section_count",
        "max_section_count", "nominal_v", "q",
        "current_q_mvar", "available_q_mvar", "total_q_mvar", "b_per_section",
    ]]


def enrich_svcs(
    svcs: pd.DataFrame, vl_lookup: pd.DataFrame,
) -> pd.DataFrame:
    """Return one row per SVC with reactive power columns.

    Output columns: ``id, voltage_level_id, connected, regulation_mode,
    nominal_v, current_q_mvar, q_min_mvar, q_max_mvar``.

    * ``current_q_mvar`` — from the LF ``q`` (0 when ``regulation_mode
      == "OFF"``); NaN when no LF has run.
    * ``q_min_mvar`` / ``q_max_mvar`` — ``b_min × V²`` / ``b_max × V²``.
    """
    if svcs.empty:
        return pd.DataFrame()
    df = svcs.reset_index()
    df["voltage_level_id"] = df["voltage_level_id"].astype(str)
    df = df.merge(vl_lookup, on="voltage_level_id", how="left")

    v2 = df["nominal_v"] ** 2

    has_lf = df["q"].notna().any()
    if has_lf:
        df["current_q_mvar"] = df["q"].where(
            df["regulation_mode"] != "OFF", other=0.0,
        )
    else:
        df["current_q_mvar"] = float("nan")

    df["q_min_mvar"] = df["b_min"] * v2
    df["q_max_mvar"] = df["b_max"] * v2

    return df[[
        "id", "voltage_level_id", "connected", "regulation_mode", "nominal_v",
        "current_q_mvar", "q_min_mvar", "q_max_mvar",
    ]]


# ---------------------------------------------------------------------------
# Bus voltage display helpers
# ---------------------------------------------------------------------------
def has_loadflow(buses: pd.DataFrame) -> bool:
    """True if at least one bus has a solved ``v_mag``. Empty input → False."""
    if buses.empty or "v_mag" not in buses.columns:
        return False
    return bool(buses["v_mag"].notna().any())


def build_bus_summary(buses: pd.DataFrame) -> pd.DataFrame:
    """Return one row per nominal voltage with bus count + (when an LF
    has run) min/max/mean per-unit voltage + min/max kV.

    Output columns: :data:`BUS_SUMMARY_COLUMNS` (the pu/kV columns are
    omitted when no LF has run — the caller wraps :func:`has_loadflow`
    around the result if it needs to display "no LF" copy).
    """
    if buses.empty:
        return pd.DataFrame(columns=BUS_SUMMARY_COLUMNS)
    lf = has_loadflow(buses)
    rows: list[dict] = []
    for nom_v, g in buses.groupby("nominal_v"):
        row: dict = {"Nominal (kV)": nom_v, "Buses": len(g)}
        if lf:
            valid = g["v_pu"].dropna()
            if not valid.empty:
                row["Min (pu)"] = round(float(valid.min()), 4)
                row["Max (pu)"] = round(float(valid.max()), 4)
                row["Mean (pu)"] = round(float(valid.mean()), 4)
                kv = g["v_mag"].dropna()
                if not kv.empty:
                    row["Min (kV)"] = round(float(kv.min()), 2)
                    row["Max (kV)"] = round(float(kv.max()), 2)
        rows.append(row)
    return pd.DataFrame(rows).sort_values("Nominal (kV)")


def list_nominal_voltages(buses: pd.DataFrame) -> list[float]:
    """Distinct nominal voltages sorted descending — picker order for
    the detail drill-down."""
    if buses.empty or "nominal_v" not in buses.columns:
        return []
    return sorted(buses["nominal_v"].dropna().unique(), reverse=True)


def build_bus_detail(buses: pd.DataFrame, nominal_v: float) -> pd.DataFrame:
    """Return the per-bus detail rows at ``nominal_v`` sorted by ``V (pu)``.

    Output columns: :data:`BUS_DETAIL_COLUMNS`. Buses without a solved
    voltage are dropped (the caller should run :func:`has_loadflow`
    first if it needs the "no LF" message).
    """
    if buses.empty:
        return pd.DataFrame(columns=BUS_DETAIL_COLUMNS)
    subset = buses[buses["nominal_v"] == nominal_v].dropna(subset=["v_pu"])
    if subset.empty:
        return pd.DataFrame(columns=BUS_DETAIL_COLUMNS)
    subset = subset.sort_values("v_pu")
    display = subset[["bus_id", "voltage_level_id", "v_mag", "v_pu"]].copy()
    display.columns = BUS_DETAIL_COLUMNS
    display["V (kV)"] = display["V (kV)"].round(3)
    display["V (pu)"] = display["V (pu)"].round(4)
    return display


def bus_pu_classify(value, lo: float, hi: float) -> str:
    """Classify a per-unit voltage as ``"warning"`` (outside ``[lo, hi]``)
    or ``"safe"``. Returns ``"unknown"`` for NaN / None."""
    if value is None:
        return "unknown"
    try:
        if pd.isna(value):
            return "unknown"
    except TypeError:
        return "unknown"
    try:
        if value < lo or value > hi:
            return "warning"
    except TypeError:
        return "unknown"
    return "safe"


# ---------------------------------------------------------------------------
# Shunt display helpers
# ---------------------------------------------------------------------------
def split_shunts_by_b(
    shunts: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Partition shunts into ``(capacitive, inductive, unknown)`` by
    ``b_per_section`` sign.

    * Capacitive — ``b > 0`` (injects reactive, raises voltage)
    * Inductive  — ``b < 0`` (absorbs reactive, lowers voltage)
    * Unknown    — ``b == 0`` or NaN (fully disconnected; pypowsybl
                   doesn't always expose a per-section value)
    """
    if shunts.empty:
        empty = shunts.iloc[0:0]
        return empty, empty, empty
    cap = shunts[shunts["b_per_section"] > 0]
    ind = shunts[shunts["b_per_section"] < 0]
    unk = shunts[shunts["b_per_section"].isna() | (shunts["b_per_section"] == 0)]
    return cap, ind, unk


def shunt_totals(group: pd.DataFrame) -> tuple[float, float, float]:
    """Return ``(active, available, total)`` MVAr aggregates for a
    shunt group.

    ``active`` only sums the connected shunts (matches the metric the
    Streamlit tab shows above the detail table).
    """
    if group.empty:
        return 0.0, 0.0, 0.0
    active = float(group.loc[group["connected"], "current_q_mvar"].sum())
    available = float(group["available_q_mvar"].sum())
    total = float(group["total_q_mvar"].sum())
    return active, available, total


def build_shunt_display(group: pd.DataFrame) -> pd.DataFrame:
    """Render-ready shunt table — renames + rounds the enriched frame.

    Output columns: :data:`SHUNT_DISPLAY_COLUMNS`. Sorted by ascending
    nominal voltage so low-V shunts surface first.
    """
    if group.empty:
        return pd.DataFrame(columns=SHUNT_DISPLAY_COLUMNS)
    display = group[[
        "id", "voltage_level_id", "nominal_v", "connected",
        "section_count", "max_section_count",
        "current_q_mvar", "available_q_mvar", "total_q_mvar",
    ]].copy()
    for col in ("current_q_mvar", "available_q_mvar", "total_q_mvar"):
        display[col] = display[col].round(3)
    display.columns = SHUNT_DISPLAY_COLUMNS
    return display.sort_values("Nominal (kV)")


# ---------------------------------------------------------------------------
# SVC display helpers
# ---------------------------------------------------------------------------
def svc_totals(svcs: pd.DataFrame) -> tuple[float, float]:
    """Return ``(active_injection, total_controllable_range)`` MVAr.

    ``active_injection`` is NaN when no LF has run; the active sum
    only covers connected, non-OFF SVCs (the only ones whose Q is a
    real operating point).
    """
    if svcs.empty:
        return 0.0, 0.0
    has_lf = svcs["current_q_mvar"].notna().any()
    if has_lf:
        active = svcs[
            svcs["connected"] & (svcs["regulation_mode"] != "OFF")
        ]
        active_q = float(active["current_q_mvar"].sum())
    else:
        active_q = float("nan")
    total_range = float((svcs["q_max_mvar"] - svcs["q_min_mvar"]).sum())
    return active_q, total_range


def build_svc_display(svcs: pd.DataFrame) -> pd.DataFrame:
    """Render-ready SVC table — renames + rounds the enriched frame.

    Output columns: :data:`SVC_DISPLAY_COLUMNS`. Sorted by ascending
    nominal voltage.
    """
    if svcs.empty:
        return pd.DataFrame(columns=SVC_DISPLAY_COLUMNS)
    display = svcs[[
        "id", "voltage_level_id", "nominal_v", "connected",
        "regulation_mode", "current_q_mvar", "q_min_mvar", "q_max_mvar",
    ]].copy()
    for col in ("current_q_mvar", "q_min_mvar", "q_max_mvar"):
        display[col] = display[col].round(3)
    display.columns = SVC_DISPLAY_COLUMNS
    return display.sort_values("Nominal (kV)")
