"""Tests for the dual-write of the Streamlit change log into the shared
:class:`iidm_viewer.change_log.ChangeLog` instance.

Phase A of the change-log unification (docs/host-sharing.md §2c):
Streamlit's legacy per-method ``_change_log_{method_name}`` lists keep
the on-disk shape they always had, but :func:`state.add_to_change_log`
now also populates :attr:`app_state().change_log` so any cross-host
reader (the N-K variant comparison plus future Streamlit panels) sees
the same edits.
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import streamlit as st

from iidm_viewer.state import add_to_change_log, app_state
from iidm_viewer.data_explorer import _add_to_removal_log


class _FakeSessionState(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


# ---------------------------------------------------------------------------
# add_to_change_log dual-write
# ---------------------------------------------------------------------------


def test_add_to_change_log_writes_legacy_per_method_list():
    """Phase-A guarantee: the legacy list shape is unchanged."""
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        changes = pd.DataFrame(
            {"target_p": [42.0]}, index=pd.Index(["G1"], name="id"),
        )
        original = pd.DataFrame(
            {"target_p": [10.0]}, index=pd.Index(["G1"], name="id"),
        )
        add_to_change_log("get_generators", changes, original)
    assert shared["_change_log_get_generators"] == [
        {"element_id": "G1", "property": "target_p", "before": 10.0, "after": 42.0},
    ]


def test_add_to_change_log_also_populates_shared_changelog():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared

        changes = pd.DataFrame(
            {"target_p": [42.0]}, index=pd.Index(["G1"], name="id"),
        )
        original = pd.DataFrame(
            {"target_p": [10.0]}, index=pd.Index(["G1"], name="id"),
        )
        add_to_change_log("get_generators", changes, original)

        log = app_state().change_log
    entries = log.entries(component="Generators")
    assert len(entries) == 1
    e = entries[0]
    assert e["component"] == "Generators"
    assert e["element_id"] == "G1"
    assert e["property"] == "target_p"
    assert e["before"] == 10.0
    assert e["after"] == 42.0


def test_add_to_change_log_collapse_applies_to_shared_log_too():
    """Re-edit collapsing must be visible in the shared log."""
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared

        changes_v1 = pd.DataFrame(
            {"target_p": [42.0]}, index=pd.Index(["G1"], name="id"),
        )
        original = pd.DataFrame(
            {"target_p": [10.0]}, index=pd.Index(["G1"], name="id"),
        )
        add_to_change_log("get_generators", changes_v1, original)

        # Set back to the original value — the entry should disappear
        # from both stores.
        changes_v2 = pd.DataFrame(
            {"target_p": [10.0]}, index=pd.Index(["G1"], name="id"),
        )
        add_to_change_log("get_generators", changes_v2, original)

        log = app_state().change_log

    assert shared["_change_log_get_generators"] == []
    assert log.entries(component="Generators") == []


def test_add_to_change_log_unknown_method_skips_shared_log_only():
    """An unrecognised method name still writes the legacy list; the
    shared log is skipped instead of raising."""
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared

        changes = pd.DataFrame(
            {"foo": [1.0]}, index=pd.Index(["X1"], name="id"),
        )
        original = pd.DataFrame(
            {"foo": [0.0]}, index=pd.Index(["X1"], name="id"),
        )
        add_to_change_log("not_a_real_method", changes, original)

        log = app_state().change_log

    assert shared["_change_log_not_a_real_method"] == [
        {"element_id": "X1", "property": "foo", "before": 0.0, "after": 1.0},
    ]
    assert log.entries() == []


def test_add_to_change_log_for_switches_uses_switches_component():
    """``get_switches`` is editable but not in COMPONENT_TYPES — the
    LABEL_FOR_METHOD entry routes it under the ``Switches`` label."""
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st:
        state_st.session_state = shared
        caches_st.session_state = shared

        changes = pd.DataFrame(
            {"open": [True]}, index=pd.Index(["SW1"], name="id"),
        )
        original = pd.DataFrame(
            {"open": [False]}, index=pd.Index(["SW1"], name="id"),
        )
        add_to_change_log("get_switches", changes, original)

        log = app_state().change_log
    entries = log.entries(component="Switches")
    assert len(entries) == 1
    assert entries[0]["element_id"] == "SW1"
    assert entries[0]["property"] == "open"


# ---------------------------------------------------------------------------
# _add_to_removal_log dual-write
# ---------------------------------------------------------------------------


def test_add_to_removal_log_writes_legacy_per_component_list():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        de_st.session_state = shared

        snapshot = pd.DataFrame(
            {"target_p": [42.0, 10.0]},
            index=pd.Index(["G1", "G2"], name="id"),
        )
        _add_to_removal_log("Generators", ["G1", "G2"], snapshot)

    assert shared["_removal_log_Generators"] == [
        {"element_id": "G1", "snapshot": {"target_p": 42.0}},
        {"element_id": "G2", "snapshot": {"target_p": 10.0}},
    ]


def test_add_to_removal_log_also_populates_shared_changelog():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        de_st.session_state = shared

        snapshot = pd.DataFrame(
            {"target_p": [42.0, 10.0]},
            index=pd.Index(["G1", "G2"], name="id"),
        )
        _add_to_removal_log("Generators", ["G1", "G2"], snapshot)

        log = app_state().change_log

    removals = log.removals(component="Generators")
    assert len(removals) == 2
    ids = sorted(r["element_id"] for r in removals)
    assert ids == ["G1", "G2"]


def test_add_to_removal_log_dedupes_across_both_stores():
    shared = _FakeSessionState()
    with patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as caches_st, \
         patch("iidm_viewer.data_explorer.st") as de_st:
        state_st.session_state = shared
        caches_st.session_state = shared
        de_st.session_state = shared

        snapshot = pd.DataFrame(
            {"target_p": [42.0]}, index=pd.Index(["G1"], name="id"),
        )
        _add_to_removal_log("Generators", ["G1"], snapshot)
        # Second call with the same id must not double-record.
        _add_to_removal_log("Generators", ["G1"], snapshot)

        log = app_state().change_log

    assert len(shared["_removal_log_Generators"]) == 1
    assert len(log.removals(component="Generators")) == 1


# ---------------------------------------------------------------------------
# install_network clears both stores
# ---------------------------------------------------------------------------


def test_install_network_clears_shared_changelog():
    """The shared ChangeLog is cleared by the inherited
    ``install_network`` (via ``self.change_log.clear()``); the legacy
    per-method session keys are cleared by the Streamlit override."""
    from unittest.mock import MagicMock
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
        assert len(state.change_log.entries()) == 1
        assert "_change_log_get_generators" in shared

        state.install_network(MagicMock())

    assert len(state.change_log.entries()) == 0
    assert "_change_log_get_generators" not in shared
