"""Streamlit "Reactive Capability Curves" tab.

The math, classification and pypowsybl integration live in the
framework-agnostic :mod:`iidm_viewer.reactive_curves` module so the
PySide6 and NiceGUI prototypes can build their own UI on top. This
file only holds the Streamlit rendering glue + per-session caching
wrappers around the shared composer.
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
    build_containment_summary,
    build_generator_plot_data,
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
    """Streamlit render of the shared :class:`ContainmentSummary`."""
    summary = build_containment_summary(classified, gens_df)

    label = (
        f"Target P/Q containment — {summary.n_action} action, "
        f"{summary.n_warning} warning"
    )
    with st.expander(label, expanded=(summary.n_action + summary.n_warning > 0)):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Inside", summary.n_inside)
        c2.metric("Edge / Near", summary.n_warning)
        c3.metric(
            "Outside / Saturated",
            summary.n_action,
            delta=f"{summary.n_saturated} PV → PQ" if summary.n_saturated else None,
            delta_color="inverse",
        )
        c4.metric("Unknown / Needs LF", summary.n_unknown)

        if summary.n_needs_lf:
            st.caption(
                f"{summary.n_needs_lf} PV generator(s) need a load flow to evaluate "
                "their operating point against the diagram (the post-LF "
                "``q`` is required to test PV gens against their Q limits)."
            )

        if summary.n_action + summary.n_warning == 0:
            st.success("All targets are inside their capability curves.")
            return

        if not summary.pq_outside.empty:
            st.markdown(
                f"**PQ outside — {len(summary.pq_outside)}** "
                "(target_q infeasible at this target_p)"
            )
            st.dataframe(summary.pq_outside, use_container_width=True)
        if not summary.pv_saturated.empty:
            st.markdown(
                f"**PV saturated — {len(summary.pv_saturated)}** "
                "(load flow clamped Q and switched to PQ)"
            )
            st.dataframe(summary.pv_saturated, use_container_width=True)

        if not summary.pq_edge.empty:
            with st.expander(
                f"PQ on edge — {len(summary.pq_edge)}", expanded=False,
            ):
                st.dataframe(summary.pq_edge, use_container_width=True)
        if not summary.pv_near_saturation.empty:
            with st.expander(
                f"PV near saturation — {len(summary.pv_near_saturation)}",
                expanded=False,
            ):
                st.dataframe(summary.pv_near_saturation, use_container_width=True)


def _build_plotly_figure(plot_data, gen_id):
    """Translate a shared :class:`GeneratorPlotData` into a Plotly figure."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=plot_data.polygon_p, y=plot_data.polygon_q,
        fill="toself",
        fillcolor="rgba(99, 110, 250, 0.15)",
        line=dict(color="rgb(99, 110, 250)"),
        name=plot_data.curve_label,
    ))

    if plot_data.operating_point is not None:
        op_p, op_q = plot_data.operating_point
        fig.add_trace(go.Scatter(
            x=[op_p], y=[op_q],
            mode="markers",
            marker=dict(size=12, color="red", symbol="x"),
            name=f"Operating (P={op_p:.1f}, Q={op_q:.1f})",
        ))

    if plot_data.target_point is not None:
        target_p, target_q, status, regulation = plot_data.target_point
        fig.add_trace(go.Scatter(
            x=[target_p], y=[target_q],
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
        title=f"Reactive Capability Curve — {gen_id}",
        showlegend=True,
        height=500,
    )
    return fig


def render_reactive_curves(network, selected_vl):
    """Streamlit "Reactive Capability Curves" tab body.

    Composes the shared view-model + plot-data helpers with Streamlit
    widgets. Caching wrappers above keep per-rerun cost down.
    """
    curves_df = get_reactive_curve_points(network)
    curve_gen_ids = set(
        curves_df.index.get_level_values("id").unique()
    ) if not curves_df.empty else set()

    gens_df = get_generators_all(network)

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

    # Augment + classify via the shared helpers; keep the Streamlit
    # caches alive by passing pre-fetched / pre-computed frames.
    from iidm_viewer.reactive_curves import (
        augment_gens_with_bus_voltage,
        augment_gens_with_step_up_transformer,
    )
    gens_df = augment_gens_with_step_up_transformer(
        network, gens_df, vl_to_xf=_vl_to_step_up_transformer_cached(network),
    )
    gens_df = augment_gens_with_bus_voltage(
        network, gens_df, bus_voltages=get_bus_voltages(network),
    )
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

    gen_row = gens_df.loc[selected_gen] if selected_gen in gens_df.index else None
    classified_row = (
        classified.loc[selected_gen]
        if selected_gen in classified.index
        else pd.Series(dtype="object")
    )

    if gen_row is not None:
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("target_p", f"{gen_row.get('target_p', float('nan')):.1f} MW")
        col2.metric("target_q", f"{gen_row.get('target_q', float('nan')):.1f} MVar")
        col3.metric("min_q at target_p", f"{gen_row.get('min_q_at_target_p', float('nan')):.1f} MVar")
        col4.metric("max_q at target_p", f"{gen_row.get('max_q_at_target_p', float('nan')):.1f} MVar")
        col5.metric("Type", classified_row.get("regulation", "?"))

        _render_target_v_sensitivity(gen_row, classified_row, selected_gen, network)

    plot_data = build_generator_plot_data(
        selected_gen, gens_df, curves_df, classified, curve_gen_ids,
    )
    if plot_data is not None:
        fig = _build_plotly_figure(plot_data, selected_gen)
        st.plotly_chart(fig, use_container_width=True)

        if plot_data.has_curve and plot_data.curve_points is not None:
            st.caption(f"{len(plot_data.curve_points)} curve points for {selected_gen}")
            st.dataframe(
                plot_data.curve_points.reset_index(drop=True),
                use_container_width=True,
            )
        else:
            st.caption(f"Min-max reactive limits for {selected_gen}")

    _render_target_containment_summary(classified, gens_df)
