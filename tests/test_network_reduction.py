"""Tests for iidm_viewer.network_reduction."""
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from iidm_viewer.network_reduction import _clear_caches
from iidm_viewer.network_reduction_actions import list_voltage_level_ids
from iidm_viewer.powsybl_worker import NetworkProxy, run
from iidm_viewer.state import load_network

ROOT = Path(__file__).resolve().parent.parent
_XIIDM = str(ROOT / "test_ieee14.xiidm")


def _fresh_ieee14() -> NetworkProxy:
    """Load a fresh IEEE14 network directly on the worker thread."""
    def _load():
        import pypowsybl.network as pn
        return pn.load(_XIIDM)
    return NetworkProxy(run(_load))


# ─────────────────────────────────────────────────────────────────────────────
# list_voltage_level_ids
# ─────────────────────────────────────────────────────────────────────────────

def testlist_voltage_level_ids_count(xiidm_upload):
    net = load_network(xiidm_upload)
    ids = list_voltage_level_ids(net)
    assert len(ids) == 14
    assert all(isinstance(i, str) for i in ids)


def testlist_voltage_level_ids_node_breaker(node_breaker_network):
    ids = list_voltage_level_ids(node_breaker_network)
    assert len(ids) > 0
    assert all(isinstance(i, str) for i in ids)


# ─────────────────────────────────────────────────────────────────────────────
# _clear_caches
# ─────────────────────────────────────────────────────────────────────────────

def test_clear_caches_removes_cache_keys():
    import iidm_viewer.network_reduction as nr

    fake = {
        "_map_data_cache": {"x": 1},
        "_vl_lookup_cache": {"y": 2},
        "_export_bytes": b"data",
        "_export_fmt": "XIIDM",
        "selected_vl": "VL1",
        "network": "kept",
    }
    with patch("iidm_viewer.network_reduction.st") as mock_st, \
         patch("iidm_viewer.caches.st") as mock_caches_st:
        mock_st.session_state = fake
        mock_caches_st.session_state = fake
        nr._clear_caches()

    assert "_map_data_cache" not in fake
    assert "_vl_lookup_cache" not in fake
    assert "_export_bytes" not in fake
    assert "_export_fmt" not in fake
    assert fake["selected_vl"] is None
    assert fake["network"] == "kept"


def test_clear_caches_clears_shared_changelog_and_export_cache_keys():
    """After the change-log unification both component edits and
    extension edits (bucketed under ``"ext:<extension_name>"``) live
    in the shared :class:`ChangeLog` on ``app_state().change_log``.
    ``_clear_caches`` resets the shared log and pops the
    ``_export_cache_*`` Streamlit-only keys."""
    import iidm_viewer.network_reduction as nr
    from iidm_viewer.state import app_state

    fake = {
        "selected_vl": "VL2",
        "_export_cache_xiidm": b"stale",
    }
    with patch("iidm_viewer.network_reduction.st") as mock_st, \
         patch("iidm_viewer.state.st") as state_st, \
         patch("iidm_viewer.caches.st") as mock_caches_st:
        mock_st.session_state = fake
        state_st.session_state = fake
        mock_caches_st.session_state = fake

        # Seed both component and extension edits in the shared log.
        state = app_state()
        state.change_log.record("Generators", "G1", "target_p", 10.0, 42.0)
        state.change_log.record("ext:activePowerControl", "G2", "droop", 4.0, 6.0)
        state.change_log.record_removal("ext:entsoeCategory", ["S1"])
        assert len(state.change_log.entries()) == 2
        assert len(state.change_log.removals()) == 1

        nr._clear_caches()

        # Shared log cleared end-to-end (component + extension entries).
        assert state.change_log.entries() == []
        assert state.change_log.removals() == []

    # Streamlit export cache popped.
    assert "_export_cache_xiidm" not in fake


def test_clear_caches_tolerates_absent_keys():
    import iidm_viewer.network_reduction as nr

    fake = {"selected_vl": "VL5"}
    with patch("iidm_viewer.network_reduction.st") as mock_st, \
         patch("iidm_viewer.caches.st") as mock_caches_st:
        mock_st.session_state = fake
        mock_caches_st.session_state = fake
        nr._clear_caches()  # must not raise

    assert fake["selected_vl"] is None


# ─────────────────────────────────────────────────────────────────────────────
# reduce_by_voltage_range
# ─────────────────────────────────────────────────────────────────────────────

def test_reduce_by_voltage_range_shrinks_network():
    net = _fresh_ieee14()
    vl_df = net.get_voltage_levels()
    nom_vs = sorted(vl_df["nominal_v"].unique().tolist())
    if len(nom_vs) < 2:
        pytest.skip("IEEE14 fixture has only one nominal voltage; range test skipped")

    # Keep only VLs at the highest nominal voltage
    v_min = nom_vs[-1] - 0.5
    v_max = nom_vs[-1] + 0.5
    net.reduce_by_voltage_range(v_min=v_min, v_max=v_max)

    remaining = net.get_voltage_levels()
    assert len(remaining) < len(vl_df)
    assert (remaining["nominal_v"] >= v_min).all()
    assert (remaining["nominal_v"] <= v_max).all()


def test_reduce_by_voltage_range_node_breaker(node_breaker_network):
    vl_df = node_breaker_network.get_voltage_levels()
    nom_vs = sorted(vl_df["nominal_v"].unique().tolist())
    if len(nom_vs) < 2:
        pytest.skip("node_breaker fixture has only one nominal voltage")

    v_min = nom_vs[-1] - 0.5
    v_max = nom_vs[-1] + 0.5
    node_breaker_network.reduce_by_voltage_range(v_min=v_min, v_max=v_max)

    remaining = node_breaker_network.get_voltage_levels()
    assert len(remaining) < len(vl_df)


def test_reduce_by_voltage_range_with_boundary_lines():
    net = _fresh_ieee14()
    vl_df = net.get_voltage_levels()
    nom_vs = sorted(vl_df["nominal_v"].unique().tolist())
    if len(nom_vs) < 2:
        pytest.skip("IEEE14 fixture has only one nominal voltage")

    v_min = nom_vs[-1] - 0.5
    v_max = nom_vs[-1] + 0.5
    net.reduce_by_voltage_range(v_min=v_min, v_max=v_max, with_boundary_lines=True)
    # Just verify it didn't raise and left a non-empty network
    assert len(net.get_voltage_levels()) > 0


# ─────────────────────────────────────────────────────────────────────────────
# reduce_by_ids
# ─────────────────────────────────────────────────────────────────────────────

def test_reduce_by_ids_keeps_subset():
    net = _fresh_ieee14()
    all_ids = list_voltage_level_ids(net)
    keep = all_ids[:3]

    net.reduce_by_ids(ids=keep)

    remaining = list_voltage_level_ids(net)
    # pypowsybl keeps the requested VLs plus elements between them, so the
    # result is a superset of `keep` and at most as large as the original.
    assert set(keep).issubset(set(remaining))
    assert 0 < len(remaining) <= len(all_ids)


def test_reduce_by_ids_single_vl_reduces_count():
    net = _fresh_ieee14()
    original = len(net.get_voltage_levels())
    seed = list_voltage_level_ids(net)[0]

    net.reduce_by_ids(ids=[seed])

    assert len(net.get_voltage_levels()) < original


def test_reduce_by_ids_with_boundary_lines():
    net = _fresh_ieee14()
    seed = list_voltage_level_ids(net)[0]
    net.reduce_by_ids(ids=[seed], with_boundary_lines=True)
    assert len(net.get_voltage_levels()) > 0


# ─────────────────────────────────────────────────────────────────────────────
# reduce_by_ids_and_depths
# ─────────────────────────────────────────────────────────────────────────────

def test_reduce_by_ids_and_depths_reduces_count():
    net = _fresh_ieee14()
    original = len(net.get_voltage_levels())
    seed = list_voltage_level_ids(net)[0]

    net.reduce_by_ids_and_depths(vl_depths=[(seed, 0)])
    assert len(net.get_voltage_levels()) < original


def test_reduce_by_ids_and_depths_greater_depth_keeps_more():
    """depth=2 must retain at least as many VLs as depth=0."""
    net_shallow = _fresh_ieee14()
    net_deep = _fresh_ieee14()
    seed = list_voltage_level_ids(net_shallow)[0]

    net_shallow.reduce_by_ids_and_depths(vl_depths=[(seed, 0)])
    net_deep.reduce_by_ids_and_depths(vl_depths=[(seed, 2)])

    assert len(net_deep.get_voltage_levels()) >= len(net_shallow.get_voltage_levels())


def test_reduce_by_ids_and_depths_multiple_seeds():
    """Providing two seed VLs keeps at least as many elements as one seed."""
    net_one = _fresh_ieee14()
    net_two = _fresh_ieee14()
    ids = list_voltage_level_ids(net_one)

    net_one.reduce_by_ids_and_depths(vl_depths=[(ids[0], 1)])
    net_two.reduce_by_ids_and_depths(vl_depths=[(ids[0], 1), (ids[1], 1)])

    assert len(net_two.get_voltage_levels()) >= len(net_one.get_voltage_levels())


def test_reduce_by_ids_and_depths_with_boundary_lines():
    net = _fresh_ieee14()
    seed = list_voltage_level_ids(net)[0]
    net.reduce_by_ids_and_depths(vl_depths=[(seed, 1)], with_boundary_lines=True)
    assert len(net.get_voltage_levels()) > 0


# ─────────────────────────────────────────────────────────────────────────────
# App integration — sidebar button visibility
# ─────────────────────────────────────────────────────────────────────────────

def _prepare(xiidm_upload):
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = load_network(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.run(timeout=30)
    return at


def test_network_reduction_button_present_with_network(xiidm_upload):
    at = _prepare(xiidm_upload)
    assert not at.exception
    labels = [b.label for b in at.button]
    assert any("Network Reduction" in label for label in labels)


def test_network_reduction_button_absent_without_network():
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    assert not at.exception
    labels = [b.label for b in at.button]
    assert not any("Network Reduction" in label for label in labels)
