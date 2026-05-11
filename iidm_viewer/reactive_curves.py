import math

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
from iidm_viewer.state import (
    compute_target_v_q_sensitivities,
    compute_target_v_q_sensitivity,
)


_TARGET_TOLERANCE = 0.1

_STATUS_DIAMOND_COLOR = {
    "inside": "green",
    "edge": "orange",
    "outside": "red",
    "n/a": "gray",
}


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


def _polygon_vertices(gen_id, gen_row, curves_df, has_curve):
    """Return closed-polygon ``(polygon_p, polygon_q)`` vertex lists for a gen.

    Curve generators: vertices follow the top boundary L→R then the bottom
    boundary R→L using the curve points sorted by P. Min-max generators: a
    four-vertex rectangle from ``[min_p, max_p] × [min_q, max_q]``. Returns
    ``(None, None)`` when the required bounds are missing.
    """
    if has_curve:
        points = curves_df.loc[gen_id].sort_values("p")
        p_vals = points["p"].tolist()
        min_q = points["min_q"].tolist()
        max_q = points["max_q"].tolist()
    else:
        min_p = gen_row.get("min_p")
        max_p = gen_row.get("max_p")
        q_min = gen_row.get("min_q")
        q_max = gen_row.get("max_q")
        if any(pd.isna(v) for v in (min_p, max_p, q_min, q_max)):
            return None, None
        p_vals = [float(min_p), float(max_p)]
        min_q = [float(q_min), float(q_min)]
        max_q = [float(q_max), float(q_max)]

    if len(p_vals) < 2:
        return None, None

    polygon_p = p_vals + list(reversed(p_vals))
    polygon_q = max_q + list(reversed(min_q))
    return polygon_p, polygon_q


def _signed_distance_to_polygon(tp, tq, polygon_p, polygon_q):
    """Signed Euclidean distance from ``(tp, tq)`` to a closed polygon.

    Negative when the point is inside (magnitude = closest-edge headroom),
    zero on the boundary, positive when outside.
    """
    n = len(polygon_p)
    if n < 3:
        return float("nan")

    min_dist = math.inf
    for i in range(n):
        j = (i + 1) % n
        x1, y1 = polygon_p[i], polygon_q[i]
        x2, y2 = polygon_p[j], polygon_q[j]
        dx, dy = x2 - x1, y2 - y1
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-30:
            d = math.hypot(tp - x1, tq - y1)
        else:
            t = max(0.0, min(1.0, ((tp - x1) * dx + (tq - y1) * dy) / length_sq))
            d = math.hypot(tp - (x1 + t * dx), tq - (y1 + t * dy))
        if d < min_dist:
            min_dist = d

    # Ray casting: shoot horizontally to +x and count crossings.
    inside = False
    for i in range(n):
        j = (i + 1) % n
        yi, yj = polygon_q[i], polygon_q[j]
        if (yi > tq) != (yj > tq):
            x_int = polygon_p[i] + (tq - yi) * (polygon_p[j] - polygon_p[i]) / (yj - yi)
            if tp < x_int:
                inside = not inside

    return -min_dist if inside else min_dist


def classify_targets(gens_df, curves_df, tolerance=_TARGET_TOLERANCE):
    """Classify (target_p, target_q) for each generator vs. its capability polygon.

    Returns the input frame augmented with:

    - ``distance``: signed Euclidean distance from ``(target_p, target_q)``
      to the polygon (positive outside, zero on edge, negative inside =
      headroom). Units: MVA (P in MW, Q in MVar, mixed).
    - ``violation``: L∞ axial overshoot — ``max(0, p_lo - target_p,
      target_p - p_hi, min_q_at_target_p - target_q, target_q -
      max_q_at_target_p)``. Non-negative; complements ``distance`` by
      reporting "how far past the worst-axis bound" without the diagonal
      coupling.
    - ``status``: ``"inside"`` / ``"edge"`` / ``"outside"`` / ``"n/a"`` derived
      from ``distance`` and ``tolerance``.
    - ``regulation``: ``"PV"`` (voltage_regulator_on), ``"PQ"`` (off + target_q),
      ``"?"`` otherwise.
    - ``lf_action``: ``"PV→PQ"`` for PV generators outside their diagram (the
      ones the load flow will switch); empty string otherwise.
    - ``p_lo`` / ``p_hi``: diagnostic P bounds (curve extremes when present,
      else min_p / max_p).
    """
    needed = ["target_p", "target_q", "min_p", "max_p",
              "min_q_at_target_p", "max_q_at_target_p",
              "voltage_regulator_on"]
    df = gens_df.reindex(columns=needed).copy()

    if not curves_df.empty:
        curve_p_range = (
            curves_df.groupby(level="id")["p"]
            .agg(["min", "max"])
            .rename(columns={"min": "p_lo", "max": "p_hi"})
        )
        df = df.join(curve_p_range, how="left")
        curve_gen_ids_set = set(curves_df.index.get_level_values("id"))
    else:
        df["p_lo"] = float("nan")
        df["p_hi"] = float("nan")
        curve_gen_ids_set = set()

    df["p_lo"] = df["p_lo"].fillna(df["min_p"])
    df["p_hi"] = df["p_hi"].fillna(df["max_p"])

    # L∞ axial overshoot — non-negative; 0 when on / inside the bounding box
    # of the polygon. Useful diagnostic: ``violation`` tells you how far the
    # target exceeds the bounds on the worst axis, while ``distance`` (below)
    # is the true Euclidean distance for ranking.
    axial = pd.concat(
        [
            df["p_lo"] - df["target_p"],
            df["target_p"] - df["p_hi"],
            df["min_q_at_target_p"] - df["target_q"],
            df["target_q"] - df["max_q_at_target_p"],
        ],
        axis=1,
    )
    max_axial = axial.max(axis=1)
    df["violation"] = max_axial.where(max_axial > 0, 0.0)

    # Signed Euclidean distance from (target_p, target_q) to each gen's polygon.
    # Loop in Python: ~O(N_gens * N_vertices_per_polygon); curves have ≤ ~20
    # points so the cost stays under a few ms even for a thousand generators.
    distances = pd.Series(float("nan"), index=df.index, dtype="float64")
    for gen_id in df.index:
        tp = df.at[gen_id, "target_p"]
        tq = df.at[gen_id, "target_q"]
        if pd.isna(tp) or pd.isna(tq):
            continue
        has_curve = gen_id in curve_gen_ids_set
        poly_p, poly_q = _polygon_vertices(
            gen_id, gens_df.loc[gen_id], curves_df, has_curve
        )
        if poly_p is None:
            continue
        distances.at[gen_id] = _signed_distance_to_polygon(
            float(tp), float(tq), poly_p, poly_q
        )
    df["distance"] = distances

    status = pd.Series("inside", index=df.index, dtype="object")
    status[distances.abs() <= tolerance] = "edge"
    status[distances > tolerance] = "outside"
    status[distances.isna()] = "n/a"
    df["status"] = status

    regulator_on = df["voltage_regulator_on"].fillna(False).astype(bool)
    has_target_q = df["target_q"].notna()
    regulation = pd.Series("?", index=df.index, dtype="object")
    regulation[regulator_on] = "PV"
    regulation[~regulator_on & has_target_q] = "PQ"
    df["regulation"] = regulation

    lf_action = pd.Series("", index=df.index, dtype="object")
    lf_action[(status == "outside") & (regulation == "PV")] = "PV→PQ"
    df["lf_action"] = lf_action

    return df


def _render_target_containment_summary(classified, gens_df):
    n_inside = int((classified["status"] == "inside").sum())
    n_edge = int((classified["status"] == "edge").sum())
    n_outside = int((classified["status"] == "outside").sum())
    n_na = int((classified["status"] == "n/a").sum())

    outside_mask = classified["status"] == "outside"
    n_outside_pv = int((outside_mask & (classified["regulation"] == "PV")).sum())
    n_outside_pq = int((outside_mask & (classified["regulation"] == "PQ")).sum())

    label = f"Target P/Q containment — {n_outside} outside, {n_edge} on edge"
    with st.expander(label, expanded=(n_outside + n_edge > 0)):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Inside", n_inside)
        c2.metric("On edge", n_edge)
        c3.metric(
            "Outside",
            n_outside,
            delta=f"{n_outside_pv} PV → PQ" if n_outside_pv else None,
            delta_color="inverse",
        )
        c4.metric("Unknown", n_na)

        if n_outside_pv or n_outside_pq:
            st.caption(
                f"Of the {n_outside} outside: {n_outside_pv} PV "
                f"(will switch to PQ in load flow — see the **lf_action** "
                f"column below), {n_outside_pq} PQ, "
                f"{n_outside - n_outside_pv - n_outside_pq} other."
            )

        issues = classified[classified["status"].isin(["outside", "edge"])]
        if issues.empty:
            st.success("All targets are inside their capability curves.")
            return

        # Sort priority: outside-PV (will switch) > outside-PQ/other > edge,
        # then within each group by signed distance descending (farthest from
        # the diagram first).
        is_switcher = (
            (issues["status"] == "outside") & (issues["lf_action"] == "PV→PQ")
        )
        sort_key = pd.Series(2, index=issues.index)
        sort_key[issues["status"] == "outside"] = 1
        sort_key[is_switcher] = 0
        issues = (
            issues.assign(_order=sort_key)
            .sort_values(["_order", "distance"], ascending=[True, False])
            .drop(columns="_order")
        )

        extra = [c for c in ("voltage_level_id", "nominal_v", "country")
                 if c in gens_df.columns]
        if extra:
            issues = issues.join(gens_df[extra], how="left")

        cols = extra + [
            "status", "regulation", "lf_action", "distance", "violation",
            "target_p", "target_q",
            "p_lo", "p_hi", "min_q_at_target_p", "max_q_at_target_p",
        ]
        st.dataframe(issues[cols], use_container_width=True)


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

    classified = classify_targets(gens_df, curves_df)

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
                    color=_STATUS_DIAMOND_COLOR.get(status, "green"),
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
