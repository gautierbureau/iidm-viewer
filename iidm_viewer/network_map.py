import json

import pandas as pd
import streamlit as st
import streamlit.components.v1 as st_components

from iidm_viewer.powsybl_worker import run


def _extract_map_data(network):
    """Extract substation positions, VL info, and line connectivity from the network."""
    raw = object.__getattribute__(network, "_obj")

    def _extract():
        from pypowsybl.network import get_extensions_names

        spos = []
        smap = []
        lmap = []

        if "substationPosition" not in get_extensions_names():
            return spos, smap, lmap

        subs_positions_df = raw.get_extensions("substationPosition")
        if subs_positions_df.empty:
            return spos, smap, lmap

        subs_df = raw.get_substations()
        subs_positions_df = subs_df.merge(
            subs_positions_df, left_on="id", right_on="id"
        )[["name", "latitude", "longitude"]]
        # Filter invalid coordinates
        subs_positions_df = subs_positions_df[
            subs_positions_df["latitude"].between(-90, 90)
            & subs_positions_df["longitude"].between(-180, 180)
        ]

        vls_df = raw.get_voltage_levels().reset_index()
        vls_subs_df = vls_df.merge(
            subs_positions_df, left_on="substation_id", right_on="id"
        )[["id", "name_x", "substation_id", "name_y", "nominal_v", "latitude", "longitude"]]

        # Lines
        lines_df = raw.get_lines().reset_index()[
            ["id", "name", "voltage_level1_id", "voltage_level2_id",
             "connected1", "connected2", "p1", "p2", "i1", "i2"]
        ]
        lines_df = lines_df.fillna(0)
        lmap = lines_df.rename(columns={
            "voltage_level1_id": "voltageLevelId1",
            "voltage_level2_id": "voltageLevelId2",
            "connected1": "terminal1Connected",
            "connected2": "terminal2Connected",
        }).to_dict(orient="records")

        # Transformers as lines too
        try:
            t2w_df = raw.get_2_windings_transformers().reset_index()[
                ["id", "name", "voltage_level1_id", "voltage_level2_id",
                 "connected1", "connected2", "p1", "p2", "i1", "i2"]
            ]
            t2w_df = t2w_df.fillna(0)
            t2w_records = t2w_df.rename(columns={
                "voltage_level1_id": "voltageLevelId1",
                "voltage_level2_id": "voltageLevelId2",
                "connected1": "terminal1Connected",
                "connected2": "terminal2Connected",
            }).to_dict(orient="records")
            lmap.extend(t2w_records)
        except Exception:
            pass

        # Substation map
        for s_id, group in vls_subs_df.groupby("substation_id"):
            smap.append({
                "id": s_id,
                "name": group["name_y"].iloc[0],
                "voltageLevels": [
                    {
                        "id": row["id"],
                        "name": row["name_x"],
                        "substationId": row["substation_id"],
                        "nominalV": row["nominal_v"],
                    }
                    for _, row in group.iterrows()
                ],
            })

        # Substation positions
        spos = [
            {
                "id": row["id"],
                "coordinate": {"lat": row["latitude"], "lon": row["longitude"]},
            }
            for _, row in subs_positions_df.reset_index().iterrows()
        ]

        # Build VL -> substation coordinate lookup and VL -> nominal voltage
        vl_coords = {}
        vl_nv = {}
        for _, row in vls_subs_df.iterrows():
            vl_coords[row["id"]] = {"lat": row["latitude"], "lon": row["longitude"]}
            vl_nv[row["id"]] = row["nominal_v"]

        # Line geo positions disabled for now — linePosition data needs
        # more investigation to chain sub-segments correctly.
        lpos = {}

        return spos, smap, lmap, vl_coords, vl_nv, lpos

    return run(_extract)


_LEAFLET_HTML = """
<!DOCTYPE html>
<html>
<head>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body {{ margin: 0; }}
  #map {{ width: 100%; height: {height}px; }}
</style>
</head>
<body>
<div id="map"></div>
<script>
var spos = {spos};
var smap = {smap};
var lmap = {lmap};
var vlCoords = {vl_coords};
var vlNv = {vl_nv};
var selectedVl = {selected_vl};
var nominalVoltages = {nominal_voltages};

// Color by nominal voltage — matches pypowsybl conventions
var NV_COLORS = [
  [380, '#ff0000'],      // 400 kV — rouge
  [225, '#228b22'],      // 225 kV — vert
  [150, '#6495ed'],      // 150 kV — bleu clair
  [90,  '#ff8c00'],      // 90 kV  — orange fonce
  [63,  '#a020f0'],      // 63 kV  — violet
  [42,  '#ff69b4'],      // 42 kV  — rose
  [0,   '#6b8e23']       // below  — vert kaki
];

var NV_LABELS = [
  [380, '400 kV'],
  [225, '225 kV'],
  [150, '150 kV'],
  [90,  '90 kV'],
  [63,  '63 kV'],
  [42,  '42 kV'],
  [0,   '< 42 kV']
];

function nvColor(nv) {{
  for (var i = 0; i < NV_COLORS.length; i++) {{
    if (nv >= NV_COLORS[i][0]) return NV_COLORS[i][1];
  }}
  return '#6b8e23';
}}

// Build substation lookup
var subCoords = {{}};
spos.forEach(function(s) {{
  subCoords[s.id] = [s.coordinate.lat, s.coordinate.lon];
}});

// Map — start centered on France
var map = L.map('map').setView([46.6, 2.5], 6);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; OpenStreetMap contributors',
  maxZoom: 18
}}).addTo(map);

var markers = [];

// Draw lines — colored by the higher nominal voltage of the two ends
// Use line geo positions (waypoints) when available
lmap.forEach(function(line) {{
  var c1 = vlCoords[line.voltageLevelId1];
  var c2 = vlCoords[line.voltageLevelId2];
  if (!c1 || !c2) return;

  var nv1 = vlNv[line.voltageLevelId1] || 0;
  var nv2 = vlNv[line.voltageLevelId2] || 0;
  var maxNv = Math.max(nv1, nv2);

  var connected = line.terminal1Connected && line.terminal2Connected;
  var lineColor = connected ? nvColor(maxNv) : '#cccccc';

  var coords = [[c1.lat, c1.lon], [c2.lat, c2.lon]];

  var polyline = L.polyline(
    coords,
    {{
      color: lineColor,
      weight: connected ? 1.2 : 0.6,
      opacity: connected ? 0.7 : 0.3,
      dashArray: connected ? null : '5,5'
    }}
  ).addTo(map);

  var p1 = Math.abs(line.p1).toFixed(1);
  var i1 = Math.abs(line.i1).toFixed(1);
  polyline.bindTooltip(
    '<b>' + line.id + '</b>' +
    (line.name ? ' (' + line.name + ')' : '') +
    '<br>P1: ' + p1 + ' MW, I1: ' + i1 + ' A'
  );
}});

// Draw substations — concentric circles, one per voltage level
smap.forEach(function(sub) {{
  var coord = subCoords[sub.id];
  if (!coord) return;

  var vlLabels = [];
  sub.voltageLevels.forEach(function(vl) {{
    vlLabels.push(
      '<span style="color:' + nvColor(vl.nominalV) + '">\u25CF</span> ' +
      vl.id + (vl.name ? ' (' + vl.name + ')' : '') + ' ' + vl.nominalV + ' kV'
    );
  }});

  var isSelected = false;
  if (selectedVl) {{
    sub.voltageLevels.forEach(function(vl) {{
      if (vl.id === selectedVl) isSelected = true;
    }});
  }}

  var tooltip = '<b>' + sub.id + '</b>' +
    (sub.name ? ' (' + sub.name + ')' : '') +
    '<br>' + vlLabels.join('<br>');

  // Sort VLs by descending nominalV so largest ring is drawn first
  var sortedVls = sub.voltageLevels.slice().sort(function(a, b) {{
    return b.nominalV - a.nominalV;
  }});

  var maxRadius = isSelected ? 5 : 4;
  var nVls = sortedVls.length;
  // Distribute rings evenly between 1.5 and maxRadius
  var ringStep = nVls > 1 ? (maxRadius - 1.5) / (nVls - 1) : 0;

  for (var vi = 0; vi < nVls; vi++) {{
    var vl = sortedVls[vi];
    var r = maxRadius - vi * ringStep;
    var isVlSelected = isSelected && vl.id === selectedVl;
    var marker = L.circleMarker(coord, {{
      radius: r,
      fillColor: nvColor(vl.nominalV),
      color: isVlSelected ? '#000000' : nvColor(vl.nominalV),
      weight: isVlSelected ? 2 : 1,
      fillOpacity: 0.9
    }}).addTo(map);
    marker.bindTooltip(tooltip);
    markers.push(marker);
  }}
}});

// Legend
var legend = L.control({{position: 'topright'}});
legend.onAdd = function(map) {{
  var div = L.DomUtil.create('div', 'legend');
  div.style.cssText = 'background:white; padding:8px 12px; border-radius:5px; box-shadow:0 1px 5px rgba(0,0,0,0.3); font-size:12px; line-height:1.6;';
  var html = '<b>Nominal Voltage</b><br>';
  // Only show voltage levels present in the network
  var presentNvs = new Set();
  nominalVoltages.forEach(function(nv) {{ presentNvs.add(nv); }});
  NV_COLORS.forEach(function(entry) {{
    var threshold = entry[0];
    var color = entry[1];
    var label = '';
    for (var i = 0; i < NV_LABELS.length; i++) {{
      if (NV_LABELS[i][0] === threshold) {{ label = NV_LABELS[i][1]; break; }}
    }}
    // Show this legend entry if any network NV falls in this bucket
    var show = false;
    presentNvs.forEach(function(nv) {{
      if (nvColor(nv) === color) show = true;
    }});
    if (show) {{
      html += '<span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:' + color + ';margin-right:6px;vertical-align:middle;"></span>' + label + '<br>';
    }}
  }});
  div.innerHTML = html;
  return div;
}};
legend.addTo(map);

// Leaflet needs a kick inside the Streamlit iframe to render tiles correctly
setTimeout(function() {{ map.invalidateSize(); }}, 200);
</script>
</body>
</html>
"""


def _get_cached_map_data(network):
    """Return map data, cached in session state to avoid re-extraction on every rerun."""
    cache = st.session_state.get("_map_data_cache")
    if cache is not None:
        return cache
    result = _extract_map_data(network)
    st.session_state["_map_data_cache"] = result
    return result


def render_network_map(network, selected_vl):
    result = _get_cached_map_data(network)

    spos = result[0]
    smap = result[1]
    lmap = result[2]
    vl_coords = result[3] if len(result) > 3 else {}
    vl_nv = result[4] if len(result) > 4 else {}

    if not spos:
        st.info(
            "No geographical data found in this network. "
            "The network needs a 'substationPosition' extension with latitude/longitude coordinates."
        )
        return

    # Nominal voltage legend
    vls_df = network.get_voltage_levels(attributes=["nominal_v"])
    nominal_voltages = sorted(vls_df["nominal_v"].unique(), reverse=True)

    html = _LEAFLET_HTML.format(
        height=650,
        spos=json.dumps(spos),
        smap=json.dumps(smap),
        lmap=json.dumps(lmap),
        vl_coords=json.dumps(vl_coords),
        vl_nv=json.dumps(vl_nv),
        selected_vl=json.dumps(selected_vl),
        nominal_voltages=json.dumps([float(v) for v in nominal_voltages]),
    )

    st_components.html(html, height=670)

    st.caption(f"{len(smap)} substations, {len(lmap)} branches on the map")
