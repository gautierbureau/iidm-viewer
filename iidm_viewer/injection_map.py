"""Geographical net-injection map.

Per substation, aggregates the active and reactive power injected into the
grid by generators and consumed by loads, and renders the net scalar value
on top of substation positions using the shared Leaflet renderer
(``leaflet_scalar_map``).

Sign convention exposed to the user:
    positive  → substation is a **net exporter** (more generation than load)
    negative  → substation is a **net importer** (more load than generation)

Under the hood, pypowsybl stores all terminal powers in **load convention**
(positive = flowing from bus into equipment), so a generator terminal's
``p`` is negative and a load terminal's ``p`` is positive. Grid injection
is therefore ``-p`` for every terminal, summed per substation.

When the load flow has not populated terminal ``p`` yet, the extraction
falls back to scheduled values: ``target_p`` for generators (already in
generation convention, i.e. injection-signed) and ``p0`` for loads (load
convention, flipped).

Disconnected terminals contribute 0.

Substations with no VL at or above ``TRANSPORT_NOMINAL_V_THRESHOLD``
(63 kV) are excluded — they are not part of the transport grid.
"""
from __future__ import annotations

import math

import pandas as pd
import streamlit as st

from iidm_viewer.leaflet_scalar_map import (
    DivergingColorScale,
    get_substation_positions,
    render_scalar_map,
)
from iidm_viewer.powsybl_worker import run


TRANSPORT_NOMINAL_V_THRESHOLD = 63.0  # kV — substations with no VL ≥ this are hidden

_INJ_COLOR_SCALE_MID = (255, 255, 224)  # pale yellow at 0 MW
_INJ_COLOR_SCALE_LOW = (199, 27, 27)    # red — net importer (consumes)
_INJ_COLOR_SCALE_HIGH = (24, 150, 58)   # green — net exporter (injects)


def _grid_inj_series(df: pd.DataFrame, realized_col: str, scheduled_col: str,
                     flip_scheduled: bool) -> pd.Series:
    """Return grid injection in MW/MVAr for each row of ``df``.

    Uses ``realized_col`` (terminal p or q, load convention) flipped to
    grid-injection convention; falls back to ``scheduled_col`` (with the
    flip controlled by ``flip_scheduled`` — ``False`` for generator
    ``target_p`` / ``target_q`` which are already injection-signed,
    ``True`` for load ``p0`` / ``q0`` which are load-convention).
    Disconnected rows contribute 0.
    """
    if df.empty:
        return pd.Series(dtype=float)
    realized = df[realized_col] if realized_col in df.columns else pd.Series(float("nan"), index=df.index)
    scheduled = df[scheduled_col] if scheduled_col in df.columns else pd.Series(float("nan"), index=df.index)

    primary = -realized
    fallback = -scheduled if flip_scheduled else scheduled
    merged = primary.where(realized.notna(), fallback)

    if "connected" in df.columns:
        merged = merged.where(df["connected"].fillna(False), other=0.0)
    return merged.fillna(0.0)


def _extract_injection_data(network) -> dict | None:
    """Aggregate P/Q injections per substation on the pypowsybl worker.

    Returns ``None`` when the network has no ``substationPosition``
    extension. Otherwise returns::

        {
          "records": [
            {"substation_id", "substation_name", "max_nominal_v",
             "nominal_v_set": [..],
             "gen_p_mw", "load_p_mw", "inj_p_mw",
             "gen_q_mvar", "load_q_mvar", "inj_q_mvar",
             "gen_count", "load_count",
             "lat", "lon"},
            ...
          ],
          "has_lf_p": bool,   # any terminal.p populated
          "has_lf_q": bool,   # any terminal.q populated
        }
    """
    positions = get_substation_positions(network)
    if not positions:
        return None

    raw = object.__getattribute__(network, "_obj")

    def _extract():
        subs_df = raw.get_substations().reset_index()
        subs_df["id"] = subs_df["id"].astype(str)

        vls_df = raw.get_voltage_levels().reset_index()
        vls_df["id"] = vls_df["id"].astype(str)
        vls_df["substation_id"] = vls_df["substation_id"].astype(str)
        vl_to_sub = dict(zip(vls_df["id"], vls_df["substation_id"]))
        vl_to_nom = dict(zip(vls_df["id"], vls_df["nominal_v"].astype(float)))

        try:
            gens = raw.get_generators(all_attributes=True).reset_index()
        except Exception:
            gens = pd.DataFrame()
        try:
            loads = raw.get_loads(all_attributes=True).reset_index()
        except Exception:
            loads = pd.DataFrame()

        for df in (gens, loads):
            if not df.empty and "voltage_level_id" in df.columns:
                df["voltage_level_id"] = df["voltage_level_id"].astype(str)
                df["substation_id"] = df["voltage_level_id"].map(vl_to_sub)

        gen_p = _grid_inj_series(gens, "p", "target_p", flip_scheduled=False)
        gen_q = _grid_inj_series(gens, "q", "target_q", flip_scheduled=False)
        load_p = _grid_inj_series(loads, "p", "p0", flip_scheduled=True)
        load_q = _grid_inj_series(loads, "q", "q0", flip_scheduled=True)

        def _sum_by_sub(df: pd.DataFrame, vals: pd.Series) -> dict[str, float]:
            if df.empty or vals.empty:
                return {}
            return (
                pd.DataFrame({"substation_id": df.get("substation_id"), "v": vals})
                .dropna(subset=["substation_id"])
                .groupby("substation_id")["v"]
                .sum()
                .to_dict()
            )

        gen_p_by_sub = _sum_by_sub(gens, gen_p)
        gen_q_by_sub = _sum_by_sub(gens, gen_q)
        load_p_by_sub = _sum_by_sub(loads, load_p)
        load_q_by_sub = _sum_by_sub(loads, load_q)

        def _count_by_sub(df: pd.DataFrame) -> dict[str, int]:
            if df.empty or "substation_id" not in df.columns:
                return {}
            return df.dropna(subset=["substation_id"]).groupby("substation_id").size().to_dict()

        gen_count_by_sub = _count_by_sub(gens)
        load_count_by_sub = _count_by_sub(loads)

        vls_by_sub: dict[str, list[str]] = {}
        for _, vl in vls_df.iterrows():
            vls_by_sub.setdefault(vl["substation_id"], []).append(vl["id"])

        records = []
        for _, sub in subs_df.iterrows():
            sub_id = sub["id"]
            if sub_id not in positions:
                continue
            vl_ids = vls_by_sub.get(sub_id, [])
            nom_set = sorted({vl_to_nom[v] for v in vl_ids if v in vl_to_nom}, reverse=True)
            max_nom = nom_set[0] if nom_set else 0.0

            gp = float(gen_p_by_sub.get(sub_id, 0.0))
            lp = float(load_p_by_sub.get(sub_id, 0.0))
            gq = float(gen_q_by_sub.get(sub_id, 0.0))
            lq = float(load_q_by_sub.get(sub_id, 0.0))

            lat, lon = positions[sub_id]
            records.append({
                "substation_id": sub_id,
                "substation_name": str(sub.get("name", "") or sub_id),
                "max_nominal_v": max_nom,
                "nominal_v_set": [float(n) for n in nom_set],
                "gen_p_mw": gp,
                "load_p_mw": lp,
                "inj_p_mw": gp + lp,  # both already in grid-injection convention
                "gen_q_mvar": gq,
                "load_q_mvar": lq,
                "inj_q_mvar": gq + lq,
                "gen_count": int(gen_count_by_sub.get(sub_id, 0)),
                "load_count": int(load_count_by_sub.get(sub_id, 0)),
                "lat": lat,
                "lon": lon,
            })

        def _any_finite(df, col):
            if df.empty or col not in df.columns:
                return False
            return bool(df[col].notna().any())

        return {
            "records": records,
            "has_lf_p": _any_finite(gens, "p") or _any_finite(loads, "p"),
            "has_lf_q": _any_finite(gens, "q") or _any_finite(loads, "q"),
        }

    return run(_extract)


def _get_cached_injection_data(network):
    cache = st.session_state.get("_injection_map_cache")
    if cache is not None:
        return cache
    result = _extract_injection_data(network)
    st.session_state["_injection_map_cache"] = result
    return result


def _filter_transport(records):
    return [r for r in records if r["max_nominal_v"] >= TRANSPORT_NOMINAL_V_THRESHOLD]


def _radius_for(value: float | None, full_scale: float,
                min_r: float = 4.0, max_r: float = 18.0) -> float:
    """Marker radius in px: min_r + (max_r - min_r) * sqrt(|value| / full_scale).

    Square-root sizing keeps small injections visible while large stations
    don't overwhelm the map. Clamped to ``[min_r, max_r]`` so an out-of-scale
    value doesn't render as a blob.
    """
    if value is None or full_scale <= 0 or math.isnan(value):
        return min_r
    frac = min(1.0, abs(value) / full_scale)
    return min_r + (max_r - min_r) * math.sqrt(frac)


def _build_tooltip(r: dict, metric: str, unit: str) -> str:
    value = r["inj_p_mw"] if metric == "P" else r["inj_q_mvar"]
    gen = r["gen_p_mw"] if metric == "P" else r["gen_q_mvar"]
    load = r["load_p_mw"] if metric == "P" else r["load_q_mvar"]
    name = r["substation_name"]
    nom_set = r.get("nominal_v_set", [])
    nom_fmt = ", ".join(f"{n:g} kV" for n in nom_set) if nom_set else "—"

    sign = "+" if value >= 0 else ""
    label = "Net exporter" if value >= 0 else "Net importer"
    html = (
        f"<b>{r['substation_id']}</b>"
        + (f" ({name})" if name and name != r["substation_id"] else "")
        + f"<br>Nominal: {nom_fmt}<br>"
        f"<b>Net injection:</b> {sign}{value:.1f} {unit}  <i>({label})</i><br>"
        f"Generation: +{gen:.1f} {unit} ({r['gen_count']} gen)<br>"
        # load is already grid-injection-signed (negative for a consumer), so
        # show its absolute value with an explicit minus to match user intuition.
        f"Load: {load:.1f} {unit} ({r['load_count']} load)"
    )
    return html


def _to_render_records(records, metric: str, unit: str, full_scale: float):
    out = []
    for r in records:
        value = r["inj_p_mw"] if metric == "P" else r["inj_q_mvar"]
        out.append({
            "id": r["substation_id"],
            "lat": r["lat"],
            "lon": r["lon"],
            "value": value,
            "tooltip": _build_tooltip(r, metric, unit),
            "radius": _radius_for(value, full_scale),
        })
    return out


def _inj_legend_stops(full_scale: float, unit: str) -> list[tuple[float, str]]:
    fractions = (-1.0, -0.5, 0.0, 0.5, 1.0)
    stops: list[tuple[float, str]] = []
    for f in fractions:
        val = f * full_scale
        if abs(val) < 1e-9:
            label = f"0 {unit}"
        else:
            label = f"{val:+.0f} {unit}"
        stops.append((val, label))
    return stops


def render_injection_map(network):
    """Render the Injection Map tab."""
    st.caption(
        "Net active or reactive power per substation. "
        "**Green** = net exporter (generation > load), "
        "**red** = net importer (load > generation). "
        "Marker size scales with the absolute net injection."
    )

    data = _get_cached_injection_data(network)
    if data is None:
        st.info(
            "No geographical data available. The network needs a "
            "'substationPosition' extension with latitude/longitude coordinates."
        )
        return

    records = _filter_transport(data["records"])
    if not records:
        st.info(
            f"No substations with a voltage level at or above "
            f"{TRANSPORT_NOMINAL_V_THRESHOLD:g} kV in this network."
        )
        return

    col1, col2, col3 = st.columns([2, 2, 1])

    metric_label = col1.radio(
        "Metric",
        ["Active power (P)", "Reactive power (Q)"],
        key="im_metric",
        horizontal=True,
    )
    metric = "P" if metric_label.startswith("Active") else "Q"
    unit = "MW" if metric == "P" else "MVAr"

    mode_label = col2.radio(
        "View",
        ["Icons per substation", "Continuous gradient"],
        key="im_mode",
        horizontal=True,
    )
    mode = "icons" if mode_label == "Icons per substation" else "gradient"

    # Reasonable starting full-scale: pick something near the 90th percentile
    # of |values| so most substations are on-scale. User can override.
    default_scale = _suggest_full_scale(records, metric)
    full_scale = col3.number_input(
        f"Full-scale ± {unit}",
        min_value=1.0,
        max_value=100000.0,
        value=float(default_scale),
        step=50.0,
        format="%.0f",
        key=f"im_range_{metric}",
        help=(
            f"{unit} at which the color fully saturates and the marker "
            f"reaches its maximum radius."
        ),
    )

    has_lf = data["has_lf_p"] if metric == "P" else data["has_lf_q"]
    if not has_lf:
        st.caption(
            f"No terminal {metric} values populated (no load flow). "
            f"Showing scheduled setpoints (target_{metric.lower()} / "
            f"{('p0' if metric == 'P' else 'q0')})."
        )

    color_scale = DivergingColorScale(
        center=0.0,
        range=float(full_scale),
        mid_rgb=_INJ_COLOR_SCALE_MID,
        low_rgb=_INJ_COLOR_SCALE_LOW,
        high_rgb=_INJ_COLOR_SCALE_HIGH,
    )

    render_scalar_map(
        _to_render_records(records, metric, unit, float(full_scale)),
        mode=mode,
        color_scale=color_scale,
        legend_title=f"Net injection ({unit})",
        legend_subtitle=f"green = exports, red = imports, full scale ±{full_scale:.0f} {unit}",
        legend_stops=_inj_legend_stops(float(full_scale), unit),
        gradient_radius_m=25000,
    )

    exporters = [r for r in records if (r["inj_p_mw"] if metric == "P" else r["inj_q_mvar"]) > 0]
    importers = [r for r in records if (r["inj_p_mw"] if metric == "P" else r["inj_q_mvar"]) < 0]
    total_export = sum((r["inj_p_mw"] if metric == "P" else r["inj_q_mvar"]) for r in exporters)
    total_import = -sum((r["inj_p_mw"] if metric == "P" else r["inj_q_mvar"]) for r in importers)

    st.caption(
        f"{len(records)} substations — {len(exporters)} exporters "
        f"({total_export:+.0f} {unit}), {len(importers)} importers "
        f"({-total_import:+.0f} {unit}), "
        f"net {total_export - total_import:+.0f} {unit}"
    )


def _suggest_full_scale(records, metric: str) -> float:
    """Pick a sensible default full-scale by taking the 90th-percentile
    |net injection| value, rounded to a "nice" step. Falls back to 500 MW /
    500 MVAr when the network has no usable injections.
    """
    vals = []
    for r in records:
        v = r["inj_p_mw"] if metric == "P" else r["inj_q_mvar"]
        if v is not None and not math.isnan(v):
            vals.append(abs(v))
    if not vals:
        return 500.0
    vals.sort()
    p90 = vals[int(0.9 * (len(vals) - 1))]
    if p90 < 1:
        p90 = max(vals) if vals else 1.0
    # Round up to 1 / 2 / 5 × 10^n
    if p90 <= 0:
        return 500.0
    magnitude = 10 ** math.floor(math.log10(p90))
    for factor in (1, 2, 5, 10):
        candidate = factor * magnitude
        if candidate >= p90:
            return float(candidate)
    return float(10 * magnitude)
