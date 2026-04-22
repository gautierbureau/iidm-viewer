"""Geographical network map tab.

Data extraction runs on the pypowsybl worker thread and produces four
lists matching the shape expected by
``@powsybl/network-map-layers``:

    substations:          MapSubstation[]       (id, name, voltageLevels[])
    substation_positions: GeoDataSubstation[]   (id, coordinate{lon,lat})
    lines:                MapLine[]             (lines + 2W transformers)
    line_positions:       LinePosition[]        (id, coordinates[{lat,lon}])

The frontend (``frontend/map_component/``) consumes them via
``render_interactive_map`` and draws the map with MapLibre + deck.gl.
"""
from __future__ import annotations

import math

import streamlit as st

from iidm_viewer.map_component import render_interactive_map
from iidm_viewer.powsybl_worker import run


def _to_float(val) -> float:
    """Coerce a pandas cell to a finite float (0.0 for NaN / None)."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(f) else f


def _str_or_id(val, fallback: str) -> str:
    """Return val as a non-empty string, falling back to the element id."""
    if val is None:
        return fallback
    s = str(val).strip()
    return s if s else fallback


def _extract_map_data(network):
    """Extract substations / positions / lines from the pypowsybl network.

    Runs on the pypowsybl worker thread via ``run(...)``.
    """
    raw = object.__getattribute__(network, "_obj")

    def _extract():
        # Network-level listing of extensions actually attached to this
        # network.  Cheap lookup that lets us skip the expensive
        # get_extensions() GraalVM calls for extensions that aren't
        # present — notably substationPosition on networks with no geo
        # data, and linePosition on networks that only carry substation
        # coordinates.
        try:
            present_exts = set(raw.get_extensions_names())
        except Exception:
            present_exts = set()

        if "substationPosition" not in present_exts:
            return None

        subs_pos_df = raw.get_extensions("substationPosition")
        if subs_pos_df.empty:
            return None

        subs_df = raw.get_substations().reset_index()
        vls_df = raw.get_voltage_levels().reset_index()

        # Substation positions, filtered to valid lat/lon.
        positions_df = subs_df.merge(subs_pos_df, left_on="id", right_on="id")[
            ["id", "latitude", "longitude"]
        ]
        positions_df = positions_df[
            positions_df["latitude"].between(-90, 90)
            & positions_df["longitude"].between(-180, 180)
        ]
        substation_positions = [
            {
                "id": row["id"],
                "coordinate": {
                    "lon": _to_float(row["longitude"]),
                    "lat": _to_float(row["latitude"]),
                },
            }
            for _, row in positions_df.iterrows()
        ]

        # Substations — each with its voltage levels.
        subs_with_coords = set(positions_df["id"].tolist())
        substations = []
        for _, sub_row in subs_df.iterrows():
            sub_id = sub_row["id"]
            if sub_id not in subs_with_coords:
                continue  # no coords -> the map layer can't place it
            sub_name = _str_or_id(sub_row.get("name"), sub_id)
            sub_vls = vls_df[vls_df["substation_id"] == sub_id]
            substations.append(
                {
                    "id": sub_id,
                    "name": sub_name,
                    "voltageLevels": [
                        {
                            "id": vl_row["id"],
                            "substationId": sub_id,
                            "substationName": sub_name,
                            "nominalV": _to_float(vl_row["nominal_v"]),
                        }
                        for _, vl_row in sub_vls.iterrows()
                    ],
                }
            )

        # Lines + 2W transformers (the map layer treats both as lines).
        line_cols = [
            "id", "name", "voltage_level1_id", "voltage_level2_id",
            "connected1", "connected2", "p1", "p2", "i1", "i2",
        ]
        lines_df = raw.get_lines().reset_index()
        present = [c for c in line_cols if c in lines_df.columns]
        lines_df = lines_df[present].fillna(0)
        lines = [_line_record(r) for _, r in lines_df.iterrows()]

        try:
            t2w_df = raw.get_2_windings_transformers().reset_index()
            present_t = [c for c in line_cols if c in t2w_df.columns]
            t2w_df = t2w_df[present_t].fillna(0)
            lines.extend(_line_record(r) for _, r in t2w_df.iterrows())
        except Exception:
            pass

        # Line positions from the linePosition extension (optional).
        # Each entry: {id, coordinates: [{lat, lon}, ...]} sorted by num.
        # Skipped entirely when the network doesn't carry the extension.
        line_positions = []
        if "linePosition" in present_exts:
            try:
                lpos_df = raw.get_extensions("linePosition").reset_index()
                lpos_df = lpos_df[
                    lpos_df["latitude"].between(-90, 90)
                    & lpos_df["longitude"].between(-180, 180)
                ]
                if not lpos_df.empty:
                    # Vectorised grouping — the previous .apply(lambda +
                    # iterrows) path was O(rows) in Python bytecode and
                    # dominated network-load time on large grids.
                    lpos_df = lpos_df.sort_values("num")
                    lpos_df = lpos_df.rename(
                        columns={"latitude": "lat", "longitude": "lon"}
                    )
                    lpos_df["lat"] = lpos_df["lat"].astype(float)
                    lpos_df["lon"] = lpos_df["lon"].astype(float)
                    for lid, group in lpos_df.groupby("id", sort=False):
                        coords = group[["lat", "lon"]].to_dict("records")
                        if coords:
                            line_positions.append(
                                {"id": str(lid), "coordinates": coords}
                            )
            except Exception:
                pass

        return substations, substation_positions, lines, line_positions

    return run(_extract)


def _line_record(row) -> dict:
    return {
        "id": row["id"],
        "name": _str_or_id(row.get("name"), row["id"]),
        "voltageLevelId1": row["voltage_level1_id"],
        "voltageLevelId2": row["voltage_level2_id"],
        "terminal1Connected": bool(row.get("connected1", True)),
        "terminal2Connected": bool(row.get("connected2", True)),
        "p1": _to_float(row.get("p1")),
        "p2": _to_float(row.get("p2")),
        "i1": _to_float(row.get("i1")),
        "i2": _to_float(row.get("i2")),
    }


def _get_cached_map_data(network):
    """Cache extraction in session state so reruns don't reprocess."""
    cache = st.session_state.get("_map_data_cache")
    if cache is not None:
        return cache
    result = _extract_map_data(network)
    st.session_state["_map_data_cache"] = result
    return result


def render_network_map(network, selected_vl):
    del selected_vl  # reserved for future highlight support
    data = _get_cached_map_data(network)

    if data is None:
        st.info(
            "No geographical data found in this network. "
            "The network needs a 'substationPosition' extension with "
            "latitude/longitude coordinates."
        )
        return

    substations, substation_positions, lines, line_positions = data

    if not substation_positions:
        st.info(
            "The 'substationPosition' extension is present but contained no "
            "valid coordinates."
        )
        return

    render_interactive_map(
        substations=substations,
        substation_positions=substation_positions,
        lines=lines,
        line_positions=line_positions,
        height=670,
        key="network_map",
    )

    line_pos_count = len(line_positions)
    caption = f"{len(substations)} substations, {len(lines)} branches"
    if line_pos_count:
        caption += f", {line_pos_count} lines with detailed geometry"
    st.caption(caption)
