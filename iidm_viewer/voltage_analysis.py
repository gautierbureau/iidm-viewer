import streamlit as st
import pandas as pd

from iidm_viewer.caches import (
    _cache_key,
    get_bus_voltages,
    get_shunts_all,
    get_svc_all,
    get_vl_nominal_v,
)
from iidm_viewer.voltage_map import render_voltage_map


def _shunt_compensation(network) -> pd.DataFrame:
    """Return one row per shunt compensator with reactive power columns (MVAr).

    current_q_mvar   — from load-flow q if available, else −b × nominal_v²
                       (pypowsybl load-sign convention: Q < 0 for capacitors, Q > 0 for reactors)
    available_q_mvar — −b_per_section × remaining_sections × nominal_v²
    total_q_mvar     — −b_per_section × max_section_count × nominal_v²
                       (NaN when section_count == 0 and no b_per_section column)
    b_per_section    — susceptance per section; sign determines capacitive vs inductive
                       (NaN when section_count == 0 and pypowsybl does not expose it)
    """
    key = _cache_key(network)
    cached = st.session_state.get("_shunts_enriched_cache")
    if cached is not None and cached.get("key") == key:
        return cached["df"]

    shunts = get_shunts_all(network)
    if shunts.empty:
        st.session_state["_shunts_enriched_cache"] = {"key": key, "df": pd.DataFrame()}
        return pd.DataFrame()
    shunts = shunts.reset_index()
    if shunts.empty:
        st.session_state["_shunts_enriched_cache"] = {"key": key, "df": shunts}
        return shunts

    shunts["voltage_level_id"] = shunts["voltage_level_id"].astype(str)
    lookup = get_vl_nominal_v(network)
    df = shunts.merge(lookup, on="voltage_level_id", how="left")

    v2 = df["nominal_v"] ** 2

    # b is the total current susceptance (b_per_section × section_count).
    # Derive b_per_section from it; fall back to the raw column that pypowsybl
    # exposes for LINEAR models (available even when section_count == 0).
    sc = df["section_count"].replace(0, float("nan"))
    bps = df["b"] / sc  # NaN when section_count == 0
    if "b_per_section" in df.columns:
        bps = bps.fillna(df["b_per_section"])

    # pypowsybl load-sign convention: Q = −b × V²
    #   capacitors (b > 0) → Q < 0   reactors (b < 0) → Q > 0
    # Use load-flow q when available (already in load-sign convention);
    # fall back to −b × V² so the sign is consistent in both cases.
    has_q = df["q"].notna()
    q_estimate = df["q"].where(has_q, -df["b"] * v2)
    df["current_q_mvar"] = q_estimate.where(df["connected"], other=0.0)

    df["total_q_mvar"] = -bps * df["max_section_count"] * v2

    # Disconnected shunts count all sections as available (none are in use).
    active_sections = df["section_count"].where(df["connected"], other=0)
    remaining = (df["max_section_count"] - active_sections).clip(lower=0)
    df["available_q_mvar"] = -bps * remaining * v2
    df["b_per_section"] = bps

    result = df[[
        "id", "voltage_level_id", "connected", "section_count",
        "max_section_count", "nominal_v", "q",
        "current_q_mvar", "available_q_mvar", "total_q_mvar", "b_per_section",
    ]]
    st.session_state["_shunts_enriched_cache"] = {"key": key, "df": result}
    return result


def _svc_compensation(network) -> pd.DataFrame:
    """Return one row per SVC with reactive power columns (MVAr).

    current_q_mvar  — from load-flow q if available; 0 for OFF mode
    q_min_mvar      — b_min × nominal_v²
    q_max_mvar      — b_max × nominal_v²
    """
    key = _cache_key(network)
    cached = st.session_state.get("_svcs_enriched_cache")
    if cached is not None and cached.get("key") == key:
        return cached["df"]

    svcs = get_svc_all(network)
    if svcs.empty:
        st.session_state["_svcs_enriched_cache"] = {"key": key, "df": pd.DataFrame()}
        return pd.DataFrame()
    svcs = svcs.reset_index()

    svcs["voltage_level_id"] = svcs["voltage_level_id"].astype(str)
    lookup = get_vl_nominal_v(network)
    df = svcs.merge(lookup, on="voltage_level_id", how="left")

    v2 = df["nominal_v"] ** 2

    has_lf = df["q"].notna().any()
    if has_lf:
        df["current_q_mvar"] = df["q"].where(df["regulation_mode"] != "OFF", other=0.0)
    else:
        df["current_q_mvar"] = float("nan")

    df["q_min_mvar"] = df["b_min"] * v2
    df["q_max_mvar"] = df["b_max"] * v2

    result = df[[
        "id", "voltage_level_id", "connected", "regulation_mode", "nominal_v",
        "current_q_mvar", "q_min_mvar", "q_max_mvar",
    ]]
    st.session_state["_svcs_enriched_cache"] = {"key": key, "df": result}
    return result


def _render_voltage_section(buses: pd.DataFrame):
    st.subheader("Bus voltages by nominal level")

    has_lf = buses["v_mag"].notna().any()
    if not has_lf:
        st.info("Voltage magnitudes are not available — run a load flow first.")

    grp = buses.groupby("nominal_v")
    summary_rows = []
    for nom_v, g in grp:
        row = {"Nominal (kV)": nom_v, "Buses": len(g)}
        if has_lf:
            valid = g["v_pu"].dropna()
            if not valid.empty:
                row["Min (pu)"] = round(float(valid.min()), 4)
                row["Max (pu)"] = round(float(valid.max()), 4)
                row["Mean (pu)"] = round(float(valid.mean()), 4)
                row["Min (kV)"] = round(float(g["v_mag"].dropna().min()), 2)
                row["Max (kV)"] = round(float(g["v_mag"].dropna().max()), 2)
        summary_rows.append(row)

    summary_df = pd.DataFrame(summary_rows).sort_values("Nominal (kV)")
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    if not has_lf:
        return

    st.markdown("**Bus detail**")
    col_nom, col_lo, col_hi = st.columns(3)
    nom_options = sorted(buses["nominal_v"].dropna().unique(), reverse=True)
    if st.session_state.get("va_nom_select") not in nom_options:
        st.session_state["va_nom_select"] = nom_options[-1]
    selected_nom = col_nom.selectbox(
        "Nominal voltage (kV)", options=nom_options, key="va_nom_select"
    )
    lo = col_lo.number_input("Low threshold (pu)", value=0.95, step=0.01, format="%.3f",
                              key="va_lo_thresh")
    hi = col_hi.number_input("High threshold (pu)", value=1.05, step=0.01, format="%.3f",
                              key="va_hi_thresh")

    subset = buses[buses["nominal_v"] == selected_nom].copy()
    subset = subset.dropna(subset=["v_pu"]).sort_values("v_pu")

    outside = subset[(subset["v_pu"] < lo) | (subset["v_pu"] > hi)]
    st.caption(
        f"{len(subset)} buses at {selected_nom} kV — "
        f"{len(outside)} outside [{lo:.3f}, {hi:.3f}] pu"
    )

    def _color_pu(val):
        try:
            if val < lo or val > hi:
                return "background-color: #ff4b4b; color: white"
        except TypeError:
            pass
        return ""

    display = subset[["bus_id", "voltage_level_id", "v_mag", "v_pu"]].copy()
    display.columns = ["Bus", "Voltage Level", "V (kV)", "V (pu)"]
    display["V (kV)"] = display["V (kV)"].round(3)
    display["V (pu)"] = display["V (pu)"].round(4)
    styled = display.style.map(_color_pu, subset=["V (pu)"])
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _render_shunt_group(grp: pd.DataFrame, has_lf: bool):
    total_active = grp.loc[grp["connected"], "current_q_mvar"].sum()
    total_available = grp["available_q_mvar"].sum()
    total_capacity = grp["total_q_mvar"].sum()

    mc1, mc2, mc3 = st.columns(3)
    label_active = "Active (MVAr)" if has_lf else "Estimated (MVAr)"
    mc1.metric(label_active, f"{total_active:.2f}")
    mc2.metric("Available not activated (MVAr)", f"{total_available:.2f}")
    mc3.metric("Total capacity (MVAr)", f"{total_capacity:.2f}")

    display = grp[[
        "id", "voltage_level_id", "nominal_v", "connected",
        "section_count", "max_section_count",
        "current_q_mvar", "available_q_mvar", "total_q_mvar",
    ]].copy()
    for col in ("current_q_mvar", "available_q_mvar", "total_q_mvar"):
        display[col] = display[col].round(3)
    display.columns = [
        "ID", "Voltage Level", "Nominal (kV)", "Connected",
        "Active sections", "Max sections",
        "Current Q (MVAr)", "Available Q (MVAr)", "Total capacity (MVAr)",
    ]
    st.dataframe(display.sort_values("Nominal (kV)"), use_container_width=True, hide_index=True)


def _render_shunt_section(shunts: pd.DataFrame):
    st.subheader("Shunt compensators")
    if shunts.empty:
        st.info("No shunt compensators in this network.")
        return

    has_lf = shunts["q"].notna().any()
    if not has_lf:
        st.caption("No load flow — injections estimated as b × nominal_v².")

    cap = shunts[shunts["b_per_section"] > 0]
    ind = shunts[shunts["b_per_section"] < 0]
    unk = shunts[shunts["b_per_section"].isna() | (shunts["b_per_section"] == 0)]

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
    active_svcs = svcs[svcs["connected"] & (svcs["regulation_mode"] != "OFF")]
    total_active = active_svcs["current_q_mvar"].sum() if has_lf else float("nan")
    total_range = (svcs["q_max_mvar"] - svcs["q_min_mvar"]).sum()

    mc1, mc2 = st.columns(2)
    if has_lf:
        mc1.metric("Active injection (MVAr)", f"{total_active:.2f}")
    else:
        mc1.metric("Active injection (MVAr)", "—", help="Run a load flow first.")
    mc2.metric("Total controllable range (MVAr)", f"{total_range:.2f}")

    display = svcs[[
        "id", "voltage_level_id", "nominal_v", "connected",
        "regulation_mode", "current_q_mvar", "q_min_mvar", "q_max_mvar",
    ]].copy()
    for col in ("current_q_mvar", "q_min_mvar", "q_max_mvar"):
        display[col] = display[col].round(3)
    display.columns = [
        "ID", "Voltage Level", "Nominal (kV)", "Connected",
        "Regulation mode", "Current Q (MVAr)", "Q min (MVAr)", "Q max (MVAr)",
    ]
    st.dataframe(display.sort_values("Nominal (kV)"), use_container_width=True, hide_index=True)


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
