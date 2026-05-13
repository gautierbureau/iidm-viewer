"""Streamlit "Reactive Capability Curves" tab.

The math, classification and pypowsybl integration live in the
framework-agnostic :mod:`iidm_viewer.reactive_curves` module so the
PySide6 and NiceGUI prototypes can build their own UI on top. This
file only holds the Streamlit rendering glue + per-session caching
wrappers.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from iidm_viewer.caches import (
    _net_key,
    get_2wt_all,
    get_bus_voltages,
    get_generators_all,
    get_reactive_curve_points,
)
from iidm_viewer.filters import (
    FILTERS,
    build_vl_lookup,
    enrich_with_joins,
    render_filters,
)
from iidm_viewer.reactive_curves import (
    STATUS_DIAMOND_COLOR,
    add_bus_voltage_columns,
    augment_gens_with_step_up_transformer,
    classify_targets,
    vl_to_step_up_transformer_table,
)
from iidm_viewer.state import (
    compute_target_v_q_sensitivities,
    compute_target_v_q_sensitivity,
)


# ---------------------------------------------------------------------------
# Streamlit-cached wrappers around the shared compute. Caches are keyed
# by ``_net_key(network)`` so topology edits (and the load-flow path,
# which pops the topology set) invalidate them automatically.
# ---------------------------------------------------------------------------
def _vl_to_step_up_transformer_cached(network):
    """Cache the VL → step-up transformer table by ``net_key``."""
    cache_key = _net_key(network)
    cached = st.session_state.get("_rcc_vl_to_xf_cache")
    if cached is not None and cached["key"] == cache_key:
        return cached["df"]

    twts = get_2wt_all(network)
    if not twts.empty:
        twts = enrich_with_joins(twts.copy(), build_vl_lookup(network))
    df = vl_to_step_up_transformer_table(twts)
    st.session_state["_rcc_vl_to_xf_cache"] = {"key": cache_key, "df": df}
    return df


def _augment_gens_with_step_up_transformer(network, gens_df):
    """Streamlit-cached wrapper around the shared augment helper."""
    return augment_gens_with_step_up_transformer(
        network, gens_df,
        vl_to_xf=_vl_to_step_up_transformer_cached(network),
    )


def _augment_gens_with_bus_voltage(network, gens_df):
    """Thin wrapper that pulls the post-LF bus voltages from the cached
    ``get_bus_voltages`` getter and applies the shared pure join."""
    return add_bus_voltage_columns(gens_df, get_bus_voltages(network))


def _classify_targets_cached(network, gens_df, curves_df):
    """Cached wrapper around ``classify_targets``.

    Key: ``(net_key, lf_gen, tuple(gens_df.index))``. The classification
    depends only on which generators are displayed and their cached
    ``get_generators`` values, so a selectbox-only rerun reuses the result.
    Invalidated on every load flow via ``_LOAD_FLOW_CACHE_KEYS``.
    """
    key = (
        _net_key(network),
        st.session_state.get("_lf_gen", 0),
        tuple(gens_df.index),
    )
    cached = st.session_state.get("_rcc_classified_cache")
    if cached is not None and cached["key"] == key:
        return cached["df"]
    classified = classify_targets(gens_df, curves_df)
    st.session_state["_rcc_classified_cache"] = {"key": key, "df": classified}
    return classified


# ---------------------------------------------------------------------------
# Streamlit render functions
# ---------------------------------------------------------------------------
def _render_target_v_sensitivity(gen_row, classified_row, gen_id, network):
    if not bool(gen_row.get("voltage_regulator_on", False)):
        return

    sens = compute_target_v_q_sensitivity(network, gen_id)
    if sens is None:
        st.caption(
            "AC sensitivity dQ/dV could not be computed for this generator."
        )
        return

    dq_dv, q_ref = sens
    target_v = gen_row.get("target_v")
    target_q = gen_row.get("target_q")
    min_q = classified_row.get("min_q_at_target_p")
    max_q = classified_row.get("max_q_at_target_p")

    pieces = [
        f"**dQ_bus / dV_target ≈ {dq_dv:+.2f} MVar/kV** "
        f"(BUS_REACTIVE_POWER ref = {q_ref:.2f} MVar)."
    ]

    if (
        abs(dq_dv) > 1e-3
        and pd.notna(target_v) and pd.notna(target_q)
        and pd.notna(min_q) and pd.notna(max_q)
    ):
        q_mid = 0.5 * (float(min_q) + float(max_q))
        delta_v = (q_mid - float(target_q)) / dq_dv
        new_target_v = float(target_v) + delta_v
        pieces.append(
            f"To shift Q toward the band midpoint "
            f"(Q_mid = {q_mid:.1f} MVar from current target_q = {float(target_q):.1f}), "
            f"the linearization suggests **Δtarget_v ≈ {delta_v:+.3f} kV** "
            f"⇒ new target_v ≈ **{new_target_v:.3f} kV** "
            f"(current target_v = {float(target_v):.3f} kV)."
        )

    st.caption(" ".join(pieces))


def _render_target_containment_summary(classified, gens_df):
    n_inside = int((classified["status"] == "inside").sum())
    n_warning = int(
        classified["status"].isin(["edge", "near_saturation"]).sum()
    )
    n_action = int(classified["status"].isin(["outside", "saturated"]).sum())
    n_unknown = int(classified["status"].isin(["n/a", "needs_lf"]).sum())
    n_saturated = int((classified["status"] == "saturated").sum())
    n_needs_lf = int((classified["status"] == "needs_lf").sum())

    label = (
        f"Target P/Q containment — {n_action} action, {n_warning} warning"
    )
    with st.expander(label, expanded=(n_action + n_warning > 0)):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Inside", n_inside)
        c2.metric("Edge / Near", n_warning)
        c3.metric(
            "Outside / Saturated",
            n_action,
            delta=f"{n_saturated} PV → PQ" if n_saturated else None,
            delta_color="inverse",
        )
        c4.metric("Unknown / Needs LF", n_unknown)

        if n_needs_lf:
            st.caption(
                f"{n_needs_lf} PV generator(s) need a load flow to evaluate "
                "their operating point against the diagram (the post-LF "
                "``q`` is required to test PV gens against their Q limits)."
            )

        issues = classified[
            classified["status"].isin(
                ["outside", "saturated", "edge", "near_saturation"]
            )
        ]
        if issues.empty:
            st.success("All targets are inside their capability curves.")
            return

        extra = [c for c in ("voltage_level_id", "nominal_v", "country")
                 if c in gens_df.columns]
        gen_attrs = [c for c in (
            "regulated_element_id", "connected",
            "step_up_transformer_id", "step_up_transformer_connected",
        ) if c in gens_df.columns]
        v_attrs = [c for c in ("target_v", "v_bus", "v_target_gap")
                   if c in gens_df.columns]
        join_cols = extra + gen_attrs + v_attrs
        if join_cols:
            issues = issues.join(gens_df[join_cols], how="left")

        cols = extra + [
            "status", "regulation", "lf_action", "distance", "violation",
        ] + gen_attrs + [
            "target_p", "target_q",
        ] + v_attrs + [
            "p_lo", "p_hi", "min_q_at_target_p", "max_q_at_target_p",
        ]

        def _subset(status_val, regulation_val):
            sub = issues[
                (issues["status"] == status_val)
                & (issues["regulation"] == regulation_val)
            ]
            if sub.empty:
                return sub
            # Push generators dispatched at zero MW to the end: their
            # diagram violation is often a side effect of the step-up
            # transformer being out of service rather than a real Q issue.
            is_zero_p = (sub["target_p"] == 0).astype(int)
            return (
                sub.assign(_zero_p=is_zero_p)
                .sort_values(["_zero_p", "distance"], ascending=[True, False])
                .drop(columns="_zero_p")
            )

        pq_out = _subset("outside", "PQ")
        pv_sat = _subset("saturated", "PV")
        pq_edge = _subset("edge", "PQ")
        pv_near = _subset("near_saturation", "PV")

        # Action-required subsets first, rendered inline & expanded.
        if not pq_out.empty:
            st.markdown(
                f"**PQ outside — {len(pq_out)}** "
                "(target_q infeasible at this target_p)"
            )
            st.dataframe(pq_out[cols], use_container_width=True)
        if not pv_sat.empty:
            st.markdown(
                f"**PV saturated — {len(pv_sat)}** "
                "(load flow clamped Q and switched to PQ)"
            )
            st.dataframe(pv_sat[cols], use_container_width=True)

        # Warning subsets — secondary, collapsed by default.
        if not pq_edge.empty:
            with st.expander(f"PQ on edge — {len(pq_edge)}", expanded=False):
                st.dataframe(pq_edge[cols], use_container_width=True)
        if not pv_near.empty:
            with st.expander(
                f"PV near saturation — {len(pv_near)}", expanded=False
            ):
                st.dataframe(pv_near[cols], use_container_width=True)


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

    gens_df = _augment_gens_with_step_up_transformer(network, gens_df)
    gens_df = _augment_gens_with_bus_voltage(network, gens_df)

    classified = _classify_targets_cached(network, gens_df, curves_df)

    # Warm the sensitivity cache for every displayed PV generator in one
    # batched AC sensitivity call. Without this, each selectbox change
    # below pays for a fresh single-gen AC sensitivity (one LF factorization
    # per generator). With it, the factorization is shared and subsequent
    # selections hit the per-gen cache.
    pv_gen_ids = classified.index[classified["regulation"] == "PV"].tolist()
    if pv_gen_ids:
        compute_target_v_q_sensitivities(network, pv_gen_ids)

    selected_gen = st.selectbox(
        "Generator",
        options=gen_ids,
        key="rcc_generator_select",
    )

    # Get generator operating point
    gen_row = gens_df.loc[selected_gen] if selected_gen in gens_df.index else None

    if gen_row is not None:
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("target_p", f"{gen_row.get('target_p', float('nan')):.1f} MW")
        col2.metric("target_q", f"{gen_row.get('target_q', float('nan')):.1f} MVar")
        col3.metric("min_q at target_p", f"{gen_row.get('min_q_at_target_p', float('nan')):.1f} MVar")
        col4.metric("max_q at target_p", f"{gen_row.get('max_q_at_target_p', float('nan')):.1f} MVar")
        classified_row = (
            classified.loc[selected_gen]
            if selected_gen in classified.index
            else pd.Series(dtype="object")
        )
        col5.metric("Type", classified_row.get("regulation", "?"))

        _render_target_v_sensitivity(gen_row, classified_row, selected_gen, network)

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
            status = classified_row.get("status", "n/a")
            regulation = classified_row.get("regulation", "?")
            fig.add_trace(go.Scatter(
                x=[float(target_p)], y=[float(target_q)],
                mode="markers",
                marker=dict(
                    size=12,
                    color=STATUS_DIAMOND_COLOR.get(status, "green"),
                    symbol="diamond",
                ),
                name=(
                    f"Target [{regulation}] (P={target_p:.1f}, "
                    f"Q={target_q:.1f}, {status})"
                ),
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

    _render_target_containment_summary(classified, gens_df)
