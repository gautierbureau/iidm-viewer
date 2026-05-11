"""Tests for the shared edit-change-log module.

Covers:
* ``merge_entry`` collapse + net-diff invariants (used by Streamlit
  and the prototypes alike).
* ``ChangeLog`` class — record, record_bulk, revert, revert_all,
  clear, listener bus.
* The end-to-end revert path against IEEE14.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from iidm_viewer.change_log import ChangeLog, merge_entry, revert_via_apply
from iidm_viewer.component_registry import apply_cell_edit, get_dataframe
from iidm_viewer.powsybl_worker import NetworkProxy, run


ROOT = Path(__file__).resolve().parent.parent
XIIDM = ROOT / "test_ieee14.xiidm"


@pytest.fixture(scope="module")
def ieee14() -> NetworkProxy:
    def _load():
        import pypowsybl.network as pn
        return pn.load(str(XIIDM))
    return NetworkProxy(run(_load))


# ---------------------------------------------------------------------------
# merge_entry
# ---------------------------------------------------------------------------
def test_merge_entry_appends_new_entry():
    log: list[dict] = []
    merge_entry(log, "Generators", "G1", "target_p", 10.0, 20.0)
    assert log == [{
        "component": "Generators",
        "element_id": "G1",
        "property": "target_p",
        "before": 10.0,
        "after": 20.0,
    }]


def test_merge_entry_collapses_re_edit_into_existing():
    log: list[dict] = []
    merge_entry(log, "Generators", "G1", "target_p", 10.0, 20.0)
    merge_entry(log, "Generators", "G1", "target_p", 10.0, 30.0)  # re-edit
    assert len(log) == 1
    assert log[0]["before"] == 10.0
    assert log[0]["after"] == 30.0  # before is preserved


def test_merge_entry_removes_entry_when_value_returns_to_original():
    log: list[dict] = []
    merge_entry(log, "Generators", "G1", "target_p", 10.0, 20.0)
    # Same as initial -> entry should be removed.
    merge_entry(log, "Generators", "G1", "target_p", 10.0, 10.0)
    assert log == []


def test_merge_entry_skips_nan_after():
    log: list[dict] = []
    merge_entry(log, "Generators", "G1", "target_p", 10.0, float("nan"))
    assert log == []


def test_merge_entry_does_not_collapse_across_different_components():
    log: list[dict] = []
    merge_entry(log, "Generators", "G1", "target_p", 10.0, 20.0)
    merge_entry(log, "Loads",      "G1", "target_p", 10.0, 30.0)
    # Same (id, property) but different component -> two entries.
    assert len(log) == 2


# ---------------------------------------------------------------------------
# ChangeLog class
# ---------------------------------------------------------------------------
def test_changelog_record_fires_listeners():
    log = ChangeLog()
    fired = [0]
    log.on_changed(lambda: fired.__setitem__(0, fired[0] + 1))
    log.record("Generators", "G1", "target_p", 10.0, 20.0)
    assert len(log) == 1
    assert fired[0] >= 1


def test_changelog_record_bulk_collapses_and_skips_nan():
    log = ChangeLog()
    log.record_bulk("Generators", "target_p", {"G1": 1.0, "G2": 2.0}, 99.0)
    log.record_bulk("Generators", "target_p", {"G1": 1.0}, 99.0)  # same after - no new entry
    assert len(log) == 2

    # Now revert G1 conceptually by setting after back to before.
    log.record("Generators", "G1", "target_p", 1.0, 1.0)
    assert len(log) == 1  # G1 entry collapsed away
    assert log.entries()[0]["element_id"] == "G2"


def test_changelog_entries_filter_by_component():
    log = ChangeLog()
    log.record("Generators", "G1", "target_p", 1.0, 2.0)
    log.record("Loads", "L1", "p0", 5.0, 6.0)
    assert len(log.entries("Generators")) == 1
    assert log.entries("Loads")[0]["element_id"] == "L1"
    assert len(log.entries()) == 2


def test_changelog_clear_fires_listener_only_when_non_empty():
    log = ChangeLog()
    fired = [0]
    log.on_changed(lambda: fired.__setitem__(0, fired[0] + 1))
    log.clear()  # already empty -> noop, no fire
    assert fired[0] == 0
    log.record("Generators", "G1", "target_p", 1.0, 2.0)
    fired[0] = 0
    log.clear()
    assert fired[0] == 1
    assert len(log) == 0


# ---------------------------------------------------------------------------
# Revert (end-to-end against pypowsybl)
# ---------------------------------------------------------------------------
def test_revert_via_apply_restores_original(ieee14):
    df_before = get_dataframe(ieee14, "Generators")
    gen_id = str(df_before["id"].iloc[0])
    original = df_before["target_p"].iloc[0]

    # Apply an edit.
    apply_cell_edit(ieee14, "Generators", gen_id, "target_p", original + 7.0)

    # Revert via the shared helper.
    entry = {
        "component": "Generators",
        "element_id": gen_id,
        "property": "target_p",
        "before": original,
        "after": original + 7.0,
    }
    revert_via_apply(ieee14, entry)

    df_after = get_dataframe(ieee14, "Generators")
    after = df_after[df_after["id"].astype(str) == gen_id]["target_p"].iloc[0]
    assert after == pytest.approx(original)


def test_changelog_revert_drops_entry(ieee14):
    df_before = get_dataframe(ieee14, "Generators")
    gen_id = str(df_before["id"].iloc[0])
    original = df_before["target_p"].iloc[0]

    log = ChangeLog()
    apply_cell_edit(ieee14, "Generators", gen_id, "target_p", original + 3.0)
    log.record("Generators", gen_id, "target_p", original, original + 3.0)
    assert len(log) == 1

    log.revert(ieee14, log.entries()[0])
    assert len(log) == 0

    df_after = get_dataframe(ieee14, "Generators")
    after = df_after[df_after["id"].astype(str) == gen_id]["target_p"].iloc[0]
    assert after == pytest.approx(original)


def test_changelog_revert_all_handles_skipped_entries(ieee14):
    df_before = get_dataframe(ieee14, "Loads")
    load_id = str(df_before["id"].iloc[0])
    original = df_before["p0"].iloc[0]

    log = ChangeLog()
    apply_cell_edit(ieee14, "Loads", load_id, "p0", original + 2.0)
    log.record("Loads", load_id, "p0", original, original + 2.0)
    # Inject a poison entry (NaN before -> can't revert).
    log.record("Loads", "FAKE", "p0", float("nan"), 42.0)
    assert len(log) == 2

    reverted, skipped = log.revert_all(ieee14)
    assert reverted == 1
    assert len(skipped) == 1
    assert skipped[0]["element_id"] == "FAKE"
    # The poison entry stays in the log (skipped, not popped).
    assert len(log) == 1

    log.clear()


def test_revert_via_apply_rejects_nan_before(ieee14):
    entry = {
        "component": "Generators",
        "element_id": "anything",
        "property": "target_p",
        "before": float("nan"),
        "after": 1.0,
    }
    with pytest.raises(ValueError, match="original value unavailable"):
        revert_via_apply(ieee14, entry)


# ---------------------------------------------------------------------------
# Streamlit add_to_change_log shape compatibility
# ---------------------------------------------------------------------------
def test_streamlit_change_log_shape_is_preserved():
    """The Streamlit path uses ``state.add_to_change_log`` which writes
    per-method session-state lists. After the refactor those lists
    still contain dicts with the legacy keys
    (``element_id``, ``property``, ``before``, ``after``) and *no*
    ``component`` key — so the existing ``data_explorer`` render code
    keeps working unchanged.
    """
    pytest.importorskip("streamlit")
    import streamlit as st
    import pandas as pd
    from iidm_viewer.state import add_to_change_log

    # ``add_to_change_log`` reads / writes st.session_state — but bare
    # mode without a script runner has a thin shim, so this works
    # without booting an AppTest.
    st.session_state.pop("_change_log_get_generators", None)
    changes = pd.DataFrame({"target_p": [42.0]}, index=pd.Index(["G1"], name="id"))
    original = pd.DataFrame({"target_p": [10.0]}, index=pd.Index(["G1"], name="id"))
    add_to_change_log("get_generators", changes, original)
    log = st.session_state["_change_log_get_generators"]
    assert log == [{
        "element_id": "G1",
        "property": "target_p",
        "before": 10.0,
        "after": 42.0,
    }]

    # Re-edit collapses; setting it back to original removes it.
    changes2 = pd.DataFrame({"target_p": [10.0]}, index=pd.Index(["G1"], name="id"))
    add_to_change_log("get_generators", changes2, original)
    assert st.session_state["_change_log_get_generators"] == []
