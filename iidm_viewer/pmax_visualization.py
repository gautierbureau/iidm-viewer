"""Pmax transmissible visualization.

For each transmission line: Pmax = V1 × V2 / X (MW, with V in kV, X in Ω).
The operating ratio P/Pmax = sin(δ) reveals how close the network is to the
steady-state stability limit (δ → 90°  ⟹  P → Pmax).
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from iidm_viewer.caches import (
    _cache_key,
    get_buses_all,
    get_enriched_component,
    get_lines_all,
)


def _compute_pmax_data(network) -> pd.DataFrame:
    """Return a DataFrame with Pmax analysis for every line that has valid LF data.

    Requires a completed load flow (v_mag > 0 on both terminal buses).
    Columns: name, voltage_level1_id, voltage_level2_id, x_ohm, v1_kv, v2_kv,
             pmax_mw, p_actual_mw, p_pmax_ratio, delta_deg, margin_pct.

    Cached by ``(net_key, lf_gen)`` so repeated tab visits hit the cache
    instead of re-iterating every line.
    """
    key = _cache_key(network)
    cached = st.session_state.get("_pmax_cache")
    if cached is not None and cached.get("key") == key:
        return cached["df"]

    lines = get_lines_all(network)
    if lines.empty:
        st.session_state["_pmax_cache"] = {"key": key, "df": pd.DataFrame()}
        return pd.DataFrame()

    buses = get_buses_all(network)
    if buses.empty:
        st.session_state["_pmax_cache"] = {"key": key, "df": pd.DataFrame()}
        return pd.DataFrame()

    lines_en = get_enriched_component(network, "get_lines")

    rows = []
    for line_id, r in lines_en.iterrows():
        x = float(r.get("x", 0) or 0)
        if abs(x) < 1e-6:
            continue

        bus1_id = r.get("bus1_id")
        bus2_id = r.get("bus2_id")
        if not bus1_id or not bus2_id:
            continue
        if bus1_id not in buses.index or bus2_id not in buses.index:
            continue

        v1 = float(buses.loc[bus1_id, "v_mag"])
        v2 = float(buses.loc[bus2_id, "v_mag"])
        if not (v1 > 0 and v2 > 0) or pd.isna(v1) or pd.isna(v2):
            continue

        # Pmax = V1_kV × V2_kV / X_Ω  [MW]
        pmax = v1 * v2 / abs(x)
        p1_raw = r.get("p1")
        p_actual = abs(float(p1_raw)) if pd.notna(p1_raw) else 0.0

        ratio = p_actual / pmax if pmax > 1e-6 else float("nan")
        if pd.notna(ratio) and 0.0 <= ratio <= 1.0:
            delta_deg = float(np.degrees(np.arcsin(ratio)))
        else:
            delta_deg = float("nan")

        margin_pct = (1.0 - ratio) * 100.0 if pd.notna(ratio) else float("nan")

        rows.append({
            "line_id": line_id,
            "name": str(r.get("name", "") or ""),
            "x_ohm": x,
            "v1_kv": v1,
            "v2_kv": v2,
            "pmax_mw": pmax,
            "p_actual_mw": p_actual,
            "p_pmax_ratio": ratio,
            "delta_deg": delta_deg,
            "margin_pct": margin_pct,
            "voltage_level1_id": str(r.get("voltage_level1_id", "") or ""),
            "voltage_level2_id": str(r.get("voltage_level2_id", "") or ""),
        })

    if not rows:
        st.session_state["_pmax_cache"] = {"key": key, "df": pd.DataFrame()}
        return pd.DataFrame()

    df = pd.DataFrame(rows).set_index("line_id").sort_values(
        "margin_pct", ascending=True
    )
    st.session_state["_pmax_cache"] = {"key": key, "df": df}
    return df


def _build_pangle_chart(line_id: str, row: pd.Series) -> go.Figure:
    """Return a Plotly figure showing the P-δ characteristic for one line."""
    pmax = row["pmax_mw"]
    p_actual = row["p_actual_mw"]
    delta_deg = row["delta_deg"]
    ratio = row["p_pmax_ratio"]

    delta_arr = np.linspace(0, 90, 270)
    p_curve = pmax * np.sin(np.radians(delta_arr))

    fig = go.Figure()

    # Background stability zones (by P/Pmax thresholds mapped to angle)
    for lo_r, hi_r, color, label in [
        (0.0, 0.6, "rgba(0,180,0,0.08)", "Safe < 60%"),
        (0.6, 0.8, "rgba(255,165,0,0.12)", "Caution 60–80%"),
        (0.8, 1.0, "rgba(220,0,0,0.12)", "Warning 80–100%"),
    ]:
        a0 = float(np.degrees(np.arcsin(lo_r)))
        a1 = float(np.degrees(np.arcsin(hi_r)))
        fig.add_vrect(
            x0=a0, x1=a1,
            fillcolor=color,
            layer="below",
            line_width=0,
            annotation_text=label,
            annotation_position="top right",
            annotation_font_size=10,
        )

    # P-δ curve
    fig.add_trace(go.Scatter(
        x=delta_arr,
        y=p_curve,
        mode="lines",
        line=dict(color="rgb(99, 110, 250)", width=2),
        name="P = Pmax × sin(δ)",
    ))

    # Pmax reference line
    fig.add_hline(
        y=pmax,
        line_dash="dot",
        line_color="grey",
        annotation_text=f"Pmax = {pmax:.0f} MW",
        annotation_position="top right",
    )

    # Operating point
    if pd.notna(delta_deg) and pd.notna(p_actual) and p_actual > 0:
        op_color = "red" if pd.notna(ratio) and ratio >= 0.8 else (
            "orange" if pd.notna(ratio) and ratio >= 0.6 else "green"
        )
        fig.add_trace(go.Scatter(
            x=[delta_deg],
            y=[p_actual],
            mode="markers",
            marker=dict(size=14, color=op_color, symbol="circle"),
            name=f"Operating point  δ={delta_deg:.1f}°  P/Pmax={ratio:.1%}",
        ))
        fig.add_vline(
            x=delta_deg,
            line_dash="dash",
            line_color=op_color,
            line_width=1,
        )

    fig.update_layout(
        title=f"Power-Angle Characteristic — {line_id}",
        xaxis_title="Angle δ (degrees)",
        yaxis_title="Active Power (MW)",
        xaxis=dict(range=[0, 90], tickvals=list(range(0, 91, 10))),
        yaxis=dict(range=[0, pmax * 1.15]),
        showlegend=True,
        height=480,
    )
    return fig


def render_pmax_visualization(network, selected_vl):
    st.caption(
        "For each line: **Pmax = V₁ × V₂ / X**  (V in kV, X in Ω, result in MW). "
        "The ratio **P/Pmax = sin(δ)** shows proximity to the steady-state "
        "stability limit — the operating point reaches the limit when δ = 90°."
    )

    pmax_df = _compute_pmax_data(network)

    if pmax_df.empty:
        st.info(
            "No data available. Make sure a load flow has been run and the "
            "network contains transmission lines."
        )
        return

    # Optional VL filter
    if selected_vl:
        vl_mask = (
            (pmax_df["voltage_level1_id"] == selected_vl)
            | (pmax_df["voltage_level2_id"] == selected_vl)
        )
        vl_subset = pmax_df[vl_mask]
        if not vl_subset.empty:
            only_vl = st.checkbox(
                f"Only lines connected to VL {selected_vl}",
                value=False,
                key="pmax_only_vl",
            )
            if only_vl:
                pmax_df = vl_subset

    # --- Summary table ---
    st.subheader("Lines sorted by proximity to stability limit")

    show = pmax_df[[
        "name", "voltage_level1_id", "voltage_level2_id",
        "pmax_mw", "p_actual_mw", "p_pmax_ratio", "delta_deg", "margin_pct",
    ]].copy()
    show.columns = [
        "Name", "VL 1", "VL 2",
        "Pmax (MW)", "P (MW)", "P/Pmax", "δ (°)", "Margin (%)",
    ]
    show["Pmax (MW)"] = show["Pmax (MW)"].round(1)
    show["P (MW)"] = show["P (MW)"].round(1)
    show["P/Pmax"] = show["P/Pmax"].round(3)
    show["δ (°)"] = show["δ (°)"].round(1)
    show["Margin (%)"] = show["Margin (%)"].round(1)

    def _color_ratio(val):
        if pd.isna(val):
            return ""
        if val >= 0.8:
            return "background-color: #ff4b4b; color: white"
        if val >= 0.6:
            return "background-color: #ffa500"
        return ""

    def _color_margin(val):
        if pd.isna(val):
            return ""
        if val <= 20:
            return "background-color: #ff4b4b; color: white"
        if val <= 40:
            return "background-color: #ffa500"
        return ""

    styled = show.style.map(_color_ratio, subset=["P/Pmax"]).map(
        _color_margin, subset=["Margin (%)"]
    )
    st.dataframe(styled, use_container_width=True)

    # --- Per-line detail ---
    st.subheader("Power-angle characteristic")

    line_ids = pmax_df.index.tolist()
    selected_line = st.selectbox(
        "Select line", options=line_ids, key="pmax_line_select"
    )

    if selected_line and selected_line in pmax_df.index:
        row = pmax_df.loc[selected_line]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Pmax", f"{row['pmax_mw']:.1f} MW")
        c2.metric("P actual", f"{row['p_actual_mw']:.1f} MW")
        ratio_val = row["p_pmax_ratio"]
        margin_val = row["margin_pct"]
        c3.metric(
            "P/Pmax",
            f"{ratio_val:.1%}" if pd.notna(ratio_val) else "N/A",
            delta=f"{margin_val:.1f}% margin" if pd.notna(margin_val) else None,
            delta_color="normal" if pd.notna(ratio_val) and ratio_val < 0.8 else "inverse",
        )
        c4.metric(
            "δ operating",
            f"{row['delta_deg']:.1f}°" if pd.notna(row["delta_deg"]) else "N/A",
        )

        fig = _build_pangle_chart(selected_line, row)
        st.plotly_chart(fig, use_container_width=True)
