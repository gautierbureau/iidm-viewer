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
    not mutated.
    """
    key = _cache_key(network)
    cached = st.session_state.get(session_key)
    if cached is not None and cached.get("key") == key:
        return cached["df"]
    df = getattr(network, getter_name)(all_attributes=True)
    st.session_state[session_key] = {"key": key, "df": df}
    return df


def get_lines_all(network):
    return _get_all_attrs(network, "_lines_all_cache", "get_lines")


def get_2wt_all(network):
    return _get_all_attrs(network, "_2wt_all_cache", "get_2_windings_transformers")


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
