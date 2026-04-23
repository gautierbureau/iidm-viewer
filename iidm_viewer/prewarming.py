"""Background cache pre-warming for unvisited tabs.

After Overview is rendered the app calls :func:`render_prewarmer` once.
The fragment runs one prewarm step per invocation, increments
``_prewarm_idx``, and re-runs itself via ``st.rerun(scope="fragment")``
until all steps are done.  Each step calls one or more ``caches.get_*``
helpers; on a warm cache those are no-ops.  On first load they populate
the shared caches before the user visits those tabs, so tab switches feel
instantaneous.

``_prewarm_idx`` is reset by :func:`caches.invalidate_on_load_flow` and
:func:`caches.invalidate_on_network_replace` so fresh data is pre-loaded
after each load-flow or network swap.
"""
from __future__ import annotations

import streamlit as st

from iidm_viewer import caches


# ---------------------------------------------------------------------------
# Per-tab prewarm functions
# ---------------------------------------------------------------------------

def _prewarm_overview(network) -> None:
    caches.get_buses_all(network)
    caches.get_lines_all(network)
    caches.get_2wt_all(network)
    caches.get_generators_all(network)
    caches.get_shunts_all(network)
    caches.get_svc_all(network)


def _prewarm_network_map(_network) -> None:
    pass  # geo tiles fetched on demand


def _prewarm_nad(_network) -> None:
    pass  # SVG rendered on demand


def _prewarm_sld(_network) -> None:
    pass  # SVG rendered on demand


def _prewarm_data_explorer(network) -> None:
    caches.get_3wt_all(network)
    caches.get_vl_nominal_v(network)


def _prewarm_extensions(_network) -> None:
    pass  # fetched per-extension on demand


def _prewarm_rcc(network) -> None:
    caches.get_reactive_curve_points(network)


def _prewarm_limits(network) -> None:
    caches.get_operational_limits_df(network)


def _prewarm_pmax(network) -> None:
    # Populates _pmax_cache so first tab visit is instant.
    from iidm_viewer.pmax_visualization import _compute_pmax_data
    _compute_pmax_data(network)


def _prewarm_voltage(_network) -> None:
    pass  # uses get_buses_all (warmed by overview step)


def _prewarm_injection(_network) -> None:
    pass  # geo + flow data, loaded on demand


def _prewarm_security_analysis(network) -> None:
    caches.get_vl_nominal_v(network)  # usually already warm from data-explorer step


def _prewarm_short_circuit(_network) -> None:
    pass  # uses get_vl_nominal_v (warmed above)


# Must match the tab order in app.py (Overview=0 … Short Circuit=12).
_PREWARM_FNS = [
    _prewarm_overview,          # 0
    _prewarm_network_map,       # 1
    _prewarm_nad,               # 2
    _prewarm_sld,               # 3
    _prewarm_data_explorer,     # 4
    _prewarm_extensions,        # 5
    _prewarm_rcc,               # 6
    _prewarm_limits,            # 7
    _prewarm_pmax,              # 8
    _prewarm_voltage,           # 9
    _prewarm_injection,         # 10
    _prewarm_security_analysis, # 11
    _prewarm_short_circuit,     # 12
]

_N = len(_PREWARM_FNS)


@st.fragment(run_every=0.1)
def render_prewarmer(network) -> None:
    """Background fragment that warms one tab's caches per re-run.

    Produces no visible output.  Streamlit re-runs this fragment every
    100 ms (``run_every=0.1``) until all steps are done.  Subsequent runs
    return immediately so the per-poll overhead is just a session-state
    lookup.
    """
    idx = st.session_state.get("_prewarm_idx", 0)
    if idx >= _N:
        return
    try:
        _PREWARM_FNS[idx](network)
    except Exception:
        pass  # prewarm is best-effort; errors surface on actual tab visit
    st.session_state["_prewarm_idx"] = idx + 1
