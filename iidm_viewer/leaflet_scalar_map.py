"""Shared Leaflet scalar-on-substation map renderer.

Two callers today:
- ``voltage_map.py``    — voltage deviation per VL (diverging scale centered at 1.0 pu)
- ``injection_map.py``  — net active/reactive power per substation (centered at 0 MW)

Both draw colored markers at geographical positions on a Leaflet map with a
diverging color scale, a legend, and optional gradient blending. This module
owns:

- ``get_substation_positions`` — the ``substationPosition`` extension lookup
  (runs on the pypowsybl worker thread);
- ``DivergingColorScale``     — an RGB triplet dataclass describing the
  mid / low / high color at a configurable center and full-scale range;
- ``render_scalar_map``       — the Streamlit entry point that formats and
  injects the Leaflet HTML.

Callers produce a list of record dicts (``id``, ``lat``, ``lon``, ``value``,
``tooltip``) plus a ``DivergingColorScale`` and call ``render_scalar_map``.
Per-record overrides (``radius`` for marker size) are picked up when
present.

The JS template keeps the same two render modes as the original voltage
map: ``icons`` (one marker per record) and ``gradient`` (overlapping wide
translucent circles with a small dot on top for the tooltip).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

import streamlit.components.v1 as st_components

from iidm_viewer.powsybl_worker import run


@dataclass(frozen=True)
class DivergingColorScale:
    """Three RGB anchors for a diverging color mapping.

    Given ``value``::

        t     = clamp((value - center) / range, -1, 1)
        target = low_rgb  if t < 0 else high_rgb
        color = lerp(mid_rgb, target, |t|)

    ``center`` is the "nominal / neutral" value (1.0 pu for voltage, 0 MW
    for injection). ``range`` is the deviation that saturates the color.
    """
    center: float
    range: float
    mid_rgb: tuple[int, int, int]
    low_rgb: tuple[int, int, int]
    high_rgb: tuple[int, int, int]


def get_substation_positions(network) -> dict[str, tuple[float, float]]:
    """Return ``{substation_id -> (lat, lon)}`` for substations with valid coords.

    Runs on the pypowsybl worker thread via ``run()``. Returns an empty dict
    when the ``substationPosition`` extension is missing or has no valid
    entries — callers can ``if not positions: return`` and render an info
    message.
    """
    raw = object.__getattribute__(network, "_obj")

    def _extract():
        from pypowsybl.network import get_extensions_names

        if "substationPosition" not in get_extensions_names():
            return {}
        pos_df = raw.get_extensions("substationPosition")
        if pos_df.empty:
            return {}
        subs_df = raw.get_substations().reset_index()
        df = subs_df.merge(pos_df, left_on="id", right_on="id")[
            ["id", "latitude", "longitude"]
        ]
        df = df[
            df["latitude"].between(-90, 90)
            & df["longitude"].between(-180, 180)
        ]
        return {
            str(row["id"]): (float(row["latitude"]), float(row["longitude"]))
            for _, row in df.iterrows()
        }

    return run(_extract)


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
var scale = {scale};
var legendCfg = {legend};
var centerLatLon = {center_latlon};
var zoom = {zoom};
var gradientRadiusMeters = {gradient_radius_m};
var defaultIconRadius = {default_icon_radius};

function lerp(a, b, t) {{ return a + (b - a) * t; }}

function divergingColor(value) {{
  if (value === null || value === undefined || isNaN(value)) {{
    return [160, 160, 160, 0.35];
  }}
  var d = value - scale.center;
  var t = Math.max(-1, Math.min(1, d / scale.range));
  var target = t < 0 ? scale.lo : scale.hi;
  var k = Math.abs(t);
  return [
    Math.round(lerp(scale.mid[0], target[0], k)),
    Math.round(lerp(scale.mid[1], target[1], k)),
    Math.round(lerp(scale.mid[2], target[2], k)),
    0.9
  ];
}}

function toRGBA(c, alpha) {{
  var a = (alpha === undefined) ? c[3] : alpha;
  return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')';
}}

// Start centered on France (same default as the pre-2dac287 Leaflet map).
var map = L.map('map').setView(centerLatLon, zoom);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; OpenStreetMap contributors',
  maxZoom: 18
}}).addTo(map);

if (mode === 'gradient') {{
  records.forEach(function(r) {{
    if (r.value === null || r.value === undefined || isNaN(r.value)) return;
    var c = divergingColor(r.value);
    L.circle([r.lat, r.lon], {{
      radius: gradientRadiusMeters,
      stroke: false,
      fillColor: toRGBA(c, 0.35),
      fillOpacity: 0.35
    }}).addTo(map);
  }});
  records.forEach(function(r) {{
    var c = divergingColor(r.value);
    var m = L.circleMarker([r.lat, r.lon], {{
      radius: 3,
      fillColor: toRGBA(c, 1.0),
      color: '#222',
      weight: 0.7,
      fillOpacity: 0.95
    }}).addTo(map);
    if (r.tooltip) m.bindTooltip(r.tooltip);
  }});
}} else {{
  records.forEach(function(r) {{
    var c = divergingColor(r.value);
    var radius = (r.radius !== undefined && r.radius !== null)
      ? r.radius : defaultIconRadius;
    var m = L.circleMarker([r.lat, r.lon], {{
      radius: radius,
      fillColor: toRGBA(c, 0.95),
      color: '#333',
      weight: 1,
      fillOpacity: 0.95
    }}).addTo(map);
    if (r.tooltip) m.bindTooltip(r.tooltip);
  }});
}}

var legendControl = L.control({{position: 'topright'}});
legendControl.onAdd = function() {{
  var div = L.DomUtil.create('div', 'legend');
  var html = '<b>' + legendCfg.title + '</b>';
  if (legendCfg.subtitle) {{
    html += '<br><span style="font-size:10px;color:#555">' + legendCfg.subtitle + '</span>';
  }}
  html += '<br>';
  legendCfg.stops.forEach(function(stop) {{
    var c = divergingColor(stop.value);
    html += '<span style="display:inline-block;width:16px;height:12px;background:' +
      toRGBA(c, 0.95) + ';margin-right:6px;vertical-align:middle;border:1px solid #999"></span>' +
      stop.label + '<br>';
  }});
  html += '<span style="display:inline-block;width:16px;height:12px;background:rgba(160,160,160,0.35);margin-right:6px;vertical-align:middle;border:1px solid #999"></span>no data';
  div.innerHTML = html;
  return div;
}};
legendControl.addTo(map);

setTimeout(function() {{ map.invalidateSize(); }}, 200);
</script>
</body>
</html>
"""


def default_legend_stops(
    scale: DivergingColorScale,
    *,
    unit: str = "",
    decimals: int = 3,
    signed: bool = False,
) -> list[tuple[float, str]]:
    """Build five default legend stops at ±range, ±range/2, center.

    ``signed`` adds an explicit ``+`` in front of positive stops — useful
    for signed quantities where the center is 0 (e.g. injection).
    """
    c = scale.center
    r = scale.range
    fractions = (-1.0, -0.5, 0.0, 0.5, 1.0)
    stops: list[tuple[float, str]] = []
    for f in fractions:
        value = c + f * r
        if signed:
            if abs(value) < 10 ** -decimals:
                label_num = "0"
            else:
                label_num = f"{value:+.{decimals}f}".rstrip("0").rstrip(".")
                if not label_num or label_num in ("+", "-"):
                    label_num = f"{value:+.{decimals}f}"
        else:
            label_num = f"{value:.{decimals}f}"
        label = f"{label_num} {unit}".strip()
        stops.append((value, label))
    return stops


def render_scalar_map(
    records: Iterable[dict],
    *,
    mode: str,
    color_scale: DivergingColorScale,
    legend_title: str,
    legend_subtitle: str = "",
    legend_stops: list[tuple[float, str]] | None = None,
    height: int = 620,
    center_latlon: tuple[float, float] = (46.6, 2.5),
    zoom: int = 6,
    gradient_radius_m: float = 25000.0,
    default_icon_radius: float = 7.0,
) -> None:
    """Inject a Leaflet map into the current Streamlit container.

    Record shape::

        {"id": str, "lat": float, "lon": float,
         "value": float | None,        # None renders as grey "no data"
         "tooltip": str (optional),    # HTML, shown on marker hover
         "radius": float (optional)}   # overrides default_icon_radius (icons mode only)

    ``mode`` is ``"icons"`` or ``"gradient"``.
    ``legend_stops`` default to ``default_legend_stops(color_scale)`` when
    omitted, which is the right default for voltage (centered, ±5 stops);
    callers with signed or unit-bearing scales should pass their own.
    """
    records_list = list(records)
    if legend_stops is None:
        legend_stops = default_legend_stops(color_scale)

    scale_js = {
        "center": float(color_scale.center),
        "range": float(color_scale.range),
        "mid": list(color_scale.mid_rgb),
        "lo": list(color_scale.low_rgb),
        "hi": list(color_scale.high_rgb),
    }
    legend_js = {
        "title": legend_title,
        "subtitle": legend_subtitle,
        "stops": [{"value": float(v), "label": l} for v, l in legend_stops],
    }
    html = _LEAFLET_HTML.format(
        height=height,
        records=json.dumps(records_list),
        mode=json.dumps(mode),
        scale=json.dumps(scale_js),
        legend=json.dumps(legend_js),
        center_latlon=json.dumps(list(center_latlon)),
        zoom=json.dumps(int(zoom)),
        gradient_radius_m=json.dumps(float(gradient_radius_m)),
        default_icon_radius=json.dumps(float(default_icon_radius)),
    )
    st_components.html(html, height=height + 20)
