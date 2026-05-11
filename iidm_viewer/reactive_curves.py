import math

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

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
from iidm_viewer.state import (
    compute_target_v_q_sensitivities,
    compute_target_v_q_sensitivity,
)


_TARGET_TOLERANCE = 0.1
_NEAR_SATURATION_THRESHOLD = 5.0  # MVar/MW; PV gens within this distance of a Q limit are flagged "near_saturation"

_STATUS_DIAMOND_COLOR = {
    "inside": "green",
    "edge": "orange",
    "outside": "red",
    "saturated": "red",          # PV gen at limit — load flow switched to PQ
    "near_saturation": "orange", # PV gen close to a Q limit
    "needs_lf": "gray",
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


def classify_targets(gens_df, curves_df, tolerance=_TARGET_TOLERANCE,
                     near_saturation_threshold=_NEAR_SATURATION_THRESHOLD):
    """Classify each generator's operating point against its capability polygon.

    The reference point depends on regulation type:

    - **PQ** gens (regulator off, ``target_q`` set): use the setpoint
      ``(target_p, target_q)`` — the load flow honours it. Statuses are
      ``inside`` / ``edge`` / ``outside`` / ``n/a``.
    - **PV** gens (regulator on): use the post-LF operating point
      ``(target_p, -q)`` — the LF picks Q to hold ``target_v``, so the
      Q the diagram should be checked against is the one the LF actually
      delivered. Statuses are ``inside`` / ``near_saturation`` /
      ``saturated`` / ``needs_lf``. ``saturated`` captures the case where
      the LF clamped Q at a limit and (silently) demoted the gen to PQ.
      ``needs_lf`` is set when ``q`` is NaN (no load flow has run).

    Other columns on the result:

    - ``distance``: signed Euclidean distance from the reference point to
      the polygon (positive outside, zero on edge, negative inside =
      headroom). Units: MVA (P in MW, Q in MVar, mixed).
    - ``violation``: L∞ axial overshoot using the same reference point.
    - ``regulation``: ``"PV"`` / ``"PQ"`` / ``"?"``.
    - ``lf_action``: ``"PV→PQ"`` exactly when ``status == "saturated"``,
      i.e. the LF itself converted the gen to PQ. This is the
      ground-truth list rather than a guess from ``target_q``.
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

    regulator_on = df["voltage_regulator_on"].fillna(False).astype(bool)
    # Post-LF Q in load convention. NaN when no LF has run — yields
    # status "needs_lf" for PV gens further down.
    q_lf = gens_df.reindex(columns=["q"])["q"]

    # Reference point for the polygon check, in gen convention.
    # PQ: target_q (setpoint the LF honours).
    # PV: -q (Q the LF actually produced to hold target_v).
    check_q = df["target_q"].where(~regulator_on, -q_lf)

    # L∞ axial overshoot using the same reference point.
    axial = pd.concat(
        [
            df["p_lo"] - df["target_p"],
            df["target_p"] - df["p_hi"],
            df["min_q_at_target_p"] - check_q,
            check_q - df["max_q_at_target_p"],
        ],
        axis=1,
    )
    max_axial = axial.max(axis=1)
    df["violation"] = max_axial.where(max_axial > 0, 0.0)

    # Signed Euclidean distance from the reference point to the polygon.
    distances = pd.Series(float("nan"), index=df.index, dtype="float64")
    for gen_id in df.index:
        tp = df.at[gen_id, "target_p"]
        tq = check_q.at[gen_id]
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

    abs_d = distances.abs()
    nan_mask = distances.isna()
    status = pd.Series("inside", index=df.index, dtype="object")

    # PQ classification: inside / edge / outside / n/a.
    pq = ~regulator_on
    status[pq & nan_mask] = "n/a"
    status[pq & ~nan_mask & (distances > tolerance)] = "outside"
    status[pq & ~nan_mask & (abs_d <= tolerance)] = "edge"

    # PV classification: inside / near_saturation / saturated / needs_lf.
    # ``saturated`` covers "at limit OR past limit" — for a converged LF the
    # latter cannot happen, but we lump them together so PV never appears
    # with an "outside" status.
    pv = regulator_on
    status[pv & nan_mask] = "needs_lf"
    status[pv & ~nan_mask & (distances >= -tolerance)] = "saturated"
    near_sat = (
        pv & ~nan_mask
        & (distances < -tolerance)
        & (distances >= -near_saturation_threshold)
    )
    status[near_sat] = "near_saturation"

    df["status"] = status

    has_target_q = df["target_q"].notna()
    regulation = pd.Series("?", index=df.index, dtype="object")
    regulation[regulator_on] = "PV"
    regulation[~regulator_on & has_target_q] = "PQ"
    df["regulation"] = regulation

    # ``saturated`` only applies to PV gens by construction → those are the
    # ground-truth PV→PQ switches reported by the LF itself.
    lf_action = pd.Series("", index=df.index, dtype="object")
    lf_action[status == "saturated"] = "PV→PQ"
    df["lf_action"] = lf_action

    return df


def _vl_to_step_up_transformer_table(twts_enriched):
    """Pure-pandas helper: from an enriched 2WT frame, return a table indexed
    by voltage_level_id with the step-up transformer for that VL — i.e. the
    2WT whose *other* side has the highest nominal voltage.

    Columns of the result: ``step_up_transformer_id`` (str) and
    ``step_up_transformer_connected`` (bool, = ``connected1 AND connected2``).
    """
    empty = pd.DataFrame(
        columns=["step_up_transformer_id", "step_up_transformer_connected"]
    )
    if twts_enriched.empty:
        return empty
    needed = {"voltage_level1_id", "voltage_level2_id",
              "connected1", "connected2", "nominal_v1", "nominal_v2"}
    if not needed.issubset(twts_enriched.columns):
        return empty

    df = twts_enriched.reset_index().rename(columns={"id": "transformer_id"})
    connected_both = (
        df["connected1"].fillna(False).astype(bool)
        & df["connected2"].fillna(False).astype(bool)
    )
    side1 = pd.DataFrame({
        "voltage_level_id": df["voltage_level1_id"],
        "transformer_id": df["transformer_id"],
        "connected": connected_both,
        "other_nv": df["nominal_v2"],
    })
    side2 = pd.DataFrame({
        "voltage_level_id": df["voltage_level2_id"],
        "transformer_id": df["transformer_id"],
        "connected": connected_both,
        "other_nv": df["nominal_v1"],
    })
    both = pd.concat([side1, side2], ignore_index=True)
    best = (
        both.sort_values("other_nv", ascending=False)
        .drop_duplicates("voltage_level_id", keep="first")
        .set_index("voltage_level_id")
    )
    return best[["transformer_id", "connected"]].rename(columns={
        "transformer_id": "step_up_transformer_id",
        "connected": "step_up_transformer_connected",
    })


def _vl_to_step_up_transformer_cached(network):
    """Cache the VL → step-up transformer table by ``net_key``.

    Registered in ``caches._TOPOLOGY_CACHE_KEYS`` so topology edits (and
    load flow, since that pops the topology set too) refresh the table.
    """
    cache_key = _net_key(network)
    cached = st.session_state.get("_rcc_vl_to_xf_cache")
    if cached is not None and cached["key"] == cache_key:
        return cached["df"]

    twts = get_2wt_all(network)
    if not twts.empty:
        twts = enrich_with_joins(twts.copy(), build_vl_lookup(network))
    df = _vl_to_step_up_transformer_table(twts)
    st.session_state["_rcc_vl_to_xf_cache"] = {"key": cache_key, "df": df}
    return df


def _augment_gens_with_step_up_transformer(network, gens_df):
    """Add ``step_up_transformer_id`` / ``step_up_transformer_connected`` to
    a generators frame, mapped via the gen's ``voltage_level_id``.
    """
    if "voltage_level_id" not in gens_df.columns:
        return gens_df
    vl_map = _vl_to_step_up_transformer_cached(network)
    if vl_map.empty:
        return gens_df.assign(
            step_up_transformer_id=pd.Series(pd.NA, index=gens_df.index, dtype="object"),
            step_up_transformer_connected=pd.Series(pd.NA, index=gens_df.index, dtype="object"),
        )
    vl_series = gens_df["voltage_level_id"]
    return gens_df.assign(
        step_up_transformer_id=vl_series.map(vl_map["step_up_transformer_id"]),
        step_up_transformer_connected=vl_series.map(vl_map["step_up_transformer_connected"]),
    )


def _add_bus_voltage_columns(gens_df, bus_voltages):
    """Pure helper: add ``v_bus`` and ``v_target_gap`` to ``gens_df``.

    ``v_bus`` is the bus voltage (kV) at the gen's terminal bus from the
    post-LF ``bus_voltages`` frame. ``v_target_gap = target_v - v_bus`` —
    for a PV gen successfully regulating, the gap is ~0. A non-zero gap
    is the load flow telling us the regulation failed:

    - ``v_target_gap > 0``: bus settled below target_v. The LF wanted
      more Q production than the gen could supply; clamped at max_q.
    - ``v_target_gap < 0``: bus settled above target_v. The LF wanted
      more Q absorption than allowed; clamped at min_q.

    Required input columns: ``gens_df`` needs ``bus_id`` and ``target_v``;
    ``bus_voltages`` needs ``bus_id`` and ``v_mag``. If any are missing
    the function fills NaN columns rather than raising.
    """
    if "bus_id" not in gens_df.columns or "target_v" not in gens_df.columns:
        return gens_df
    if (bus_voltages.empty
            or "v_mag" not in bus_voltages.columns
            or "bus_id" not in bus_voltages.columns):
        return gens_df.assign(
            v_bus=pd.Series(float("nan"), index=gens_df.index, dtype="float64"),
            v_target_gap=pd.Series(float("nan"), index=gens_df.index, dtype="float64"),
        )
    lookup = bus_voltages.set_index("bus_id")["v_mag"]
    v_bus = gens_df["bus_id"].map(lookup)
    return gens_df.assign(
        v_bus=v_bus,
        v_target_gap=gens_df["target_v"] - v_bus,
    )


def _augment_gens_with_bus_voltage(network, gens_df):
    """Thin wrapper around ``_add_bus_voltage_columns`` that pulls the
    post-LF bus voltages from the cached ``get_bus_voltages`` getter.
    """
    return _add_bus_voltage_columns(gens_df, get_bus_voltages(network))


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
