import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from iidm_viewer.caches import get_generators_all, get_reactive_curve_points
from iidm_viewer.filters import (
    FILTERS,
    build_vl_lookup,
    enrich_with_joins,
    render_filters,
)


def render_reactive_curves(network, selected_vl):
    curves_df = get_reactive_curve_points(network)
    curve_gen_ids = set(
        curves_df.index.get_level_values("id").unique()
    ) if not curves_df.empty else set()

    # Load all generators — those with curves and those with min/max limits
    gens_df = get_generators_all(network)

    # Keep generators that have either a curve or finite min/max reactive limits
    has_curve = gens_df.index.isin(curve_gen_ids)
    has_minmax = (
        gens_df["min_q"].abs() < 1e300
    ) & (
        gens_df["max_q"].abs() < 1e300
    )
    gens_df = gens_df[has_curve | has_minmax]

    if gens_df.empty:
        st.info("No generators with reactive limits found.")
        return

    gens_df = enrich_with_joins(gens_df, build_vl_lookup(network))

    # If a VL is selected, optionally filter
    if selected_vl and "voltage_level_id" in gens_df.columns:
        vl_gens = gens_df[gens_df["voltage_level_id"] == selected_vl]
        if not vl_gens.empty:
            only_vl = st.checkbox(
                f"Only generators in VL {selected_vl}",
                value=False,
                key="rcc_only_vl",
            )
            if only_vl:
                gens_df = vl_gens

    gens_df = render_filters(
        gens_df, FILTERS.get("Generators", []), key_prefix="rcc_flt"
    )

    if gens_df.empty:
        st.info("No generators match the current filters.")
        return

    gen_ids = gens_df.index.tolist()
    st.caption(f"{len(gen_ids)} generators with reactive limits")

    selected_gen = st.selectbox(
        "Generator",
        options=gen_ids,
        key="rcc_generator_select",
    )

    # Get generator operating point
    gen_row = gens_df.loc[selected_gen] if selected_gen in gens_df.index else None

    if gen_row is not None:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("target_p", f"{gen_row.get('target_p', float('nan')):.1f} MW")
        col2.metric("target_q", f"{gen_row.get('target_q', float('nan')):.1f} MVar")
        col3.metric("min_q at target_p", f"{gen_row.get('min_q_at_target_p', float('nan')):.1f} MVar")
        col4.metric("max_q at target_p", f"{gen_row.get('max_q_at_target_p', float('nan')):.1f} MVar")

    has_curve_points = selected_gen in curve_gen_ids

    if has_curve_points:
        points = curves_df.loc[selected_gen].sort_values("p")
        p_vals = points["p"].values
        min_q = points["min_q"].values
        max_q = points["max_q"].values
        curve_label = "Capability curve"
    else:
        # Min-max type: build a rectangle from min_p, max_p, min_q, max_q
        min_p = float(gen_row.get("min_p", 0))
        max_p = float(gen_row.get("max_p", 0))
        q_min = float(gen_row.get("min_q", 0))
        q_max = float(gen_row.get("max_q", 0))
        p_vals = [min_p, max_p]
        min_q = [q_min, q_min]
        max_q = [q_max, q_max]
        curve_label = "Min-max reactive limits"

    # Build closed polygon: max_q left->right, then min_q right->left
    poly_p = list(p_vals) + list(reversed(p_vals)) + [p_vals[0]]
    poly_q = list(max_q) + list(reversed(min_q)) + [max_q[0]]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=poly_p, y=poly_q,
        fill="toself",
        fillcolor="rgba(99, 110, 250, 0.15)",
        line=dict(color="rgb(99, 110, 250)"),
        name=curve_label,
    ))

    # Plot operating point if p and q are available
    if gen_row is not None:
        op_p = gen_row.get("p")
        op_q = gen_row.get("q")
        if pd.notna(op_p) and pd.notna(op_q):
            # pypowsybl convention: p is negative for generation
            fig.add_trace(go.Scatter(
                x=[float(-op_p)], y=[float(-op_q)],
                mode="markers",
                marker=dict(size=12, color="red", symbol="x"),
                name=f"Operating (P={-op_p:.1f}, Q={-op_q:.1f})",
            ))

        target_p = gen_row.get("target_p")
        target_q = gen_row.get("target_q")
        if pd.notna(target_p) and pd.notna(target_q):
            fig.add_trace(go.Scatter(
                x=[float(target_p)], y=[float(target_q)],
                mode="markers",
                marker=dict(size=12, color="green", symbol="diamond"),
                name=f"Target (P={target_p:.1f}, Q={target_q:.1f})",
            ))

    fig.update_layout(
        xaxis_title="P (MW)",
        yaxis_title="Q (MVar)",
        title=f"Reactive Capability Curve — {selected_gen}",
        showlegend=True,
        height=500,
    )
    st.plotly_chart(fig, use_container_width=True)

    if has_curve_points:
        st.caption(f"{len(points)} curve points for {selected_gen}")
        st.dataframe(points.reset_index(drop=True), use_container_width=True)
    else:
        st.caption(f"Min-max reactive limits for {selected_gen}")
