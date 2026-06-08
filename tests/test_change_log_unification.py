"""Tests for the Streamlit change-log unification (docs/host-sharing.md §2c).

Phase A introduced the dual-write of ``add_to_change_log`` and
``_add_to_removal_log`` into the shared
:class:`iidm_viewer.change_log.ChangeLog`.

Phase B switched the four Streamlit Data Explorer readers
(``_render_change_log``, ``_render_removal_log``,
``_render_all_change_logs``, ``_revert_all_changes``) onto the shared
log.

Phase C dropped the legacy per-method ``_change_log_{method_name}``
and per-component ``_removal_log_{component}`` session-state writes
entirely. The tests below assert the steady-state contract: writes
land *only* in the shared log; the legacy keys are not created.
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from iidm_viewer.state import add_to_change_log, app_state
from iidm_viewer.data_explorer import _add_to_removal_log


class _FakeSessionState(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


# ---------------------------------------------------------------------------
# add_to_change_log writes only the shared log
# ---------------------------------------------------------------------------


def test_add_to_change_log_writes_shared_log_only():
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

        entries = app_state().change_log.entries(component="Generators")

    # Shared log populated with the canonical entry shape.
    assert len(entries) == 1
    e = entries[0]
    assert e["component"] == "Generators"
    assert e["element_id"] == "G1"
    assert e["property"] == "target_p"
    assert e["before"] == 10.0
    assert e["after"] == 42.0
    # Phase C: the legacy per-method key is not created.
    assert "_change_log_get_generators" not in shared


def test_add_to_change_log_collapse_applies_to_shared_log():
    """Re-edit collapsing visible in the shared log."""
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

        # Set back to the original value — the entry should disappear.
        changes_v2 = pd.DataFrame(
            {"target_p": [10.0]}, index=pd.Index(["G1"], name="id"),
        )
        add_to_change_log("get_generators", changes_v2, original)

        assert app_state().change_log.entries(component="Generators") == []


def test_add_to_change_log_unknown_method_is_silent_no_op():
    """A method name not in ``LABEL_FOR_METHOD`` is silently ignored —
    Phase C dropped the legacy fallback that used to record under the
    unknown method's own session-state key."""
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

        assert app_state().change_log.entries() == []
    # No legacy key materialised either.
    assert "_change_log_not_a_real_method" not in shared


def test_add_to_change_log_for_switches_uses_switches_component():
    """``get_switches`` is editable but not in ``COMPONENT_TYPES`` — the
    ``LABEL_FOR_METHOD`` explicit entry routes it under ``Switches``."""
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

        entries = app_state().change_log.entries(component="Switches")
    assert len(entries) == 1
    assert entries[0]["element_id"] == "SW1"
    assert entries[0]["property"] == "open"


# ---------------------------------------------------------------------------
# _add_to_removal_log writes only the shared log
# ---------------------------------------------------------------------------


def test_add_to_removal_log_writes_shared_log_only():
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

        removals = app_state().change_log.removals(component="Generators")

    assert len(removals) == 2
    by_id = {r["element_id"]: r for r in removals}
    assert by_id["G1"]["snapshot"]["target_p"] == 42.0
    assert by_id["G2"]["snapshot"]["target_p"] == 10.0
    # Phase C: the legacy per-component key is not created.
    assert "_removal_log_Generators" not in shared


def test_add_to_removal_log_dedupes_in_shared_log():
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

        assert len(app_state().change_log.removals(component="Generators")) == 1


# ---------------------------------------------------------------------------
# install_network clears the shared log
# ---------------------------------------------------------------------------


def test_install_network_clears_shared_changelog():
    """The shared ChangeLog is cleared by the inherited
    ``install_network`` via ``self.change_log.clear()``."""
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
        assert len(state.change_log.entries()) == 1

        state.install_network(MagicMock())

    assert len(state.change_log.entries()) == 0
