"""Geographical voltage map for the Voltage Analysis tab.

Reuses the Leaflet-based approach from the pre-``2dac287`` era (before the
main Network Map tab was rewritten on top of ``@powsybl/network-map-layers``)
because voltage-deviation shading is not offered by that library.

The map colours each voltage level by its per-unit voltage deviation from
nominal (diverging blue-white-red scale). Substations without geographical
coordinates are skipped. Voltage levels below ``TRANSPORT_NOMINAL_V_THRESHOLD``
(63 kV by default) are filtered out because they are not part of the
transport network and would clutter the map.

Two render modes:
- ``icons``    — one colored dot per substation at the geo position
- ``gradient`` — overlapping translucent wide circles blending into a
                 heatmap-like surface, with a small dot at each substation

Data extraction runs on the pypowsybl worker thread and produces a dict of
pure-Python primitives (lists, floats, strings) so the result can be safely
cached in ``st.session_state`` across reruns on different threads.
"""
from __future__ import annotations

import json
import math

import pandas as pd
import streamlit as st
import streamlit.components.v1 as st_components

from iidm_viewer.powsybl_worker import run


TRANSPORT_NOMINAL_V_THRESHOLD = 63.0  # kV — below this we don't show on the map


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


_LEAFLET_HTML = """
<!DOCTYPE html>
<html>
<head>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  #map {{ width: 100%; height: {height}px; }}
  .legend {{
    background: white;
    padding: 8px 12px;
    border-radius: 5px;
    box-shadow: 0 1px 5px rgba(0,0,0,0.3);
    font-size: 12px;
    line-height: 1.5;
  }}
</style>
</head>
<body>
<div id="map"></div>
<script>
var records = {records};
var mode = {mode};
var vRange = {v_range};
var gradientRadiusMeters = {gradient_radius_m};

function lerp(a, b, t) {{ return a + (b - a) * t; }}

function divergingColor(v_pu) {{
  if (v_pu === null || v_pu === undefined || isNaN(v_pu)) {{
    return [160, 160, 160, 0.35];
  }}
  var d = v_pu - 1.0;
  var t = Math.max(-1, Math.min(1, d / vRange));
  var mid = [255, 255, 224];  // pale yellow — easy to distinguish from OSM whites
  var lo  = [27,  74,  199];  // blue  — under-voltage
  var hi  = [199, 27,  27];   // red   — over-voltage
  var target = t < 0 ? lo : hi;
  var k = Math.abs(t);
  return [
    Math.round(lerp(mid[0], target[0], k)),
    Math.round(lerp(mid[1], target[1], k)),
    Math.round(lerp(mid[2], target[2], k)),
    0.9
  ];
}}

function toRGBA(c, alpha) {{
  var a = (alpha === undefined) ? c[3] : alpha;
  return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')';
}}

var map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; OpenStreetMap contributors',
  maxZoom: 18
}}).addTo(map);

if (records.length > 0) {{
  var pts = records.map(function(r) {{ return [r.lat, r.lon]; }});
  var bounds = L.latLngBounds(pts);
  map.fitBounds(bounds, {{ padding: [30, 30] }});
}} else {{
  map.setView([46.6, 2.5], 6);
}}

function tooltipHtml(r) {{
  var html = '<b>' + r.vl_id + '</b><br>' +
    'Substation: ' + r.substation_id + '<br>' +
    'Nominal: ' + r.nominal_v.toFixed(1) + ' kV<br>';
  if (r.v_pu !== null && !isNaN(r.v_pu)) {{
    html += 'Mean V: ' + r.v_mag_mean.toFixed(2) + ' kV' +
            ' (' + r.v_pu.toFixed(4) + ' pu)<br>';
    var dev = (r.v_pu - 1.0) * 100;
    html += 'Deviation: ' + (dev >= 0 ? '+' : '') + dev.toFixed(2) + ' %';
  }} else {{
    html += '<i>No load-flow voltage</i>';
  }}
  return html;
}}

if (mode === 'gradient') {{
  records.forEach(function(r) {{
    if (r.v_pu === null || isNaN(r.v_pu)) return;
    var c = divergingColor(r.v_pu);
    L.circle([r.lat, r.lon], {{
      radius: gradientRadiusMeters,
      stroke: false,
      fillColor: toRGBA(c, 0.35),
      fillOpacity: 0.35
    }}).addTo(map);
  }});
  records.forEach(function(r) {{
    var c = divergingColor(r.v_pu);
    var m = L.circleMarker([r.lat, r.lon], {{
      radius: 3,
      fillColor: toRGBA(c, 1.0),
      color: '#222',
      weight: 0.7,
      fillOpacity: 0.95
    }}).addTo(map);
    m.bindTooltip(tooltipHtml(r));
  }});
}} else {{
  records.forEach(function(r) {{
    var c = divergingColor(r.v_pu);
    var m = L.circleMarker([r.lat, r.lon], {{
      radius: 7,
      fillColor: toRGBA(c, 0.95),
      color: '#333',
      weight: 1,
      fillOpacity: 0.95
    }}).addTo(map);
    m.bindTooltip(tooltipHtml(r));
  }});
}}

var legend = L.control({{position: 'topright'}});
legend.onAdd = function() {{
  var div = L.DomUtil.create('div', 'legend');
  var html = '<b>Voltage (pu)</b><br>' +
    '<span style="font-size:10px;color:#555">center = 1.000 pu, full scale &plusmn;' +
    vRange.toFixed(3) + '</span><br>';
  var stops = [-1, -0.5, 0, 0.5, 1];
  stops.forEach(function(t) {{
    var v_pu = 1.0 + t * vRange;
    var c = divergingColor(v_pu);
    html += '<span style="display:inline-block;width:16px;height:12px;background:' +
      toRGBA(c, 0.95) + ';margin-right:6px;vertical-align:middle;border:1px solid #999"></span>' +
      v_pu.toFixed(3) + '<br>';
  }});
  html += '<span style="display:inline-block;width:16px;height:12px;background:rgba(160,160,160,0.35);margin-right:6px;vertical-align:middle;border:1px solid #999"></span>no data';
  div.innerHTML = html;
  return div;
}};
legend.addTo(map);

setTimeout(function() {{ map.invalidateSize(); }}, 200);
</script>
</body>
</html>
"""


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

    html = _LEAFLET_HTML.format(
        height=620,
        records=json.dumps(display),
        mode=json.dumps(mode),
        v_range=json.dumps(float(v_range)),
        gradient_radius_m=json.dumps(gradient_radius_m),
    )
    st_components.html(html, height=640)

    with_v = [r for r in display if r["v_pu"] is not None]
    total = len(display)
    shown_nom = "all nominal voltages" if sel_nom is None else f"{sel_nom:g} kV"
    st.caption(
        f"{total} voltage levels at {shown_nom} "
        f"({len(with_v)} with load-flow voltages)"
    )
