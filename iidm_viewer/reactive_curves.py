import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from iidm_viewer.filters import (
    FILTERS,
    build_vl_lookup,
    enrich_with_joins,
    render_filters,
)


def render_reactive_curves(network, selected_vl):
    curves_df = network.get_reactive_capability_curve_points()

    if curves_df.empty:
        st.info("No reactive capability curves found in this network.")
        return

    curve_gen_ids = set(curves_df.index.get_level_values("id").unique())

    # Load generators dataframe and apply filters to narrow the selection
    gens_df = network.get_generators(all_attributes=True)
    # Keep only generators that have a capability curve
    gens_df = gens_df[gens_df.index.isin(curve_gen_ids)]

    if gens_df.empty:
        st.info("No generators with reactive capability curves found.")
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
    st.caption(f"{len(gen_ids)} generators with curves")

    selected_gen = st.selectbox(
        "Generator",
        options=gen_ids,
        key="rcc_generator_select",
    )

    points = curves_df.loc[selected_gen].sort_values("p")

    # Get generator operating point
    gen_row = gens_df.loc[selected_gen] if selected_gen in gens_df.index else None

    if gen_row is not None:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("target_p", f"{gen_row.get('target_p', float('nan')):.1f} MW")
        col2.metric("target_q", f"{gen_row.get('target_q', float('nan')):.1f} MVar")
        col3.metric("min_q at target_p", f"{gen_row.get('min_q_at_target_p', float('nan')):.1f} MVar")
        col4.metric("max_q at target_p", f"{gen_row.get('max_q_at_target_p', float('nan')):.1f} MVar")

    # Build closed polygon: max_q left->right, then min_q right->left
    p_vals = points["p"].values
    min_q = points["min_q"].values
    max_q = points["max_q"].values

    poly_p = list(p_vals) + list(reversed(p_vals)) + [p_vals[0]]
    poly_q = list(max_q) + list(reversed(min_q)) + [max_q[0]]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=poly_p, y=poly_q,
        fill="toself",
        fillcolor="rgba(99, 110, 250, 0.15)",
        line=dict(color="rgb(99, 110, 250)"),
        name="Capability curve",
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

    st.caption(f"{len(points)} curve points for {selected_gen}")
    st.dataframe(points.reset_index(drop=True), use_container_width=True)
