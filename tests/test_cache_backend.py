"""Tests for the host-agnostic :mod:`iidm_viewer.cache_backend`.

These run without streamlit / PySide6 / NiceGUI — they use
:class:`~iidm_viewer.cache_backend.DictBackend` as the storage.
"""
from __future__ import annotations

import pytest

from iidm_viewer import cache_backend as cb
from iidm_viewer.cache_backend import (
    DictBackend,
    GEOGRAPHY_SLOTS,
    LF_GEN,
    LOAD_FLOW_SLOTS,
    NETWORK_REPLACE_SLOTS,
    TOPOLOGY_SLOTS,
)


def _seed_all_slots(backend: DictBackend) -> None:
    """Write a marker into every known slot so we can see which get popped."""
    for slot in TOPOLOGY_SLOTS + GEOGRAPHY_SLOTS + LOAD_FLOW_SLOTS + NETWORK_REPLACE_SLOTS:
        backend.set(slot, {"marker": slot})


# --- DictBackend ------------------------------------------------------------


def test_dict_backend_get_default():
    b = DictBackend()
    assert b.get("missing") is None
    assert b.get("missing", "default") == "default"


def test_dict_backend_set_then_get():
    b = DictBackend()
    b.set("k", 42)
    assert b.get("k") == 42


def test_dict_backend_setdefault_returns_existing():
    b = DictBackend()
    b.set("k", "first")
    assert b.setdefault("k", "second") == "first"
    assert b.get("k") == "first"


def test_dict_backend_setdefault_writes_default_when_missing():
    b = DictBackend()
    out = b.setdefault("k", {"a": 1})
    assert out == {"a": 1}
    assert b.get("k") == {"a": 1}


def test_dict_backend_pop_returns_value_and_removes():
    b = DictBackend()
    b.set("k", "v")
    assert b.pop("k") == "v"
    assert b.get("k") is None


def test_dict_backend_pop_default_when_missing():
    b = DictBackend()
    assert b.pop("missing", "default") == "default"


def test_dict_backend_keys_lists_set_keys():
    b = DictBackend()
    b.set("a", 1)
    b.set("b", 2)
    assert sorted(b.keys()) == ["a", "b"]


# --- Cache key helper -------------------------------------------------------


def test_cache_key_no_extras_is_net_gen_variant_triple():
    # variant_id defaults to "InitialState" so the tuple stays the same
    # for every pre-N-K caller without needing them to pass it explicitly.
    assert cb.cache_key(123, 4) == (123, 4, "InitialState")


def test_cache_key_with_extras_appends_them():
    assert cb.cache_key(123, 4, "get_lines") == (123, 4, "InitialState", "get_lines")
    assert cb.cache_key(123, 4, "x", "y") == (123, 4, "InitialState", "x", "y")


def test_cache_key_variant_id_changes_tuple():
    """Different ``variant_id`` values must produce distinct cache keys
    so the same (net, lf_gen) coordinates don't collide across variants."""
    assert (
        cb.cache_key(123, 4, variant_id="N-K")
        != cb.cache_key(123, 4, variant_id="InitialState")
    )


def test_lf_gen_keyed_per_variant():
    """Bumping one variant's counter must not disturb another's."""
    b = DictBackend()
    assert cb.lf_gen(b, "N-K") == 0
    cb.bump_lf_gen(b)  # default InitialState bump
    cb.bump_lf_gen(b, "N-K")
    cb.bump_lf_gen(b, "N-K")
    assert cb.lf_gen(b) == 1
    assert cb.lf_gen(b, "N-K") == 2


def test_lf_gen_migrates_legacy_int_storage():
    """A backend still carrying an int (pre-N-K shape) must still read
    correctly as the InitialState counter."""
    b = DictBackend()
    b.set(LF_GEN, 3)
    assert cb.lf_gen(b) == 3
    # Next bump rewrites it as a dict.
    cb.bump_lf_gen(b)
    assert isinstance(b.get(LF_GEN), dict)
    assert b.get(LF_GEN)["InitialState"] == 4


def test_invalidate_load_flow_only_bumps_requested_variant():
    b = DictBackend()
    cb.invalidate_load_flow(b)  # default InitialState
    cb.invalidate_load_flow(b, variant_id="N-K")
    assert cb.lf_gen(b) == 1
    assert cb.lf_gen(b, "N-K") == 1
    cb.invalidate_load_flow(b, variant_id="N-K")
    assert cb.lf_gen(b) == 1   # not bumped
    assert cb.lf_gen(b, "N-K") == 2


def test_invalidate_network_replace_pops_nk_slots():
    """Network replace must drop the N-K dock state alongside every
    DataFrame / map cache."""
    b = DictBackend()
    for slot in cb.NK_CACHE_KEYS:
        b.set(slot, {"placeholder": slot})
    cb.bump_lf_gen(b, "N-K")
    cb.invalidate_network_replace(b)
    for slot in cb.NK_CACHE_KEYS:
        assert b.get(slot) is None
    # LF counter is back to a clean {"InitialState": 0}.
    assert b.get(LF_GEN) == {"InitialState": 0}


# --- LF generation counter --------------------------------------------------


def test_lf_gen_defaults_to_zero():
    b = DictBackend()
    assert cb.lf_gen(b) == 0


def test_bump_lf_gen_increments_and_returns_new_value():
    b = DictBackend()
    assert cb.bump_lf_gen(b) == 1
    assert cb.bump_lf_gen(b) == 2
    # LF_GEN now stores a per-variant dict — default variant counter is 2.
    assert b.get(LF_GEN) == {"InitialState": 2}
    assert cb.lf_gen(b) == 2


def test_reset_lf_gen_zeroes_counter():
    b = DictBackend()
    cb.bump_lf_gen(b)
    cb.bump_lf_gen(b)
    cb.reset_lf_gen(b)
    assert cb.lf_gen(b) == 0


# --- Invalidation -----------------------------------------------------------


def test_invalidate_topology_pops_topology_slots_only():
    b = DictBackend()
    _seed_all_slots(b)
    cb.invalidate_topology(b)
    for slot in TOPOLOGY_SLOTS:
        assert b.get(slot) is None, f"{slot} should have been popped"
    for slot in GEOGRAPHY_SLOTS + LOAD_FLOW_SLOTS + NETWORK_REPLACE_SLOTS:
        assert b.get(slot) == {"marker": slot}


def test_invalidate_topology_with_geography_pops_geography_too():
    b = DictBackend()
    _seed_all_slots(b)
    cb.invalidate_topology(b, affects_geography=True)
    for slot in TOPOLOGY_SLOTS + GEOGRAPHY_SLOTS:
        assert b.get(slot) is None
    for slot in LOAD_FLOW_SLOTS + NETWORK_REPLACE_SLOTS:
        assert b.get(slot) == {"marker": slot}


def test_invalidate_topology_does_not_bump_lf_gen():
    b = DictBackend()
    cb.bump_lf_gen(b)
    cb.invalidate_topology(b)
    assert cb.lf_gen(b) == 1


def test_invalidate_load_flow_bumps_counter_and_pops_topology_and_lf_slots():
    b = DictBackend()
    _seed_all_slots(b)
    cb.invalidate_load_flow(b)
    assert cb.lf_gen(b) == 1
    for slot in TOPOLOGY_SLOTS + LOAD_FLOW_SLOTS:
        assert b.get(slot) is None, f"{slot} should have been popped"
    for slot in GEOGRAPHY_SLOTS + NETWORK_REPLACE_SLOTS:
        assert b.get(slot) == {"marker": slot}


def test_invalidate_load_flow_does_not_touch_geography():
    b = DictBackend()
    _seed_all_slots(b)
    cb.invalidate_load_flow(b)
    for slot in GEOGRAPHY_SLOTS:
        assert b.get(slot) == {"marker": slot}


def test_invalidate_network_replace_pops_everything_and_resets_counter():
    b = DictBackend()
    _seed_all_slots(b)
    cb.bump_lf_gen(b)
    cb.bump_lf_gen(b)

    cb.invalidate_network_replace(b)

    for slot in TOPOLOGY_SLOTS + GEOGRAPHY_SLOTS + LOAD_FLOW_SLOTS + NETWORK_REPLACE_SLOTS:
        assert b.get(slot) is None, f"{slot} should have been popped"
    assert cb.lf_gen(b) == 0


def test_invalidate_does_not_remove_unrelated_keys():
    b = DictBackend()
    b.set("user_data", {"keep": "me"})
    cb.invalidate_network_replace(b)
    assert b.get("user_data") == {"keep": "me"}


# --- Slot grouping disjointness --------------------------------------------


@pytest.mark.parametrize(
    "a,b_",
    [
        (TOPOLOGY_SLOTS, GEOGRAPHY_SLOTS),
        (TOPOLOGY_SLOTS, NETWORK_REPLACE_SLOTS),
        (GEOGRAPHY_SLOTS, LOAD_FLOW_SLOTS),
        (GEOGRAPHY_SLOTS, NETWORK_REPLACE_SLOTS),
        (LOAD_FLOW_SLOTS, NETWORK_REPLACE_SLOTS),
    ],
)
def test_slot_groups_are_disjoint(a, b_):
    """No slot should appear in two groupings — otherwise invalidation rules
    become ambiguous (does it belong to topology, LF or replace?)."""
    overlap = set(a) & set(b_)
    assert overlap == set(), f"overlapping slots: {overlap}"
