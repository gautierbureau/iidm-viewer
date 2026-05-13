"""Framework-agnostic helpers for the Reactive Capability Curves tab.

This module owns the math and pypowsybl integration; each UI host
(Streamlit ``reactive_curves_tab``, PySide6, NiceGUI) renders on top.
No streamlit / Qt / NiceGUI imports here.

Public API:

* :data:`TARGET_TOLERANCE` / :data:`NEAR_SATURATION_THRESHOLD` /
  :data:`STATUS_DIAMOND_COLOR` — tunables shared across hosts.
* :func:`polygon_vertices` / :func:`signed_distance_to_polygon` —
  pure geometry over the capability polygon.
* :func:`classify_targets` — the core PV/PQ status assignment
  (``inside`` / ``edge`` / ``outside`` / ``saturated`` /
  ``near_saturation`` / ``needs_lf`` / ``n/a``).
* :func:`vl_to_step_up_transformer_table` —  pure pandas reduction
  over an enriched 2WT frame.
* :func:`add_bus_voltage_columns` — pure pandas join of bus voltages
  onto a generators frame.
* :func:`augment_gens_with_step_up_transformer` /
  :func:`augment_gens_with_bus_voltage` — worker-routed convenience
  wrappers that hosts can call when they don't carry their own
  cached frame.
* :func:`compute_target_v_q_sensitivities` — worker-routed batched
  AC sensitivity analysis ``{gen_id: (dq_dv, q_ref) | None}``. No
  caching here — hosts wrap with their own (Streamlit's
  ``state.compute_target_v_q_sensitivities`` adds the per-LF cache).

The Streamlit-only UI rendering lives in
:mod:`iidm_viewer.reactive_curves_tab`.
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from iidm_viewer.data_view import build_vl_lookup, enrich_with_joins
from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TARGET_TOLERANCE: float = 0.1
NEAR_SATURATION_THRESHOLD: float = 5.0  # MVar/MW; PV gens within this distance of a Q limit are flagged "near_saturation"

# Status → diamond colour mapping. Hosts can override; centralised so
# the Streamlit / Qt / NiceGUI plots stay visually consistent.
STATUS_DIAMOND_COLOR: dict[str, str] = {
    "inside": "green",
    "edge": "orange",
    "outside": "red",
    "saturated": "red",          # PV gen at limit — load flow switched to PQ
    "near_saturation": "orange", # PV gen close to a Q limit
    "needs_lf": "gray",
    "n/a": "gray",
}


# ---------------------------------------------------------------------------
# Polygon geometry
# ---------------------------------------------------------------------------
def polygon_vertices(gen_id, gen_row, curves_df, has_curve):
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


def signed_distance_to_polygon(tp, tq, polygon_p, polygon_q):
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


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify_targets(
    gens_df,
    curves_df,
    tolerance: float = TARGET_TOLERANCE,
    near_saturation_threshold: float = NEAR_SATURATION_THRESHOLD,
):
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
        poly_p, poly_q = polygon_vertices(
            gen_id, gens_df.loc[gen_id], curves_df, has_curve
        )
        if poly_p is None:
            continue
        distances.at[gen_id] = signed_distance_to_polygon(
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


# ---------------------------------------------------------------------------
# Step-up transformer mapping (VL → 2WT whose other side has the highest nominal V)
# ---------------------------------------------------------------------------
def vl_to_step_up_transformer_table(twts_enriched):
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


def augment_gens_with_step_up_transformer(
    network: NetworkProxy,
    gens_df: pd.DataFrame,
    *,
    vl_to_xf: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Add ``step_up_transformer_id`` / ``step_up_transformer_connected``
    to a generators frame, mapped via the gen's ``voltage_level_id``.

    ``vl_to_xf`` is the pre-computed result of
    :func:`vl_to_step_up_transformer_table`; pass it when the host
    already cached the mapping (Streamlit does this). When ``None``, the
    helper fetches the 2WT frame on the worker thread, enriches it
    against the shared VL lookup and runs the pure reduction itself —
    convenient for one-shot calls from PySide6 / NiceGUI.
    """
    if "voltage_level_id" not in gens_df.columns:
        return gens_df
    if vl_to_xf is None:
        twts = network.get_2_windings_transformers(all_attributes=True)
        if not twts.empty:
            twts = enrich_with_joins(twts.copy(), build_vl_lookup(network))
        vl_to_xf = vl_to_step_up_transformer_table(twts)
    if vl_to_xf.empty:
        return gens_df.assign(
            step_up_transformer_id=pd.Series(pd.NA, index=gens_df.index, dtype="object"),
            step_up_transformer_connected=pd.Series(pd.NA, index=gens_df.index, dtype="object"),
        )
    vl_series = gens_df["voltage_level_id"]
    return gens_df.assign(
        step_up_transformer_id=vl_series.map(vl_to_xf["step_up_transformer_id"]),
        step_up_transformer_connected=vl_series.map(vl_to_xf["step_up_transformer_connected"]),
    )


# ---------------------------------------------------------------------------
# Bus voltage join
# ---------------------------------------------------------------------------
def add_bus_voltage_columns(gens_df, bus_voltages):
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


def augment_gens_with_bus_voltage(
    network: NetworkProxy,
    gens_df: pd.DataFrame,
    *,
    bus_voltages: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Worker-routed wrapper around :func:`add_bus_voltage_columns`.

    ``bus_voltages`` is the pre-fetched ``(bus_id, v_mag)`` frame; pass
    it when the host has a cached copy (Streamlit's
    ``caches.get_bus_voltages``). When ``None``, the helper builds a
    minimal one from ``network.get_buses`` on the worker thread.
    """
    if bus_voltages is None:
        buses = network.get_buses(all_attributes=True)
        if buses.empty:
            bus_voltages = pd.DataFrame(columns=["bus_id", "v_mag"])
        else:
            bus_voltages = buses.reset_index().rename(columns={"id": "bus_id"})
    return add_bus_voltage_columns(gens_df, bus_voltages)


# ---------------------------------------------------------------------------
# AC sensitivity (worker-routed; hosts wrap with their own caching)
# ---------------------------------------------------------------------------
def compute_target_v_q_sensitivities(
    network: NetworkProxy,
    gen_ids: list,
) -> dict:
    """Return ``{gen_id: (dq_dv, q_ref) | None}`` for each id in ``gen_ids``.

    Runs one batched AC sensitivity analysis on the pypowsybl worker
    thread (one LF factorization shared across every generator, plus
    one RHS solve per gen) so a host that needs the gradient for many
    PV generators only pays a single round-trip.

    No caching here — Streamlit's
    :func:`iidm_viewer.state.compute_target_v_q_sensitivities` wraps
    this with a per-LF session-state cache; the PySide6 / NiceGUI hosts
    can wrap with their own state container.
    """
    gen_ids = list(gen_ids)
    if not gen_ids:
        return {}

    raw = object.__getattribute__(network, "_obj")

    def _run_sensitivity() -> dict:
        try:
            import pypowsybl.sensitivity as sens
            from pypowsybl.sensitivity import (
                ContingencyContextType,
                SensitivityFunctionType,
                SensitivityVariableType,
            )
            analysis = sens.create_ac_analysis()
            analysis.add_factor_matrix(
                gen_ids, gen_ids, [],
                ContingencyContextType.NONE,
                SensitivityFunctionType.BUS_REACTIVE_POWER,
                SensitivityVariableType.BUS_TARGET_VOLTAGE,
            )
            result = analysis.run(raw)
            sens_matrix = result.get_sensitivity_matrix()
            ref_matrix = result.get_reference_matrix()
            out: dict = {}
            for gid in gen_ids:
                try:
                    out[gid] = (
                        float(sens_matrix.loc[gid, gid]),
                        float(ref_matrix.loc["reference_values", gid]),
                    )
                except Exception:
                    out[gid] = None
            return out
        except Exception:
            return {gid: None for gid in gen_ids}

    return run(_run_sensitivity)


def compute_target_v_q_sensitivity(network: NetworkProxy, gen_id: str):
    """Single-gen convenience wrapper around :func:`compute_target_v_q_sensitivities`."""
    return compute_target_v_q_sensitivities(network, [gen_id]).get(gen_id)


# ---------------------------------------------------------------------------
# Legacy aliases — existing tests / streamlit_tab consume the underscored
# names. Keep them re-exported so the rename can land without breakage.
# ---------------------------------------------------------------------------
_TARGET_TOLERANCE = TARGET_TOLERANCE
_NEAR_SATURATION_THRESHOLD = NEAR_SATURATION_THRESHOLD
_STATUS_DIAMOND_COLOR = STATUS_DIAMOND_COLOR
_polygon_vertices = polygon_vertices
_signed_distance_to_polygon = signed_distance_to_polygon
_vl_to_step_up_transformer_table = vl_to_step_up_transformer_table
_add_bus_voltage_columns = add_bus_voltage_columns
