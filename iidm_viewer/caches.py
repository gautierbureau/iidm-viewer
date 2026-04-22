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

import pandas as pd
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


def get_3wt_all(network):
    """Cache ``get_3_windings_transformers(all_attributes=True)`` per ``(net_key, lf_gen)``."""
    return _get_all_attrs(network, "_3wt_all_cache", "get_3_windings_transformers")


# Map pypowsybl method names to their already-cached getters above so
# get_component_df can reuse them without a second fetch.
_METHOD_TO_CACHE_FN: dict = {
    "get_lines": get_lines_all,
    "get_2_windings_transformers": get_2wt_all,
    "get_3_windings_transformers": get_3wt_all,
    "get_generators": get_generators_all,
    "get_buses": get_buses_all,
    "get_shunt_compensators": get_shunts_all,
    "get_static_var_compensators": get_svc_all,
}


def get_component_df(network, method_name: str) -> pd.DataFrame:
    """Return ``network.method_name(all_attributes=True)`` cached per ``(net_key, lf_gen)``.

    For types already cached in this module (lines, generators, buses, etc.)
    delegates to the existing getter so the DataFrame is shared across tabs.
    For all other component types a general ``"_de_component_cache"`` dict is
    used, keyed by ``(net_key, lf_gen, method_name)``.
    Returns an empty DataFrame on failure.
    """
    known = _METHOD_TO_CACHE_FN.get(method_name)
    if known is not None:
        return known(network)

    cache = st.session_state.setdefault("_de_component_cache", {})
    key = _cache_key(network) + (method_name,)
    if key in cache:
        return cache[key]

    df = getattr(network, method_name)(all_attributes=True)
    cache[key] = df
    return df


def get_extension_df(network, extension_name: str) -> pd.DataFrame:
    """Return ``network.get_extensions(extension_name)`` cached per ``(net_key, lf_gen)``.

    Some extensions carry post-LF attributes, so the cache is keyed by
    ``(net_key, lf_gen)``. Invalidated via ``_TOPOLOGY_CACHE_KEYS`` on every
    topology or extension edit.
    Returns an empty DataFrame on failure or when the extension is absent.
    """
    cache = st.session_state.setdefault("_ext_df_cache", {})
    key = _cache_key(network) + (extension_name,)
    if key in cache:
        return cache[key]

    df = network.get_extensions(extension_name)
    if df is None:
        df = pd.DataFrame()

    # Only cache non-empty results: an absent extension can become present after
    # create_extension, and in AppTest the session-state invalidation from
    # invalidate_on_topology_change may not reach at.session_state when called
    # outside at.run(). Re-fetching on every call costs 2 RT but is correct.
    if not df.empty:
        cache[key] = df
    return df


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


def get_vl_lookup(network) -> pd.DataFrame:
    """VL id → (substation_id, nominal_v, country) lookup, cached by ``net_key``.

    Fetches voltage-levels and substations (2 RT) and merges them.  Cached by
    topology (not load-flow) since nominal voltages and country assignments
    don't change after a load flow.
    Columns: ``id`` (VL id), ``substation_id``, ``nominal_v``, ``country``.
    """
    key = _net_key(network)
    cached = st.session_state.get("_vl_lookup_cache")
    if cached is not None and cached.get("key") == key:
        return cached["df"]
    try:
        vls = network.get_voltage_levels(
            attributes=["substation_id", "nominal_v"]
        ).reset_index()
        subs = (
            network.get_substations(attributes=["country"])
            .reset_index()
            .rename(columns={"id": "substation_id"})
        )
        vls["id"] = vls["id"].astype(str)
        vls["substation_id"] = vls["substation_id"].astype(str)
        subs["substation_id"] = subs["substation_id"].astype(str)
        df = vls.merge(subs, on="substation_id", how="left")
    except Exception:
        df = pd.DataFrame(columns=["id", "substation_id", "nominal_v", "country"])
    st.session_state["_vl_lookup_cache"] = {"key": key, "df": df}
    return df


def enrich_with_joins(df: pd.DataFrame, vl_lookup: pd.DataFrame) -> pd.DataFrame:
    """Left-join VL/substation-derived columns (``nominal_v``, ``country``) onto ``df``.

    Inspects ``df`` for ``substation_id``, ``voltage_level_id``, and
    ``voltage_level{1,2}_id`` columns and adds the corresponding lookup
    columns when they are missing.  Returns a new DataFrame; the index is
    preserved when possible.
    """
    idx_name = df.index.name
    out = df.reset_index()

    if "substation_id" in out.columns and "country" not in out.columns:
        out = out.merge(
            vl_lookup[["substation_id", "country"]].drop_duplicates("substation_id"),
            on="substation_id",
            how="left",
        )

    if "voltage_level_id" in out.columns:
        missing = [c for c in ("nominal_v", "country") if c not in out.columns]
        if missing:
            lookup = vl_lookup.rename(columns={"id": "voltage_level_id"})[
                ["voltage_level_id", *missing]
            ].copy()
            lookup["voltage_level_id"] = lookup["voltage_level_id"].astype(str)
            out["voltage_level_id"] = out["voltage_level_id"].astype(str)
            out = out.merge(lookup, on="voltage_level_id", how="left")

    for side in ("1", "2"):
        col = f"voltage_level{side}_id"
        if col in out.columns:
            lookup = vl_lookup.rename(
                columns={
                    "id": col,
                    "nominal_v": f"nominal_v{side}",
                    "country": f"country{side}",
                }
            )[[col, f"nominal_v{side}", f"country{side}"]].copy()
            lookup[col] = lookup[col].astype(str)
            out[col] = out[col].astype(str)
            out = out.merge(lookup, on=col, how="left")

    if idx_name and idx_name in out.columns:
        out = out.set_index(idx_name)
    return out


def get_enriched_component(network, method_name: str) -> pd.DataFrame:
    """Component DF enriched with VL-derived columns, cached per ``(net_key, lf_gen)``.

    Delegates to :func:`get_component_df` then applies :func:`enrich_with_joins`
    so callers never re-run the merge on repeated reruns.  The enriched
    **full** DataFrame is cached; callers should apply their own VL or ID
    filters on the result rather than before calling this function.
    Returns an empty DataFrame when the component type is absent or fails.
    """
    cache = st.session_state.setdefault("_enriched_component_cache", {})
    key = _cache_key(network) + (method_name,)
    if key in cache:
        return cache[key]
    df = get_component_df(network, method_name)
    if not df.empty:
        df = enrich_with_joins(df, get_vl_lookup(network))
    cache[key] = df
    return df


def get_bus_voltages(network) -> pd.DataFrame:
    """Buses merged with ``nominal_v`` and ``v_pu``, cached per ``(net_key, lf_gen)``.

    Columns: ``bus_id``, ``voltage_level_id``, ``nominal_v``, ``v_mag``, ``v_pu``.
    ``v_mag`` / ``v_pu`` are NaN when no load flow has run.
    """
    key = _cache_key(network)
    cached = st.session_state.get("_bus_voltages_cache")
    if cached is not None and cached.get("key") == key:
        return cached["df"]
    buses = get_buses_all(network)
    if buses.empty:
        df = pd.DataFrame(
            columns=["bus_id", "voltage_level_id", "nominal_v", "v_mag", "v_pu"]
        )
    else:
        buses = buses.reset_index()
        buses["voltage_level_id"] = buses["voltage_level_id"].astype(str)
        lookup = get_vl_nominal_v(network)
        merged = buses.merge(lookup, on="voltage_level_id", how="left")
        merged = merged.rename(columns={"id": "bus_id"})
        merged["v_pu"] = merged["v_mag"] / merged["nominal_v"]
        df = merged[["bus_id", "voltage_level_id", "nominal_v", "v_mag", "v_pu"]]
    st.session_state["_bus_voltages_cache"] = {"key": key, "df": df}
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
    "_sa_id_cache",
    "_sa_manual_df_cache",
    "_de_component_cache",
    "_ext_df_cache",
    "_enriched_component_cache",  # dict keyed by (net_key, lf_gen, method_name)
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
    "_3wt_all_cache",
    "_prewarm_idx",          # restart background prewarming after a load flow
    "_bus_voltages_cache",   # buses + nominal_v + v_pu
    "_shunts_enriched_cache",
    "_svcs_enriched_cache",
    "_loading_cache",        # operational limits loading %
)

# Caches holding pre-rendered map payloads or positions — only need to
# clear when the network itself is swapped out.
_NETWORK_REPLACE_CACHE_KEYS = (
    "_substation_positions_cache",
    "_voltage_map_cache",
    "_injection_map_cache",
    "_prewarm_idx",     # restart background prewarming on network replace
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
