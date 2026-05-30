"""Streamlit "Pmax Visualization" tab.

For each transmission line: ``Pmax = V1 × V2 / X`` (MW, with V in kV,
X in Ω). The operating ratio ``P/Pmax = sin(δ)`` reveals how close
the network is to the steady-state stability limit
(δ → 90°  ⟹  P → Pmax).

The maths + Plotly chart live in the framework-agnostic
:mod:`iidm_viewer.pmax_visualization` module so the PySide6 and
NiceGUI prototypes can build their own UI on top. This file holds
only the Streamlit rendering glue.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from iidm_viewer.pmax_visualization import (
    build_display_dataframe,
    build_pangle_chart,
    compute_pmax_data,
    filter_by_vl,
    margin_color,
    ratio_color,
)


_COLOR_CSS: dict = {
    "warning": "background-color: #ff4b4b; color: white",
    "caution": "background-color: #ffa500",
    "safe": "",
    "unknown": "",
}


def _style_ratio(val):
    return _COLOR_CSS[ratio_color(val)]


def _style_margin(val):
    return _COLOR_CSS[margin_color(val)]


def render_pmax_visualization(network, selected_vl):
    st.caption(
        "For each line: **Pmax = V₁ × V₂ / X**  (V in kV, X in Ω, result in MW). "
        "The ratio **P/Pmax = sin(δ)** shows proximity to the steady-state "
        "stability limit — the operating point reaches the limit when δ = 90°."
    )

    pmax_df = compute_pmax_data(network)

    if pmax_df.empty:
        st.info(
            "No data available. Make sure a load flow has been run and the "
            "network contains transmission lines."
        )
        return

    # Optional VL filter
    if selected_vl:
        vl_subset = filter_by_vl(pmax_df, selected_vl)
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

    show = build_display_dataframe(pmax_df)
    styled = (
        show.style
        .map(_style_ratio, subset=["P/Pmax"])
        .map(_style_margin, subset=["Margin (%)"])
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

        fig = build_pangle_chart(selected_line, row)
        st.plotly_chart(fig, use_container_width=True)
