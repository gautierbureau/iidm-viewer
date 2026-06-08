"""Phase B + Phase C of the change-log unification (docs/host-sharing.md §2c).

The four Streamlit Data Explorer change-log readers
(``_render_change_log``, ``_render_removal_log``,
``_render_all_change_logs``, ``_revert_all_changes``) consume the
shared :class:`iidm_viewer.change_log.ChangeLog` on
:func:`iidm_viewer.state.app_state` exclusively after Phase C — the
legacy ``_change_log_{method_name}`` / ``_removal_log_{component}``
session-state lists are no longer written, read, or mirrored on
revert.
"""
from __future__ import annotations

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

        _render_change_log(MagicMock(), "Generators", "get_generators")

        # Header columns + one data row.
        assert de_st.columns.call_count >= 2


def test_render_change_log_no_op_when_shared_log_empty():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        _setup_de_mock(de_st, shared)

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

        _render_all_change_logs(MagicMock())
        assert de_st.divider.call_count == 0


# ---------------------------------------------------------------------------
# _revert_all_changes reads from + mutates the shared log
# ---------------------------------------------------------------------------


def test_revert_all_changes_drops_entries_from_shared_log():
    shared = _FakeSessionState()
    network = MagicMock()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st, \
         patch("iidm_viewer.data_explorer.update_components") as upd, \
         patch("iidm_viewer.data_explorer.script_recorder"):
        state_st.session_state = shared
        caches_st.session_state = shared
        _setup_de_mock(de_st, shared)

        state = app_state()
        state.change_log.record("Generators", "G1", "target_p", 10.0, 42.0)
        state.change_log.record("Loads", "L1", "p0", 5.0, 7.0)

        _revert_all_changes(network)

        # All entries reverted (no NaN ``before`` values).
        assert state.change_log.entries() == []
        # update_components called once per component (Generators + Loads).
        assert upd.call_count == 2
        # Status message recorded for the rerun pass.
        assert "_revert_status_message" in shared


def test_revert_all_changes_keeps_unrevertable_in_shared_log():
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

        _revert_all_changes(network)

        # Shared log still holds the unrevertable entry.
        remaining = state.change_log.entries(component="Generators")
        assert len(remaining) == 1


# ---------------------------------------------------------------------------
# install_network clears the shared log
# ---------------------------------------------------------------------------


def test_install_network_clears_shared_log():
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

        state.install_network(MagicMock())

    assert len(state.change_log.entries()) == 0
