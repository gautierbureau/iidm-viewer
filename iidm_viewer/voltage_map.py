"""Geographical voltage map for the Voltage Analysis tab.

Uses the shared Leaflet renderer in ``leaflet_scalar_map``. Each voltage
level with a known substation position is drawn as a colored marker whose
color encodes per-unit voltage deviation from nominal (diverging
blue-white-red). Voltage levels below ``TRANSPORT_NOMINAL_V_THRESHOLD``
(63 kV) are filtered out — the transport-network focus.

Two render modes:
- ``icons``    — one colored dot per voltage level at its substation
- ``gradient`` — wide translucent circles blending into a heatmap surface,
                 with small dots on top for tooltips

Data extraction runs on the pypowsybl worker thread; the result is a dict
of pure-Python primitives cached in ``st.session_state`` so reruns on
different threads don't re-query pypowsybl.
"""
from __future__ import annotations

import math

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


def _build_tooltip(r: dict) -> str:
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

    col1, col2, col3 = st.columns([2, 2, 1])

    nom_options = ["All nominal voltages"] + [
        f"{nv:g} kV ({nom_counts[nv]} VL)" for nv in available_noms
    ]
    sel_label = col1.selectbox(
        "Nominal voltage filter",
        nom_options,
        key="va_map_nom_select",
        help=(
            "Restrict the map to a single nominal voltage so the pu color scale "
            "is comparable across substations."
        ),
    )
    sel_nom: float | None = None
    if sel_label != nom_options[0]:
        sel_nom = available_noms[nom_options.index(sel_label) - 1]

    mode_label = col2.radio(
        "View",
        ["Icons per substation", "Continuous gradient"],
        key="va_map_mode",
        horizontal=True,
    )
    mode = "icons" if mode_label == "Icons per substation" else "gradient"

    v_range = col3.number_input(
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

    gradient_radius_m = 25000 if sel_nom is None or sel_nom >= 200 else 12000

    color_scale = DivergingColorScale(
        center=1.0,
        range=float(v_range),
        mid_rgb=_VOLTAGE_COLOR_SCALE_MID,
        low_rgb=_VOLTAGE_COLOR_SCALE_LOW,
        high_rgb=_VOLTAGE_COLOR_SCALE_HIGH,
    )
    render_scalar_map(
        _to_render_records(display),
        mode=mode,
        color_scale=color_scale,
        legend_title="Voltage (pu)",
        legend_subtitle=f"center = 1.000 pu, full scale ±{float(v_range):.3f}",
        legend_stops=_voltage_legend_stops(float(v_range)),
        gradient_radius_m=gradient_radius_m,
    )

    with_v = [r for r in display if r["v_pu"] is not None]
    total = len(display)
    shown_nom = "all nominal voltages" if sel_nom is None else f"{sel_nom:g} kV"
    st.caption(
        f"{total} voltage levels at {shown_nom} "
        f"({len(with_v)} with load-flow voltages)"
    )
