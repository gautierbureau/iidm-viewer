"""Streamlit "Voltage Analysis" tab.

Renders three sections sharing the framework-agnostic core in
:mod:`iidm_viewer.voltage_analysis_core` (numeric work + display
DataFrames) and the Streamlit-specific Leaflet voltage map in
:mod:`iidm_viewer.voltage_map`:

* **Bus voltages** — summary stats per nominal level + per-nominal
  drill-down with out-of-band cell colouring.
* **Voltage map** — geographical view (Streamlit only — the
  PySide6 / NiceGUI tabs skip this section by design).
* **Reactive compensation** — shunt compensators (capacitive /
  inductive / unknown groups) + static VAR compensators.

This module keeps its existing per-session caches (one per section)
and routes the numeric work through the shared core so PySide6 +
NiceGUI stay bit-identical.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from iidm_viewer.cache_backend import SHUNTS_ENRICHED, SVCS_ENRICHED
from iidm_viewer.caches import (
    _cache_key,
    backend as _backend,
    get_bus_voltages,
    get_shunts_all,
    get_svc_all,
    get_vl_nominal_v,
)
from iidm_viewer.voltage_analysis_core import (
    build_bus_detail,
    build_bus_summary,
    build_shunt_display,
    build_svc_display,
    bus_pu_classify,
    enrich_shunts,
    enrich_svcs,
    has_loadflow,
    list_nominal_voltages,
    shunt_totals,
    split_shunts_by_b,
    svc_totals,
)
from iidm_viewer.voltage_map import render_voltage_map


def _shunt_compensation(network) -> pd.DataFrame:
    """Per-session cache around :func:`enrich_shunts`."""
    key = _cache_key(network)
    cached = _backend.get(SHUNTS_ENRICHED)
    if cached is not None and cached.get("key") == key:
        return cached["df"]

    shunts = get_shunts_all(network)
    if shunts.empty:
        _backend.set(SHUNTS_ENRICHED, {"key": key, "df": pd.DataFrame()})
        return pd.DataFrame()
    result = enrich_shunts(shunts, get_vl_nominal_v(network))
    _backend.set(SHUNTS_ENRICHED, {"key": key, "df": result})
    return result


def _svc_compensation(network) -> pd.DataFrame:
    """Per-session cache around :func:`enrich_svcs`."""
    key = _cache_key(network)
    cached = _backend.get(SVCS_ENRICHED)
    if cached is not None and cached.get("key") == key:
        return cached["df"]

    svcs = get_svc_all(network)
    if svcs.empty:
        _backend.set(SVCS_ENRICHED, {"key": key, "df": pd.DataFrame()})
        return pd.DataFrame()
    result = enrich_svcs(svcs, get_vl_nominal_v(network))
    _backend.set(SVCS_ENRICHED, {"key": key, "df": result})
    return result


def _render_voltage_section(buses: pd.DataFrame):
    st.subheader("Bus voltages by nominal level")

    lf = has_loadflow(buses)
    if not lf:
        st.info("Voltage magnitudes are not available — run a load flow first.")

    summary_df = build_bus_summary(buses)
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    if not lf:
        return

    st.markdown("**Bus detail**")
    col_nom, col_lo, col_hi = st.columns(3)
    nom_options = list_nominal_voltages(buses)
    if st.session_state.get("va_nom_select") not in nom_options:
        st.session_state["va_nom_select"] = nom_options[0]
    selected_nom = col_nom.selectbox(
        "Nominal voltage (kV)", options=nom_options, key="va_nom_select"
    )
    lo = col_lo.number_input("Low threshold (pu)", value=0.95, step=0.01, format="%.3f",
                              key="va_lo_thresh")
    hi = col_hi.number_input("High threshold (pu)", value=1.05, step=0.01, format="%.3f",
                              key="va_hi_thresh")

    display = build_bus_detail(buses, selected_nom)
    outside = display[display["V (pu)"].apply(
        lambda v: bus_pu_classify(v, lo, hi) == "warning"
    )]
    st.caption(
        f"{len(display)} buses at {selected_nom} kV — "
        f"{len(outside)} outside [{lo:.3f}, {hi:.3f}] pu"
    )

    def _color_pu(val):
        if bus_pu_classify(val, lo, hi) == "warning":
            return "background-color: #ff4b4b; color: white"
        return ""

    styled = display.style.map(_color_pu, subset=["V (pu)"])
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _render_shunt_group(grp: pd.DataFrame, has_lf: bool):
    active, available, capacity = shunt_totals(grp)

    mc1, mc2, mc3 = st.columns(3)
    label_active = "Active (MVAr)" if has_lf else "Estimated (MVAr)"
    mc1.metric(label_active, f"{active:.2f}")
    mc2.metric("Available not activated (MVAr)", f"{available:.2f}")
    mc3.metric("Total capacity (MVAr)", f"{capacity:.2f}")

    st.dataframe(
        build_shunt_display(grp), use_container_width=True, hide_index=True,
    )


def _render_shunt_section(shunts: pd.DataFrame):
    st.subheader("Shunt compensators")
    if shunts.empty:
        st.info("No shunt compensators in this network.")
        return

    has_lf = shunts["q"].notna().any()
    if not has_lf:
        st.caption("No load flow — injections estimated as b × nominal_v².")

    cap, ind, unk = split_shunts_by_b(shunts)

    st.markdown("##### Capacitive (b > 0, Q < 0) — injects reactive power, raises voltage")
    if cap.empty:
        st.info("No capacitive shunt compensators in this network.")
    else:
        _render_shunt_group(cap, has_lf)

    st.markdown("##### Inductive (b < 0, Q > 0) — absorbs reactive power, lowers voltage")
    if ind.empty:
        st.info("No inductive shunt compensators in this network.")
    else:
        _render_shunt_group(ind, has_lf)

    if not unk.empty:
        st.markdown("##### Unclassified (b per section unknown — fully disconnected)")
        _render_shunt_group(unk, has_lf)


def _render_svc_section(svcs: pd.DataFrame):
    st.subheader("Static VAR compensators")
    if svcs.empty:
        st.info("No static VAR compensators in this network.")
        return

    has_lf = svcs["current_q_mvar"].notna().any()
    active, total_range = svc_totals(svcs)

    mc1, mc2 = st.columns(2)
    if has_lf:
        mc1.metric("Active injection (MVAr)", f"{active:.2f}")
    else:
        mc1.metric("Active injection (MVAr)", "—", help="Run a load flow first.")
    mc2.metric("Total controllable range (MVAr)", f"{total_range:.2f}")

    st.dataframe(
        build_svc_display(svcs), use_container_width=True, hide_index=True,
    )


def render_voltage_analysis(network):
    buses = get_bus_voltages(network)
    if buses.empty:
        st.info("No bus data available in this network.")
        return

    _render_voltage_section(buses)

    st.divider()
    render_voltage_map(network)

    st.divider()

    st.subheader("Reactive compensation")
    shunts = _shunt_compensation(network)
    svcs = _svc_compensation(network)

    if shunts.empty and svcs.empty:
        st.info("No reactive compensation equipment found in this network.")
        return

    st.info(
        "**Current Q** — Q from the network file when available, "
        "otherwise estimated as −b × V²_nom. "
        "Sign convention: Q < 0 for capacitors, Q > 0 for reactors."
    )

    _render_shunt_section(shunts)
    _render_svc_section(svcs)
