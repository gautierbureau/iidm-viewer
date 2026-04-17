"""Geographical network map tab.

Data extraction runs on the pypowsybl worker thread and produces three
lists matching the shape expected by
``@powsybl/network-map-layers``:

    substations:          MapSubstation[]       (id, name, voltageLevels[])
    substation_positions: GeoDataSubstation[]   (id, coordinate{lon,lat})
    lines:                MapLine[]             (lines + 2W transformers)

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
        from pypowsybl.network import get_extensions_names

        if "substationPosition" not in get_extensions_names():
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

        return substations, substation_positions, lines

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

    substations, substation_positions, lines = data

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
        height=670,
        key="network_map",
    )

    st.caption(
        f"{len(substations)} substations, {len(lines)} branches on the map"
    )
