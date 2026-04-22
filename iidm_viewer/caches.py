"""Shared per-network DataFrame caches.

The heavy `get_lines(all_attributes=True)` / `get_2_windings_transformers(all_attributes=True)`
fetches are requested by Operational Limits, Overview losses, and Pmax on
every rerun. Each call is 2 worker round-trips; on IEEE 14 that's a small
constant, but on real transmission networks the DataFrame is large and the
call also drags a lot of JNI data across the GraalVM boundary.

Caches are keyed by ``(net_key, lf_gen)``:

- ``net_key`` is the identity of the raw pypowsybl network handle; it
  changes when the user loads a new file or clicks "Create empty".
- ``lf_gen`` is a session counter incremented by :func:`state.run_loadflow`.
  A bump makes every flow/loss-carrying column stale.

Topology edits (add/remove a line, update r/x/…) don't bump ``lf_gen``
but *do* change the set of rows. Every ``_vl_lookup_cache`` pop site in
``state.py`` also pops ``_lines_all_cache`` / ``_2wt_all_cache`` /
``_oplimits_cache`` so the next read rebuilds from the live network.
"""
from __future__ import annotations

import streamlit as st


def _net_key(network) -> int:
    try:
        return id(object.__getattribute__(network, "_obj"))
    except AttributeError:
        return id(network)


def _lf_gen() -> int:
    return st.session_state.get("_lf_gen", 0)


def _cache_key(network) -> tuple[int, int]:
    return _net_key(network), _lf_gen()


def _get_all_attrs(network, session_key: str, getter_name: str):
    """Cache ``getattr(network, getter_name)(all_attributes=True)`` per (net, lf_gen).

    Returns the DataFrame with its natural pypowsybl index intact (usually
    the element id). Consumers that want ``reset_index`` should call it
    themselves — ``reset_index`` produces a copy so the cached frame is
    not mutated. Returns an empty DataFrame when the call raises.
    """
    key = _cache_key(network)
    cached = st.session_state.get(session_key)
    if cached is not None and cached.get("key") == key:
        return cached["df"]
    try:
        df = getattr(network, getter_name)(all_attributes=True)
    except Exception:
        return pd.DataFrame()
    st.session_state[session_key] = {"key": key, "df": df}
    return df


def get_lines_all(network):
    return _get_all_attrs(network, "_lines_all_cache", "get_lines")


def get_2wt_all(network):
    return _get_all_attrs(network, "_2wt_all_cache", "get_2_windings_transformers")


def get_buses_all(network):
    """Cache ``get_buses(all_attributes=True)`` per ``(net_key, lf_gen)``.

    Bus voltages (v_mag, v_angle) change after a load flow; the cache is
    auto-invalidated when ``_lf_gen`` bumps and explicitly popped by
    :func:`invalidate_on_load_flow`.
    """
    return _get_all_attrs(network, "_buses_all", "get_buses")


def get_shunts_all(network):
    """Cache ``get_shunt_compensators(all_attributes=True)`` per ``(net_key, lf_gen)``."""
    return _get_all_attrs(network, "_shunts_all_cache", "get_shunt_compensators")


def get_svc_all(network):
    """Cache ``get_static_var_compensators(all_attributes=True)`` per ``(net_key, lf_gen)``."""
    return _get_all_attrs(network, "_svc_all_cache", "get_static_var_compensators")


def get_generators_all(network):
    """Cache ``get_generators(all_attributes=True)`` per ``(net_key, lf_gen)``."""
    return _get_all_attrs(network, "_generators_all_cache", "get_generators")


def get_reactive_curve_points(network) -> pd.DataFrame:
    """Cache ``get_reactive_capability_curve_points()`` per ``net_key``.

    Capability curves are physical properties of the generator — they change
    only when the topology changes, not after a load flow.
    """
    key = _net_key(network)
    cached = st.session_state.get("_reactive_curves_cache")
    if cached is not None and cached.get("key") == key:
        return cached["df"]
    try:
        df = network.get_reactive_capability_curve_points()
    except Exception:
        df = pd.DataFrame()
    st.session_state["_reactive_curves_cache"] = {"key": key, "df": df}
    return df


def get_vl_nominal_v(network) -> pd.DataFrame:
    """Return a ``voltage_level_id`` → ``nominal_v`` lookup, cached by ``net_key``.

    Nominal voltages are topology-dependent but not load-flow-dependent, so
    the cache is keyed by ``net_key`` alone and invalidated by
    :func:`invalidate_on_topology_change`.
    """
    key = _net_key(network)
    cached = st.session_state.get("_vl_nominal_v_cache")
    if cached is not None and cached.get("key") == key:
        return cached["df"]
    try:
        vls = network.get_voltage_levels(attributes=["nominal_v"]).reset_index()
        vls["id"] = vls["id"].astype(str)
        df = vls.rename(columns={"id": "voltage_level_id"})[["voltage_level_id", "nominal_v"]]
    except Exception:
        df = pd.DataFrame(columns=["voltage_level_id", "nominal_v"])
    st.session_state["_vl_nominal_v_cache"] = {"key": key, "df": df}
    return df


def get_operational_limits_df(network):
    """Cache ``get_operational_limits()`` per network.

    Limits are topology-dependent but unaffected by load flow; we key on
    ``net_key`` alone and let the topology-edit invalidation sites in
    ``state.py`` pop the cache when needed.
    """
    key = _net_key(network)
    cached = st.session_state.get("_oplimits_cache")
    if cached is not None and cached.get("key") == key:
        return cached["df"]
    df = network.get_operational_limits()
    st.session_state["_oplimits_cache"] = {"key": key, "df": df}
    return df


# --- Invalidation ---
#
# Three levels, called from ``state.py`` to keep every pypowsybl-facing
# cache consistent with the underlying network:
#
# - Topology edit (add/remove/update elements) → network rows change.
# - Load flow → flow-carrying columns (p/q/i) + bus voltages change.
# - Network replace (file upload or blank network) → everything.
#
# Several caches are keyed by ``(net_key, lf_gen)`` and self-invalidate
# when ``_lf_gen`` bumps, but we pop them explicitly to free memory and
# keep the behavior visible from a single place.

# Caches reflecting the component set / attributes (topology).
_TOPOLOGY_CACHE_KEYS = (
    "_vl_lookup_cache",
    "_vl_nominal_v_cache",
    "_overview_cache",
    "_lines_all_cache",
    "_2wt_all_cache",
    "_oplimits_cache",
    "_reactive_curves_cache",
    "_bbt_cache",
)

# Caches additionally tied to geographic layout (lat/lon extensions).
_GEOGRAPHY_CACHE_KEYS = (
    "_map_data_cache",
)

# Caches depending on load-flow results (p, q, i, bus voltages).
_LOAD_FLOW_CACHE_KEYS = (
    "_nad_cache",
    "_sld_cache",
    "_buses_all",
    "_buses_all_net",   # stale key written by old diagrams._get_buses_all — clean up
    "_shunts_all_cache",
    "_svc_all_cache",
    "_generators_all_cache",
)

# Caches holding pre-rendered map payloads or positions — only need to
# clear when the network itself is swapped out.
_NETWORK_REPLACE_CACHE_KEYS = (
    "_substation_positions_cache",
    "_voltage_map_cache",
    "_injection_map_cache",
)


def _pop_all(keys) -> None:
    for key in keys:
        st.session_state.pop(key, None)


def invalidate_on_topology_change(affects_geography: bool = False) -> None:
    """Pop caches invalidated by a topology edit.

    Pass ``affects_geography=True`` from create_* sites that add or move
    elements carrying a position extension (substations, lines with
    ``linePosition``).
    """
    _pop_all(_TOPOLOGY_CACHE_KEYS)
    if affects_geography:
        _pop_all(_GEOGRAPHY_CACHE_KEYS)


def invalidate_on_load_flow() -> None:
    """Bump ``_lf_gen`` and pop caches affected by the new flow solution.

    ``_lf_gen`` alone would be enough for caches keyed by
    ``(net_key, lf_gen)``; we still pop explicitly to free memory and
    cover caches (``_nad_cache``, ``_sld_cache``, ``_buses_all``) that
    are not keyed by lf_gen.
    """
    st.session_state["_lf_gen"] = st.session_state.get("_lf_gen", 0) + 1
    _pop_all(_TOPOLOGY_CACHE_KEYS + _LOAD_FLOW_CACHE_KEYS)


def invalidate_on_network_replace() -> None:
    """Pop every per-network cache — used by load_network / create_empty_network."""
    _pop_all(
        _TOPOLOGY_CACHE_KEYS
        + _GEOGRAPHY_CACHE_KEYS
        + _LOAD_FLOW_CACHE_KEYS
        + _NETWORK_REPLACE_CACHE_KEYS
    )
