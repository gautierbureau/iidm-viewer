"""Geographical voltage map for the Voltage Analysis tab.

Uses the shared Leaflet renderer in ``leaflet_scalar_map``. Each voltage
level with a known substation position is drawn as a colored marker whose
color encodes per-unit voltage deviation from nominal (diverging
blue-white-red). Voltage levels below ``TRANSPORT_NOMINAL_V_THRESHOLD``
(63 kV) are filtered out — the transport-network focus.

Three layouts (orthogonal to the icons / gradient render mode):

- ``per_vl``         — one marker per VL at the substation centre (the
                       multi-VL substations stack on top of each other)
- ``per_vl_fanned``  — one marker per VL, jittered around the substation
                       centre on a small circle so each VL is visible
- ``per_sub_worst``  — one marker per substation, value = signed worst
                       |v_pu − 1| across the substation's VLs; tooltip
                       lists the breakdown

Data extraction runs on the pypowsybl worker thread; the result is a dict
of pure-Python primitives cached in ``st.session_state`` so reruns on
different threads don't re-query pypowsybl.
"""
from __future__ import annotations

import math
from collections import defaultdict

import pandas as pd
import streamlit as st

from iidm_viewer.leaflet_scalar_map import (
    DivergingColorScale,
    render_scalar_map,
)
from iidm_viewer.powsybl_worker import run


TRANSPORT_NOMINAL_V_THRESHOLD = 63.0  # kV — below this we don't show on the map

_VOLTAGE_COLOR_SCALE_MID = (255, 255, 224)  # pale yellow at 1.0 pu
_VOLTAGE_COLOR_SCALE_LOW = (27, 74, 199)    # blue — under-voltage
_VOLTAGE_COLOR_SCALE_HIGH = (199, 27, 27)   # red — over-voltage

_FAN_JITTER_DEG = 0.025  # ~2.8 km in latitude — visible at the France default zoom

_LAYOUT_OPTIONS = {
    "Per VL": "per_vl",
    "Per VL (fanned)": "per_vl_fanned",
    "Per substation (worst)": "per_sub_worst",
}


def _extract_voltage_map_data(network) -> dict | None:
    """Collect substation positions and per-VL voltage data on the worker.

    Returns a dict with:
        records:   list of dicts, one per voltage level that has a substation
                   position. Each entry: vl_id, substation_id, nominal_v,
                   v_mag_mean, v_mag_min, v_mag_max, lat, lon, bus_count.
        has_lf:    True when any bus has a finite v_mag (a load flow has run).

    Returns ``None`` when the network has no ``substationPosition`` extension
    or when that extension has no valid coordinates.
    """
    raw = object.__getattribute__(network, "_obj")

    def _extract():
        from pypowsybl.network import get_extensions_names

        if "substationPosition" not in get_extensions_names():
            return None

        subs_pos_df = raw.get_extensions("substationPosition")
        if subs_pos_df.empty:
            return None

        subs_df = raw.get_substations().reset_index()
        pos_df = subs_df.merge(subs_pos_df, left_on="id", right_on="id")[
            ["id", "latitude", "longitude"]
        ]
        pos_df = pos_df[
            pos_df["latitude"].between(-90, 90)
            & pos_df["longitude"].between(-180, 180)
        ]
        if pos_df.empty:
            return None

        vls_df = raw.get_voltage_levels().reset_index()
        vls_df["id"] = vls_df["id"].astype(str)
        vls_df["substation_id"] = vls_df["substation_id"].astype(str)

        try:
            buses = raw.get_buses(all_attributes=True).reset_index()
        except Exception:
            buses = pd.DataFrame()

        if not buses.empty and "v_mag" in buses.columns:
            buses["voltage_level_id"] = buses["voltage_level_id"].astype(str)
            bus_agg = (
                buses.groupby("voltage_level_id")
                .agg(
                    v_mag_mean=("v_mag", "mean"),
                    v_mag_min=("v_mag", "min"),
                    v_mag_max=("v_mag", "max"),
                    bus_count=("id", "count"),
                )
                .reset_index()
            )
        else:
            bus_agg = pd.DataFrame(
                columns=[
                    "voltage_level_id", "v_mag_mean", "v_mag_min",
                    "v_mag_max", "bus_count",
                ]
            )

        merged = vls_df.merge(
            bus_agg, left_on="id", right_on="voltage_level_id", how="left"
        )

        positions = {
            str(row["id"]): (float(row["latitude"]), float(row["longitude"]))
            for _, row in pos_df.iterrows()
        }

        records = []
        for _, vl in merged.iterrows():
            sub_id = str(vl["substation_id"])
            if sub_id not in positions:
                continue
            lat, lon = positions[sub_id]
            nominal_v = float(vl["nominal_v"]) if pd.notna(vl.get("nominal_v")) else 0.0
            records.append({
                "vl_id": str(vl["id"]),
                "substation_id": sub_id,
                "nominal_v": nominal_v,
                "v_mag_mean": _nan_to_none(vl.get("v_mag_mean")),
                "v_mag_min": _nan_to_none(vl.get("v_mag_min")),
                "v_mag_max": _nan_to_none(vl.get("v_mag_max")),
                "bus_count": int(vl["bus_count"]) if pd.notna(vl.get("bus_count")) else 0,
                "lat": lat,
                "lon": lon,
            })

        has_lf = bool(
            not bus_agg.empty and bus_agg["v_mag_mean"].notna().any()
        )
        return {"records": records, "has_lf": has_lf}

    return run(_extract)


def _nan_to_none(val):
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _prepare_display_records(records, sel_nom, min_nominal):
    """Filter records and attach a computed ``v_pu``.

    - drop nominal_v < min_nominal (transport-only filter)
    - keep only nominal_v == sel_nom if sel_nom is not None
    - compute v_pu = v_mag_mean / nominal_v when both are finite
    """
    out = []
    for r in records:
        if r["nominal_v"] < min_nominal:
            continue
        if sel_nom is not None and abs(r["nominal_v"] - sel_nom) > 0.01:
            continue
        v_pu = None
        if r["v_mag_mean"] is not None and r["nominal_v"] > 0:
            v_pu = r["v_mag_mean"] / r["nominal_v"]
        entry = dict(r)
        entry["v_pu"] = v_pu
        out.append(entry)
    return out


def _group_by_substation(display) -> dict[str, list[dict]]:
    by_sub: dict[str, list[dict]] = defaultdict(list)
    for r in display:
        by_sub[r["substation_id"]].append(r)
    return by_sub


def _fan_records(display, jitter_deg: float = _FAN_JITTER_DEG):
    """Spread co-located VLs around their substation on a small circle.

    Substations with a single VL are returned unchanged. Substations with
    N > 1 VLs get N markers placed at evenly-spaced angles on a circle of
    radius ``jitter_deg`` degrees around the centre. VLs are sorted by
    nominal voltage (highest first) so the placement is stable across
    reruns.
    """
    out = []
    for sub_id, group in _group_by_substation(display).items():
        if len(group) <= 1:
            out.extend(group)
            continue
        ordered = sorted(group, key=lambda r: -r["nominal_v"])
        n = len(ordered)
        for i, r in enumerate(ordered):
            angle = 2 * math.pi * i / n
            shifted = dict(r)
            shifted["lat"] = r["lat"] + jitter_deg * math.cos(angle)
            shifted["lon"] = r["lon"] + jitter_deg * math.sin(angle)
            out.append(shifted)
    return out


def _aggregate_per_substation_worst(display):
    """Collapse per-VL records to one record per substation.

    The aggregated record's ``v_pu`` is the signed deviation of the VL
    with the largest ``|v_pu − 1|``. Substations whose VLs all lack a
    load-flow voltage are kept (so the operator still sees the dot) with
    ``v_pu = None`` — they render as the grey "no data" swatch.
    """
    out = []
    for sub_id, group in _group_by_substation(display).items():
        with_v = [r for r in group if r["v_pu"] is not None]
        ordered = sorted(group, key=lambda r: -r["nominal_v"])
        base = ordered[0]
        if with_v:
            worst = max(with_v, key=lambda r: abs(r["v_pu"] - 1.0))
            v_pu = worst["v_pu"]
            v_mag_mean = worst["v_mag_mean"]
            worst_vl_id = worst["vl_id"]
            worst_nominal = worst["nominal_v"]
        else:
            v_pu = None
            v_mag_mean = None
            worst_vl_id = None
            worst_nominal = None
        out.append({
            "vl_id": sub_id,  # used as the renderer "id" only
            "substation_id": sub_id,
            "nominal_v": base["nominal_v"],
            "v_mag_mean": v_mag_mean,
            "v_pu": v_pu,
            "lat": base["lat"],
            "lon": base["lon"],
            "_aggregate": True,
            "_worst_vl_id": worst_vl_id,
            "_worst_nominal": worst_nominal,
            "_group": ordered,
        })
    return out


def _apply_layout(display, layout: str):
    if layout == "per_vl":
        return display
    if layout == "per_vl_fanned":
        return _fan_records(display)
    if layout == "per_sub_worst":
        return _aggregate_per_substation_worst(display)
    raise ValueError(f"Unknown layout: {layout!r}")


def _format_pu_line(nominal_v: float, v_pu: float | None,
                    v_mag_mean: float | None, prefix: str = "") -> str:
    if v_pu is None or v_mag_mean is None:
        return f"{prefix}{nominal_v:g} kV: <i>no LF voltage</i>"
    dev_pct = (v_pu - 1.0) * 100.0
    sign = "+" if dev_pct >= 0 else ""
    return (
        f"{prefix}{nominal_v:g} kV: {v_mag_mean:.2f} kV "
        f"({v_pu:.4f} pu, {sign}{dev_pct:.2f} %)"
    )


def _build_per_vl_tooltip(r: dict) -> str:
    html = (
        f"<b>{r['vl_id']}</b><br>"
        f"Substation: {r['substation_id']}<br>"
        f"Nominal: {r['nominal_v']:.1f} kV<br>"
    )
    v_pu = r.get("v_pu")
    if v_pu is not None and r.get("v_mag_mean") is not None:
        dev_pct = (v_pu - 1.0) * 100.0
        sign = "+" if dev_pct >= 0 else ""
        html += (
            f"Mean V: {r['v_mag_mean']:.2f} kV ({v_pu:.4f} pu)<br>"
            f"Deviation: {sign}{dev_pct:.2f} %"
        )
    else:
        html += "<i>No load-flow voltage</i>"
    return html


def _build_per_substation_tooltip(r: dict) -> str:
    group = r["_group"]
    html = f"<b>Substation: {r['substation_id']}</b><br>{len(group)} VLs<br>"
    if r.get("v_pu") is None:
        html += "<i>No load-flow voltage on any VL</i><br>"
    else:
        html += (
            f"<b>Worst:</b> {_format_pu_line(r['_worst_nominal'], r['v_pu'], r['v_mag_mean'])}"
            f"<br>"
        )
    html += "All VLs:<br>"
    for vl in group:
        html += _format_pu_line(
            vl["nominal_v"], vl.get("v_pu"), vl.get("v_mag_mean"),
            prefix="&nbsp;&nbsp;",
        ) + "<br>"
    return html


def _build_tooltip(r: dict) -> str:
    if r.get("_aggregate"):
        return _build_per_substation_tooltip(r)
    return _build_per_vl_tooltip(r)


def _to_render_records(display):
    """Turn internal voltage records into the shared renderer shape."""
    out = []
    for r in display:
        out.append({
            "id": r["vl_id"],
            "lat": r["lat"],
            "lon": r["lon"],
            "value": r["v_pu"],
            "tooltip": _build_tooltip(r),
        })
    return out


def _voltage_legend_stops(v_range: float) -> list[tuple[float, str]]:
    fractions = (-1.0, -0.5, 0.0, 0.5, 1.0)
    return [(1.0 + f * v_range, f"{1.0 + f * v_range:.3f} pu") for f in fractions]


def _get_cached_voltage_map_data(network):
    cache = st.session_state.get("_voltage_map_cache")
    if cache is not None:
        return cache
    result = _extract_voltage_map_data(network)
    st.session_state["_voltage_map_cache"] = result
    return result


def render_voltage_map(network):
    """Draw the geographical voltage map inside the Voltage Analysis tab."""
    st.subheader("Geographical voltage map")

    data = _get_cached_voltage_map_data(network)
    if data is None:
        st.info(
            "No geographical data available. The network needs a "
            "'substationPosition' extension with latitude/longitude coordinates."
        )
        return

    records = data["records"]
    has_lf = data["has_lf"]
    transport_records = [r for r in records if r["nominal_v"] >= TRANSPORT_NOMINAL_V_THRESHOLD]

    if not transport_records:
        st.info(
            f"No voltage levels at or above {TRANSPORT_NOMINAL_V_THRESHOLD:g} kV with "
            "geographical coordinates in this network."
        )
        return

    if not has_lf:
        st.info(
            "Voltage magnitudes are not available on the map — run a load flow first."
        )
        return

    nom_counts: dict[float, int] = {}
    for r in transport_records:
        key = round(r["nominal_v"], 3)
        nom_counts[key] = nom_counts.get(key, 0) + 1
    available_noms = sorted(nom_counts.keys(), reverse=True)

    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])

    nom_options = ["All nominal voltages"] + [
        f"{nv:g} kV ({nom_counts[nv]} VL)" for nv in available_noms
    ]
    sel_label = col1.selectbox(
        "Nominal voltage filter",
        nom_options,
        key="va_map_nom_select",
        help=(
            "Restrict the map to a single nominal voltage. With 'All', the pu "
            "scale stays comparable across classes thanks to per-unit normalisation."
        ),
    )
    sel_nom: float | None = None
    if sel_label != nom_options[0]:
        sel_nom = available_noms[nom_options.index(sel_label) - 1]

    layout_label = col2.radio(
        "Layout",
        list(_LAYOUT_OPTIONS.keys()),
        key="va_map_layout",
        horizontal=True,
        help=(
            "How to place markers when a substation has several voltage levels. "
            "'Per VL' stacks them at the same point; 'fanned' offsets each VL on "
            "a small circle; 'worst' shows one marker per substation colored by "
            "its worst |v_pu − 1|."
        ),
    )
    layout = _LAYOUT_OPTIONS[layout_label]

    mode_label = col3.radio(
        "View",
        ["Icons per substation", "Continuous gradient"],
        key="va_map_mode",
        horizontal=True,
    )
    mode = "icons" if mode_label == "Icons per substation" else "gradient"

    v_range = col4.number_input(
        "Full-scale ± pu",
        min_value=0.005,
        max_value=0.5,
        value=0.05,
        step=0.005,
        format="%.3f",
        key="va_map_vrange",
        help="Deviation from 1.0 pu that fully saturates the red / blue color.",
    )

    display = _prepare_display_records(
        transport_records,
        sel_nom=sel_nom,
        min_nominal=TRANSPORT_NOMINAL_V_THRESHOLD,
    )

    if not display:
        st.info("No voltage levels match the current filter.")
        return

    laid_out = _apply_layout(display, layout)

    gradient_radius_m = 25000 if sel_nom is None or sel_nom >= 200 else 12000

    color_scale = DivergingColorScale(
        center=1.0,
        range=float(v_range),
        mid_rgb=_VOLTAGE_COLOR_SCALE_MID,
        low_rgb=_VOLTAGE_COLOR_SCALE_LOW,
        high_rgb=_VOLTAGE_COLOR_SCALE_HIGH,
    )
    render_scalar_map(
        _to_render_records(laid_out),
        mode=mode,
        color_scale=color_scale,
        legend_title="Voltage (pu)",
        legend_subtitle=f"center = 1.000 pu, full scale ±{float(v_range):.3f}",
        legend_stops=_voltage_legend_stops(float(v_range)),
        gradient_radius_m=gradient_radius_m,
    )

    with_v = [r for r in display if r["v_pu"] is not None]
    total_vls = len(display)
    total_subs = len({r["substation_id"] for r in display})
    shown_nom = "all nominal voltages" if sel_nom is None else f"{sel_nom:g} kV"
    if layout == "per_sub_worst":
        st.caption(
            f"{total_subs} substations at {shown_nom} "
            f"(aggregated from {total_vls} VLs, {len(with_v)} with load-flow voltages)"
        )
    else:
        st.caption(
            f"{total_vls} voltage levels at {shown_nom} "
            f"across {total_subs} substations "
            f"({len(with_v)} with load-flow voltages)"
        )
