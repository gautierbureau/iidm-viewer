"""Phase B of the change-log unification (docs/host-sharing.md §2c).

The four Streamlit Data Explorer change-log readers
(``_render_change_log``, ``_render_removal_log``,
``_render_all_change_logs``, ``_revert_all_changes``) now consume
the shared :class:`iidm_viewer.change_log.ChangeLog` on
:func:`iidm_viewer.state.app_state`. The legacy
``_change_log_{method_name}`` / ``_removal_log_{component}``
session-state lists keep being dual-written for any external reader
still pointed at them.

These tests assert the new contract: drop the shared log → the
readers see no entries, even if the legacy lists are still populated.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pandas as pd


def _ctx_mock() -> MagicMock:
    """MagicMock that doubles as a ``with col:`` context manager."""
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    return m


def _setup_de_mock(de_st: MagicMock, shared: dict) -> None:
    """Configure the patched ``data_explorer.st`` mock so render code
    that calls ``st.columns([…])`` and ``with col:`` works."""
    de_st.session_state = shared
    de_st.columns.side_effect = lambda spec: tuple(
        _ctx_mock()
        for _ in range(len(spec) if hasattr(spec, "__len__") else int(spec))
    )
    de_st.button.return_value = False

from iidm_viewer.change_log import ChangeLog
from iidm_viewer.data_explorer import (
    _render_all_change_logs,
    _render_change_log,
    _render_removal_log,
    _revert_all_changes,
)
from iidm_viewer.state import app_state


class _FakeSessionState(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


# ---------------------------------------------------------------------------
# ChangeLog.drop_entry
# ---------------------------------------------------------------------------


def test_drop_entry_removes_matching_entry_and_returns_true():
    log = ChangeLog()
    log.record("Generators", "G1", "target_p", 10.0, 42.0)
    entry = log.entries()[0]
    assert log.drop_entry(entry) is True
    assert log.entries() == []


def test_drop_entry_returns_false_when_missing():
    log = ChangeLog()
    log.record("Generators", "G1", "target_p", 10.0, 42.0)
    log.clear()
    # ``entry`` was returned by an earlier snapshot but is no longer in the log.
    stale = {
        "component": "Generators", "element_id": "G1",
        "property": "target_p", "before": 10.0, "after": 42.0,
    }
    assert log.drop_entry(stale) is False


def test_drop_entry_fires_change_listener():
    log = ChangeLog()
    log.record("Generators", "G1", "target_p", 10.0, 42.0)
    entry = log.entries()[0]

    fired: list = []
    log.on_changed(lambda: fired.append(1))
    log.drop_entry(entry)
    assert fired == [1]


# ---------------------------------------------------------------------------
# _render_change_log reads from the shared log
# ---------------------------------------------------------------------------


def test_render_change_log_reads_from_shared_changelog():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        _setup_de_mock(de_st, shared)

        state = app_state()
        state.change_log.record("Generators", "G1", "target_p", 10.0, 42.0)
        # Empty the legacy list deliberately — the renderer must still
        # surface the entry via the shared log.
        shared["_change_log_get_generators"] = []

        _render_change_log(MagicMock(), "Generators", "get_generators")

        # The header columns are emitted plus one data row.
        # 2 calls to st.columns: one header + one row with the data.
        assert de_st.columns.call_count >= 2


def test_render_change_log_no_op_when_shared_log_empty():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        _setup_de_mock(de_st, shared)

        # Legacy list populated but shared log empty — Phase B contract:
        # the shared log is the source of truth, so no rows are drawn.
        shared["_change_log_get_generators"] = [
            {"element_id": "G1", "property": "target_p", "before": 10.0, "after": 42.0},
        ]

        _render_change_log(MagicMock(), "Generators", "get_generators")
        assert de_st.columns.call_count == 0


# ---------------------------------------------------------------------------
# _render_removal_log reads from the shared log
# ---------------------------------------------------------------------------


def test_render_removal_log_reads_from_shared_changelog():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        _setup_de_mock(de_st, shared)

        state = app_state()
        state.change_log.record_removal("Generators", ["G1", "G2"])
        # Empty legacy list to prove the renderer reads the shared store.
        shared["_removal_log_Generators"] = []

        _render_removal_log("Generators")
        # One markdown header + two captions for the two removed ids.
        assert de_st.markdown.call_count == 1
        assert de_st.caption.call_count == 2


def test_render_removal_log_no_op_when_shared_log_empty():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        _setup_de_mock(de_st, shared)

        shared["_removal_log_Generators"] = [
            {"element_id": "G1", "snapshot": {}},
        ]
        _render_removal_log("Generators")
        assert de_st.markdown.call_count == 0


# ---------------------------------------------------------------------------
# _render_all_change_logs reads totals from the shared log
# ---------------------------------------------------------------------------


def test_render_all_change_logs_reads_totals_from_shared_log():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        _setup_de_mock(de_st, shared)

        state = app_state()
        state.change_log.record("Generators", "G1", "target_p", 10.0, 42.0)
        state.change_log.record("Loads", "L1", "p0", 5.0, 7.0)
        state.change_log.record_removal("Generators", ["G2"])

        # Empty every legacy key — proves the totals come from the shared log.
        for k in list(shared):
            if k.startswith("_change_log_") or k.startswith("_removal_log_"):
                shared[k] = []

        _render_all_change_logs(MagicMock())
        # Two edits + one removal = 3 entries in the heading.
        md_calls = [c.args[0] for c in de_st.markdown.call_args_list]
        assert any("(3)" in arg for arg in md_calls), md_calls


def test_render_all_change_logs_no_op_when_shared_log_empty():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        _setup_de_mock(de_st, shared)

        # Legacy list populated but shared log empty.
        shared["_change_log_get_generators"] = [
            {"element_id": "G1", "property": "target_p", "before": 10.0, "after": 42.0},
        ]
        _render_all_change_logs(MagicMock())
        assert de_st.divider.call_count == 0


# ---------------------------------------------------------------------------
# _revert_all_changes reads from + mutates the shared log
# ---------------------------------------------------------------------------


def test_revert_all_changes_reads_from_shared_log_and_drops_entries():
    shared = _FakeSessionState()
    network = MagicMock()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st, \
         patch("iidm_viewer.data_explorer.update_components") as upd, \
         patch("iidm_viewer.data_explorer.script_recorder") as rec:
        state_st.session_state = shared
        caches_st.session_state = shared
        _setup_de_mock(de_st, shared)

        state = app_state()
        state.change_log.record("Generators", "G1", "target_p", 10.0, 42.0)
        state.change_log.record("Loads", "L1", "p0", 5.0, 7.0)
        # The legacy lists are kept in sync by the Phase A dual-write
        # in production; here we mirror by hand so the test exercises
        # both stores ending up empty.
        shared["_change_log_get_generators"] = [
            {"element_id": "G1", "property": "target_p", "before": 10.0, "after": 42.0},
        ]
        shared["_change_log_get_loads"] = [
            {"element_id": "L1", "property": "p0", "before": 5.0, "after": 7.0},
        ]

        _revert_all_changes(network)

        # All shared-log entries reverted (no NaN ``before`` values).
        assert state.change_log.entries() == []
        # update_components called once per component (Generators + Loads).
        assert upd.call_count == 2
        # Status message recorded for the rerun pass.
        assert "_revert_status_message" in shared
        # Legacy lists also mirrored to empty.
        assert shared["_change_log_get_generators"] == []
        assert shared["_change_log_get_loads"] == []


def test_revert_all_changes_keeps_unrevertable_in_shared_log_and_legacy_list():
    shared = _FakeSessionState()
    network = MagicMock()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st, \
         patch("iidm_viewer.data_explorer.update_components"), \
         patch("iidm_viewer.data_explorer.script_recorder"):
        state_st.session_state = shared
        caches_st.session_state = shared
        _setup_de_mock(de_st, shared)

        state = app_state()
        # ``before`` is NaN — unrevertable.
        state.change_log.record("Generators", "G1", "target_p", float("nan"), 42.0)
        unrevertable_entry = {
            "element_id": "G1", "property": "target_p",
            "before": float("nan"), "after": 42.0,
        }
        shared["_change_log_get_generators"] = [unrevertable_entry]

        _revert_all_changes(network)

        # Shared log still holds the unrevertable entry.
        remaining = state.change_log.entries(component="Generators")
        assert len(remaining) == 1
        # Legacy list also kept the unrevertable entry.
        assert shared["_change_log_get_generators"] == [unrevertable_entry]


# ---------------------------------------------------------------------------
# install_network keeps both stores in sync
# ---------------------------------------------------------------------------


def test_install_network_clears_shared_log_and_legacy_lists():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch(
             "iidm_viewer.app_state.network_loader.pick_default_vl",
             return_value=None,
         ):
        state_st.session_state = shared
        caches_st.session_state = shared

        state = app_state()
        state.change_log.record("Generators", "G1", "target_p", 10.0, 42.0)
        shared["_change_log_get_generators"] = [
            {"element_id": "G1", "property": "target_p", "before": 10.0, "after": 42.0},
        ]

        state.install_network(MagicMock())

        assert state.change_log.entries() == []
        assert "_change_log_get_generators" not in shared
