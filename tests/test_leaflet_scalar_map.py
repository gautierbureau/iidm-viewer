"""Tests for iidm_viewer.leaflet_scalar_map."""
import json
from unittest.mock import patch

import streamlit as st

from iidm_viewer.leaflet_scalar_map import (
    DivergingColorScale,
    _SUBSTATION_POSITIONS_CACHE_KEY,
    _extract_substation_positions,
    clear_substation_positions_cache,
    default_legend_stops,
    get_substation_positions,
)
from iidm_viewer.state import load_network


# ── DivergingColorScale ──────────────────────────────────────────────────────

def test_diverging_color_scale_is_frozen_dataclass():
    scale = DivergingColorScale(
        center=1.0, range=0.05,
        mid_rgb=(255, 255, 224),
        low_rgb=(27, 74, 199),
        high_rgb=(199, 27, 27),
    )
    assert scale.center == 1.0
    assert scale.range == 0.05
    assert scale.mid_rgb == (255, 255, 224)
    # dataclass(frozen=True) — no field reassignment
    try:
        scale.center = 2.0  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("DivergingColorScale should be frozen")


# ── default_legend_stops ─────────────────────────────────────────────────────

def test_default_legend_stops_gives_five_stops():
    scale = DivergingColorScale(
        center=1.0, range=0.05,
        mid_rgb=(0, 0, 0), low_rgb=(0, 0, 0), high_rgb=(0, 0, 0),
    )
    stops = default_legend_stops(scale, unit="pu", decimals=3)
    assert len(stops) == 5
    values = [v for v, _ in stops]
    assert values[0] < values[1] < values[2] < values[3] < values[4]
    assert values[2] == 1.0  # center
    # Symmetrical around center
    assert values[0] + values[4] == 2.0
    assert values[1] + values[3] == 2.0


def test_default_legend_stops_signed_adds_plus_sign():
    scale = DivergingColorScale(
        center=0.0, range=500.0,
        mid_rgb=(0, 0, 0), low_rgb=(0, 0, 0), high_rgb=(0, 0, 0),
    )
    stops = default_legend_stops(scale, unit="MW", decimals=0, signed=True)
    labels = {round(v): lab for v, lab in stops}
    # Center stop at 0 should NOT carry + or - (it's "0" plus unit)
    assert "0" in labels[0]
    assert "+" not in labels[0] and "-" not in labels[0]
    assert "+" in labels[500]
    assert "-" in labels[-500]


# ── get_substation_positions ─────────────────────────────────────────────────

def test_get_substation_positions_empty_without_extension(blank_network):
    assert get_substation_positions(blank_network) == {}


def test_get_substation_positions_empty_on_four_substations(node_breaker_network):
    assert get_substation_positions(node_breaker_network) == {}


def test_get_substation_positions_ieee14_has_coords(xiidm_upload):
    network = load_network(xiidm_upload)
    positions = get_substation_positions(network)
    assert positions, "IEEE14 fixture should have substationPosition extension"
    for sub_id, (lat, lon) in positions.items():
        assert isinstance(sub_id, str)
        assert -90 <= lat <= 90
        assert -180 <= lon <= 180


def test_get_substation_positions_is_json_serializable(xiidm_upload):
    network = load_network(xiidm_upload)
    positions = get_substation_positions(network)
    # dict[str, tuple] — tuples become lists, still serialisable
    as_pairs = {k: list(v) for k, v in positions.items()}
    json.dumps(as_pairs)


# ── caching ──────────────────────────────────────────────────────────────────

def test_get_substation_positions_caches_result(xiidm_upload):
    """Two consecutive calls hit the worker only once."""
    network = load_network(xiidm_upload)  # load_network pops the cache
    with patch(
        "iidm_viewer.leaflet_scalar_map._extract_substation_positions",
        wraps=_extract_substation_positions,
    ) as spy:
        first = get_substation_positions(network)
        second = get_substation_positions(network)
    assert first == second
    assert spy.call_count == 1


def test_get_substation_positions_populates_session_state(xiidm_upload):
    network = load_network(xiidm_upload)
    assert _SUBSTATION_POSITIONS_CACHE_KEY not in st.session_state
    positions = get_substation_positions(network)
    assert st.session_state[_SUBSTATION_POSITIONS_CACHE_KEY] is positions


def test_clear_substation_positions_cache_forces_reextraction(xiidm_upload):
    network = load_network(xiidm_upload)
    get_substation_positions(network)
    assert _SUBSTATION_POSITIONS_CACHE_KEY in st.session_state
    clear_substation_positions_cache()
    assert _SUBSTATION_POSITIONS_CACHE_KEY not in st.session_state
    with patch(
        "iidm_viewer.leaflet_scalar_map._extract_substation_positions",
        wraps=_extract_substation_positions,
    ) as spy:
        get_substation_positions(network)
    assert spy.call_count == 1


def test_load_network_invalidates_substation_positions_cache(xiidm_upload):
    network = load_network(xiidm_upload)
    get_substation_positions(network)
    assert _SUBSTATION_POSITIONS_CACHE_KEY in st.session_state
    # Loading the same bytes again creates a fresh NetworkProxy + raw object;
    # state.load_network should drop the cached positions.
    load_network(xiidm_upload)
    assert _SUBSTATION_POSITIONS_CACHE_KEY not in st.session_state
