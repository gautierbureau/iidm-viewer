"""Tests for the Streamlit :class:`iidm_viewer.state.AppState` subclass.

Verifies the Streamlit-flavoured AppState correctly proxies its
storage through ``st.session_state`` so the unified host-agnostic API
shares one source of truth with the existing module-level functions.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from iidm_viewer import cache_backend as cb
from iidm_viewer.cache_backend import LF_GEN, LINES_ALL
from iidm_viewer.change_log import ChangeLog
from iidm_viewer.loadflow import LoadFlowResult
from iidm_viewer.state import AppState, app_state


class _FakeSessionState(dict):
    """Dict with attribute-style access — mirrors st.session_state."""
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


# ---------------------------------------------------------------------------
# Storage hooks
# ---------------------------------------------------------------------------


def test_get_reads_from_session_state():
    shared = _FakeSessionState({"network": "X"})
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        state = AppState()
        assert state._get("network") == "X"


def test_set_writes_to_session_state():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        state = AppState()
        state._set("network", "Y")
        assert shared["network"] == "Y"


def test_storage_descriptor_proxies_through_session_state():
    """``state._network = X`` writes into ``st.session_state["network"]``,
    so the legacy module-level functions see the same value."""
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        state = AppState()
        state._network = "X"
        assert shared["network"] == "X"
        assert state.network == "X"


def test_persisted_lf_params_round_trip_via_session_state():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        state = AppState()
        state.lf_generic_params = {"distributed_slack": False}
        assert shared["lf_generic_params"] == {"distributed_slack": False}
        # A fresh instance reads the same persisted value.
        state2 = AppState()
        assert state2.lf_generic_params == {"distributed_slack": False}


# ---------------------------------------------------------------------------
# Notification hooks — Streamlit reruns instead of dispatching listeners
# ---------------------------------------------------------------------------


def test_emit_hooks_are_no_ops():
    state = AppState()
    seen: list = []
    state.on_network_changed(lambda n: seen.append(("net", n)))
    state.on_selected_vl_changed(lambda v: seen.append(("vl", v)))
    state.on_loadflow_completed(lambda r: seen.append(("lf", r)))
    state._emit_network_changed("X")
    state._emit_selected_vl_changed("VL1")
    state._emit_loadflow_completed("R")
    # Streamlit's rerun model carries state changes — no listener fires.
    assert seen == []


# ---------------------------------------------------------------------------
# Cache backend wiring
# ---------------------------------------------------------------------------


def test_cache_backend_is_shared_streamlit_backend():
    """The AppState's cache backend is the same singleton used by the
    module-level functions in ``iidm_viewer.caches``."""
    from iidm_viewer.caches import backend as caches_backend

    state = AppState()
    assert state.cache_backend is caches_backend


def test_install_network_invalidates_via_streamlit_backend():
    shared = _FakeSessionState({
        LINES_ALL: {"marker": "stale"},
        LF_GEN: 5,
    })
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        state = AppState()
        with patch(
            "iidm_viewer.app_state.network_loader.pick_default_vl",
            return_value=None,
        ):
            state.install_network(MagicMock())
        assert LINES_ALL not in shared
        # LF_GEN is per-variant: a fresh network resets to the InitialState slot.
        assert shared.get(LF_GEN) == {"InitialState": 0}


# ---------------------------------------------------------------------------
# Lifecycle integration
# ---------------------------------------------------------------------------


def test_install_network_round_trips_network_through_session_state():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        state = AppState()
        net = MagicMock()
        with patch(
            "iidm_viewer.app_state.network_loader.pick_default_vl",
            return_value="VL_DEFAULT",
        ):
            state.install_network(net)
        # The legacy ``st.session_state.get("network")`` and the new
        # ``state.network`` property return the same instance.
        assert shared["network"] is net
        assert state.network is net
        assert state.selected_vl == "VL_DEFAULT"
        assert shared["selected_vl"] == "VL_DEFAULT"


def test_run_loadflow_writes_report_to_session_state():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        state = AppState()
        state._set("network", MagicMock())

        fake_result = LoadFlowResult([], '{"report": "ok"}')
        with patch(
            "iidm_viewer.state.run_ac", return_value=fake_result,
        ), patch(
            "iidm_viewer.app_state.script_recorder.record_run_loadflow",
        ):
            result = state.run_loadflow()

        assert result is fake_result
        assert state.last_report_json == '{"report": "ok"}'
        # The Streamlit AppState maps ``last_report_json`` to the legacy
        # ``_lf_report_json`` session-state key so the LF report dialog
        # and any other reader keeps finding it.
        assert shared["_lf_report_json"] == '{"report": "ok"}'
        # _lf_gen bumped via the Streamlit cache backend — per-variant dict.
        assert shared[LF_GEN] == {"InitialState": 1}


def test_run_ac_override_uses_module_local_binding():
    """``monkeypatch.setattr("iidm_viewer.state.run_ac", …)`` intercepts
    the LF call — proves the host-scoped patch target still works after
    the AppState refactor."""
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        state = AppState()
        state._set("network", MagicMock())

        called = {"yes": False}

        def fake_run_ac(net, g, p):
            called["yes"] = True
            return LoadFlowResult([], "{}")

        with patch("iidm_viewer.state.run_ac", fake_run_ac), \
             patch("iidm_viewer.app_state.script_recorder.record_run_loadflow"):
            state.run_loadflow()
        assert called["yes"] is True


# ---------------------------------------------------------------------------
# app_state() singleton
# ---------------------------------------------------------------------------


def test_app_state_singleton_returns_same_instance_within_session():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        first = app_state()
        second = app_state()
        assert first is second


def test_app_state_singleton_persists_change_log_across_calls():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        first = app_state()
        first.change_log.record("Lines", "L1", "p1", before=0.0, after=1.0)
        second = app_state()
        assert second is first
        assert len(second.change_log.entries()) == 1
