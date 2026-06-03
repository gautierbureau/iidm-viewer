"""Host-agnostic cache backend for the per-network DataFrame and diagram caches.

The Streamlit, PySide6 and NiceGUI hosts each plug in their own storage
backend (``st.session_state``, plain ``dict``); the slot names, invalidation
rules and load-flow generation counter live here so all three hosts share
them.

Today only the Streamlit host actively uses these caches at runtime
(:mod:`iidm_viewer.caches` wraps this module). Qt and NiceGUI keep small
local SVG dicts and re-fetch DataFrames on every refresh; once they adopt
:class:`CacheBackend` they get the same speedup and the same invalidation
contract.

The backend is intentionally minimal:

* :class:`CacheBackend` is a ``Protocol`` that exposes the dict-like methods
  every getter in :mod:`iidm_viewer.caches` already uses.
* :class:`DictBackend` is the default concrete implementation, used by
  PySide6 / NiceGUI hosts and by tests.
* Slot-name constants (``LINES_ALL`` etc.) are the single source of truth
  for the ~25 cache keys spread across the codebase.
* Slot groupings (:data:`TOPOLOGY_SLOTS`, :data:`LOAD_FLOW_SLOTS`,
  :data:`GEOGRAPHY_SLOTS`, :data:`NETWORK_REPLACE_SLOTS`) drive the three
  invalidation hooks (:func:`invalidate_topology`,
  :func:`invalidate_load_flow`, :func:`invalidate_network_replace`).
"""
from __future__ import annotations

from typing import Any, Iterable, Protocol


class CacheBackend(Protocol):
    """Minimal dict-like storage interface a host implements."""

    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...
    def setdefault(self, key: str, default: Any) -> Any: ...
    def pop(self, key: str, default: Any = None) -> Any: ...
    def keys(self) -> Iterable[str]: ...


class DictBackend:
    """Plain-``dict`` :class:`CacheBackend`, used by Qt/NiceGUI and tests."""

    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._d[key] = value

    def setdefault(self, key: str, default: Any) -> Any:
        return self._d.setdefault(key, default)

    def pop(self, key: str, default: Any = None) -> Any:
        return self._d.pop(key, default)

    def keys(self) -> Iterable[str]:
        return list(self._d.keys())


# --- Slot names (single source of truth) ------------------------------------

LF_GEN = "_lf_gen"

# Raw per-network DataFrames
LINES_ALL = "_lines_all_cache"
TWO_WT_ALL = "_2wt_all_cache"
THREE_WT_ALL = "_3wt_all_cache"
BUSES_ALL = "_buses_all"
GENERATORS_ALL = "_generators_all_cache"
SHUNTS_ALL = "_shunts_all_cache"
SVC_ALL = "_svc_all_cache"

# Derived / merged DataFrames
BUS_VOLTAGES = "_bus_voltages_cache"
SHUNTS_ENRICHED = "_shunts_enriched_cache"
SVCS_ENRICHED = "_svcs_enriched_cache"
LOADING = "_loading_cache"
DE_COMPONENT = "_de_component_cache"
ENRICHED_COMPONENT = "_enriched_component_cache"
EXT_DF = "_ext_df_cache"

# Lookups
VL_LOOKUP = "_vl_lookup_cache"
VL_NOMINAL_V = "_vl_nominal_v_cache"
OPLIMITS = "_oplimits_cache"
REACTIVE_CURVES = "_reactive_curves_cache"

# Diagrams
SLD = "_sld_cache"
NAD = "_nad_cache"
BBT = "_bbt_cache"

# Tabs / panels
OVERVIEW = "_overview_cache"
SA_ID = "_sa_id_cache"
SA_MANUAL_DF = "_sa_manual_df_cache"

# Maps
MAP_DATA = "_map_data_cache"
SUBSTATION_POSITIONS = "_substation_positions_cache"
VOLTAGE_MAP = "_voltage_map_cache"
INJECTION_MAP = "_injection_map_cache"


# --- Slot groupings for invalidation ----------------------------------------

#: Caches reflecting the component set / attributes (topology).
TOPOLOGY_SLOTS: tuple[str, ...] = (
    VL_LOOKUP,
    VL_NOMINAL_V,
    OVERVIEW,
    LINES_ALL,
    TWO_WT_ALL,
    OPLIMITS,
    REACTIVE_CURVES,
    BBT,
    SLD,  # switch open/closed state is topology, not load-flow
    SA_ID,
    SA_MANUAL_DF,
    DE_COMPONENT,
    EXT_DF,
    ENRICHED_COMPONENT,
)

#: Caches additionally tied to geographic layout (lat/lon extensions).
GEOGRAPHY_SLOTS: tuple[str, ...] = (
    MAP_DATA,
)

#: Caches depending on load-flow results (p, q, i, bus voltages).
LOAD_FLOW_SLOTS: tuple[str, ...] = (
    NAD,
    BUSES_ALL,
    "_buses_all_net",   # stale key written by old diagrams._get_buses_all — clean up
    SHUNTS_ALL,
    SVC_ALL,
    GENERATORS_ALL,
    THREE_WT_ALL,
    BUS_VOLTAGES,
    SHUNTS_ENRICHED,
    SVCS_ENRICHED,
    LOADING,
)

#: Pre-rendered map payloads / positions — only need to clear when the
#: network itself is swapped out.
NETWORK_REPLACE_SLOTS: tuple[str, ...] = (
    SUBSTATION_POSITIONS,
    VOLTAGE_MAP,
    INJECTION_MAP,
)


# --- Load-flow generation counter -------------------------------------------

def lf_gen(backend: CacheBackend) -> int:
    """Read the load-flow generation counter from ``backend``."""
    return backend.get(LF_GEN, 0)


def bump_lf_gen(backend: CacheBackend) -> int:
    """Increment the LF generation counter and return the new value."""
    new = lf_gen(backend) + 1
    backend.set(LF_GEN, new)
    return new


def reset_lf_gen(backend: CacheBackend) -> None:
    """Reset the LF generation counter to ``0`` (network replace)."""
    backend.set(LF_GEN, 0)


# --- Cache keying -----------------------------------------------------------

def cache_key(net_key: int, gen: int, *extra: Any) -> tuple:
    """Build a cache key tuple from network id, LF generation and extras."""
    if extra:
        return (net_key, gen, *extra)
    return (net_key, gen)


# --- Invalidation -----------------------------------------------------------
#
# Three levels, called by the host's mutation entry points to keep every
# cache consistent with the underlying network:
#
# - Topology edit (add/remove/update elements) → network rows change.
# - Load flow → flow-carrying columns (p/q/i) + bus voltages change.
# - Network replace (file upload or blank network) → everything.
#
# Several caches are keyed by ``(net_key, lf_gen)`` and self-invalidate
# when ``LF_GEN`` bumps, but we pop them explicitly to free memory and
# keep the behaviour visible from a single place.


def _pop_all(backend: CacheBackend, slots: Iterable[str]) -> None:
    for slot in slots:
        backend.pop(slot, None)


def invalidate_topology(
    backend: CacheBackend, *, affects_geography: bool = False
) -> None:
    """Pop caches invalidated by a topology edit.

    Pass ``affects_geography=True`` from create_* sites that add or move
    elements carrying a position extension (substations, lines with
    ``linePosition``).
    """
    _pop_all(backend, TOPOLOGY_SLOTS)
    if affects_geography:
        _pop_all(backend, GEOGRAPHY_SLOTS)


def invalidate_load_flow(backend: CacheBackend) -> None:
    """Bump ``LF_GEN`` and pop caches affected by the new flow solution.

    ``LF_GEN`` alone would be enough for caches keyed by
    ``(net_key, lf_gen)``; we still pop explicitly to free memory and
    cover caches (:data:`NAD`, :data:`SLD`, :data:`BUSES_ALL`) that are not
    keyed by ``lf_gen``.
    """
    bump_lf_gen(backend)
    _pop_all(backend, TOPOLOGY_SLOTS)
    _pop_all(backend, LOAD_FLOW_SLOTS)


def invalidate_network_replace(backend: CacheBackend) -> None:
    """Pop every per-network cache — used by ``load_network`` /
    ``create_empty_network``."""
    _pop_all(
        backend,
        TOPOLOGY_SLOTS + GEOGRAPHY_SLOTS + LOAD_FLOW_SLOTS + NETWORK_REPLACE_SLOTS,
    )
    reset_lf_gen(backend)
