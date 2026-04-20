import streamlit as st
import pandas as pd

from iidm_viewer.filters import build_vl_lookup


def _bus_voltages(network) -> pd.DataFrame:
    """Return buses enriched with nominal_v and v_pu.

    Columns: bus_id, voltage_level_id, nominal_v, v_mag, v_pu.
    v_mag / v_pu are NaN when no load flow has run.
    """
    try:
        buses = network.get_buses(attributes=["v_mag", "angle", "voltage_level_id"]).reset_index()
    except Exception:
        return pd.DataFrame(columns=["bus_id", "voltage_level_id", "nominal_v", "v_mag", "v_pu"])

    vl_lookup = build_vl_lookup(network)
    # vl_lookup index is "id" (voltage level id); rename for the merge
    lookup = vl_lookup[["id", "nominal_v"]].rename(columns={"id": "voltage_level_id"})
    merged = buses.merge(lookup, on="voltage_level_id", how="left")
    merged = merged.rename(columns={"id": "bus_id"})

    merged["v_pu"] = merged["v_mag"] / merged["nominal_v"]
    return merged[["bus_id", "voltage_level_id", "nominal_v", "v_mag", "v_pu"]]


def _shunt_compensation(network) -> pd.DataFrame:
    """Return one row per shunt compensator with reactive power columns (MVAr).

    current_q_mvar  — from load-flow q if available, else b × nominal_v²
    available_q_mvar — additional capacity not yet activated (remaining
                       sections × b_per_section × nominal_v², plus full
                       capacity of disconnected units)
    total_q_mvar    — full capacity at maximum_section_count
    """
    try:
        shunts = network.get_shunt_compensators(
            attributes=[
                "voltage_level_id", "connected", "section_count",
                "max_section_count", "b_per_section", "g_per_section", "q",
            ]
        ).reset_index()
    except Exception:
        return pd.DataFrame()
    if shunts.empty:
        return shunts

    vl_lookup = build_vl_lookup(network)
    lookup = vl_lookup[["id", "nominal_v"]].rename(columns={"id": "voltage_level_id"})
    df = shunts.merge(lookup, on="voltage_level_id", how="left")

    v2 = df["nominal_v"] ** 2  # kV² → MVAr when multiplied by S

    # current injection
    has_lf = df["q"].notna().any()
    if has_lf:
        df["current_q_mvar"] = df["q"]
    else:
        df["current_q_mvar"] = df["b_per_section"] * df["section_count"] * v2

    # total capacity at max sections
    df["total_q_mvar"] = df["b_per_section"] * df["max_section_count"] * v2

    # available but not yet activated
    remaining_sections = (df["max_section_count"] - df["section_count"]).clip(lower=0)
    available_from_sections = df["b_per_section"] * remaining_sections * v2
    # disconnected units: their current contribution is zero, so the full
    # current_q_mvar is "available" once reconnected
    disconnected_loss = df["current_q_mvar"].where(~df["connected"], other=0.0).fillna(0.0)
    df["available_q_mvar"] = available_from_sections + disconnected_loss

    return df[[
        "id", "voltage_level_id", "connected", "section_count",
        "max_section_count", "nominal_v",
        "current_q_mvar", "available_q_mvar", "total_q_mvar",
    ]]


def _svc_compensation(network) -> pd.DataFrame:
    """Return one row per SVC with reactive power columns (MVAr).

    current_q_mvar — from load-flow q if available; 0 for OFF mode
    q_min_mvar / q_max_mvar — operating range from b_min/b_max × nominal_v²
    """
    try:
        svcs = network.get_static_var_compensators(
            attributes=[
                "voltage_level_id", "connected", "regulation_mode",
                "b_min", "b_max", "q",
            ]
        ).reset_index()
    except Exception:
        return pd.DataFrame()
    if svcs.empty:
        return svcs

    vl_lookup = build_vl_lookup(network)
    lookup = vl_lookup[["id", "nominal_v"]].rename(columns={"id": "voltage_level_id"})
    df = svcs.merge(lookup, on="voltage_level_id", how="left")

    v2 = df["nominal_v"] ** 2

    has_lf = df["q"].notna().any()
    if has_lf:
        df["current_q_mvar"] = df["q"].where(df["regulation_mode"] != "OFF", other=0.0)
    else:
        df["current_q_mvar"] = float("nan")

    df["q_min_mvar"] = df["b_min"] * v2
    df["q_max_mvar"] = df["b_max"] * v2

    return df[[
        "id", "voltage_level_id", "connected", "regulation_mode", "nominal_v",
        "current_q_mvar", "q_min_mvar", "q_max_mvar",
    ]]


def _render_voltage_section(buses: pd.DataFrame):
    st.subheader("Bus voltages by nominal level")

    has_lf = buses["v_mag"].notna().any()
    if not has_lf:
        st.info("Voltage magnitudes are not available — run a load flow first.")

    # Summary table grouped by nominal_v
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
    nom_options = sorted(buses["nominal_v"].dropna().unique())
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


def _render_shunt_section(shunts: pd.DataFrame):
    st.subheader("Shunt compensators")
    if shunts.empty:
        st.info("No shunt compensators in this network.")
        return

    has_lf = shunts["current_q_mvar"].notna().any()
    total_active = shunts.loc[shunts["connected"], "current_q_mvar"].sum()
    total_available = shunts["available_q_mvar"].sum()
    total_capacity = shunts["total_q_mvar"].sum()

    mc1, mc2, mc3 = st.columns(3)
    label_active = "Active injection (MVAr)" if has_lf else "Estimated injection (MVAr)"
    mc1.metric(label_active, f"{total_active:.2f}")
    mc2.metric("Available not activated (MVAr)", f"{total_available:.2f}")
    mc3.metric("Total capacity (MVAr)", f"{total_capacity:.2f}")

    if not has_lf:
        st.caption("No load flow — injections estimated as b_per_section × section_count × nominal_v².")

    display = shunts[[
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
    buses = _bus_voltages(network)
    if buses.empty:
        st.info("No bus data available in this network.")
        return

    _render_voltage_section(buses)

    st.divider()

    st.subheader("Reactive compensation")
    shunts = _shunt_compensation(network)
    svcs = _svc_compensation(network)

    if shunts.empty and svcs.empty:
        st.info("No reactive compensation equipment found in this network.")
        return

    _render_shunt_section(shunts)
    _render_svc_section(svcs)
