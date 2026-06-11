"""Tests for the host-agnostic :mod:`iidm_viewer.variants` primitives.

The contract these tests lock down:

* The working variant is always restored before any public function
  returns — even when the wrapped call raises.
* :func:`build_contingency_variant` produces a clone whose disconnect
  state differs from the base; the base variant is untouched.
* :func:`run_loadflow_on_variant` writes flows on the target variant
  only — the InitialState variant's ``p`` / ``q`` are not affected by
  an N-K load flow.
* :func:`drop_variant` is idempotent and tolerates the "is currently
  working" case.
"""
from __future__ import annotations

import pandas as pd
import pypowsybl.network as pn
import pytest

from iidm_viewer.powsybl_worker import NetworkProxy, run
from iidm_viewer.variants import (
    INITIAL_VARIANT_ID,
    NK_VARIANT_ID,
    build_contingency_variant,
    drop_variant,
    fetch_for_variant,
    get_working_variant_id,
    list_variants,
    run_loadflow_on_variant,
)


# ---------------------------------------------------------------------------
# Fixtures — built per-test so variants don't leak across tests.
# ---------------------------------------------------------------------------
@pytest.fixture
def ieee14() -> NetworkProxy:
    return NetworkProxy(run(pn.create_ieee14))


def _raw(network: NetworkProxy):
    return object.__getattribute__(network, "_obj")


# ---------------------------------------------------------------------------
# list_variants + get_working_variant_id
# ---------------------------------------------------------------------------
def test_list_variants_starts_with_initial_state_only(ieee14):
    assert list_variants(ieee14) == [INITIAL_VARIANT_ID]


def test_get_working_variant_id_is_initial_state(ieee14):
    assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID


# ---------------------------------------------------------------------------
# fetch_for_variant
# ---------------------------------------------------------------------------
def test_fetch_for_variant_fast_path_none(ieee14):
    df = fetch_for_variant(ieee14, "get_lines", None, attributes=[])
    assert len(df.index) > 0


def test_fetch_for_variant_fast_path_initial_state(ieee14):
    df = fetch_for_variant(ieee14, "get_lines", INITIAL_VARIANT_ID, attributes=[])
    assert len(df.index) > 0


def test_fetch_for_variant_restores_working_variant(ieee14):
    """After a fetch against a non-InitialState variant the working
    variant must read back as InitialState — between worker calls the
    invariant is always 'InitialState is the current variant'."""
    raw = _raw(ieee14)
    run(lambda: raw.clone_variant(INITIAL_VARIANT_ID, "PEEK"))
    try:
        df = fetch_for_variant(ieee14, "get_lines", "PEEK", attributes=[])
        assert len(df.index) > 0
        assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID
    finally:
        drop_variant(ieee14, "PEEK")


def test_fetch_for_variant_restores_working_variant_on_error(ieee14):
    """Even when the wrapped pypowsybl call raises the working variant
    must be restored — otherwise a single failed fetch can leave every
    subsequent reader on the wrong variant."""
    raw = _raw(ieee14)
    run(lambda: raw.clone_variant(INITIAL_VARIANT_ID, "PEEK"))
    try:
        with pytest.raises(Exception):
            fetch_for_variant(ieee14, "this_method_does_not_exist", "PEEK")
        assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID
    finally:
        drop_variant(ieee14, "PEEK")


# ---------------------------------------------------------------------------
# build_contingency_variant
# ---------------------------------------------------------------------------
def test_build_contingency_variant_creates_variant(ieee14):
    build_contingency_variant(
        ieee14, {"id": "single_line", "element_ids": ["L1-2-1"]},
    )
    try:
        assert NK_VARIANT_ID in list_variants(ieee14)
        # Working variant is back to InitialState — atomic round-trip.
        assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID
    finally:
        drop_variant(ieee14)


def test_build_contingency_variant_disconnects_on_clone_only(ieee14):
    """The base variant must remain connected — only the N-K clone
    sees the disconnection."""
    build_contingency_variant(
        ieee14, {"id": "single_line", "element_ids": ["L1-2-1"]},
    )
    try:
        # Base (InitialState) — line should still report connected on both sides.
        base_df = fetch_for_variant(
            ieee14, "get_lines", INITIAL_VARIANT_ID,
            attributes=["connected1", "connected2"],
        )
        assert bool(base_df.loc["L1-2-1", "connected1"]) is True
        assert bool(base_df.loc["L1-2-1", "connected2"]) is True

        # N-K — both sides disconnected.
        nk_df = fetch_for_variant(
            ieee14, "get_lines", NK_VARIANT_ID,
            attributes=["connected1", "connected2"],
        )
        assert bool(nk_df.loc["L1-2-1", "connected1"]) is False
        assert bool(nk_df.loc["L1-2-1", "connected2"]) is False
    finally:
        drop_variant(ieee14)


def test_build_contingency_variant_replaces_existing(ieee14):
    """A pre-existing N-K variant must be replaced cleanly — no stale
    disconnect state from the previous contingency leaking in."""
    build_contingency_variant(
        ieee14, {"id": "first", "element_ids": ["L1-2-1"]},
    )
    try:
        build_contingency_variant(
            ieee14, {"id": "second", "element_ids": ["L2-3-1"]},
        )
        # New N-K disconnects L2-3-1, NOT L1-2-1.
        nk_df = fetch_for_variant(
            ieee14, "get_lines", NK_VARIANT_ID,
            attributes=["connected1", "connected2"],
        )
        assert bool(nk_df.loc["L1-2-1", "connected1"]) is True
        assert bool(nk_df.loc["L2-3-1", "connected1"]) is False
    finally:
        drop_variant(ieee14)


def test_build_contingency_variant_supports_generator(ieee14):
    build_contingency_variant(
        ieee14, {"id": "gen_outage", "element_ids": ["B1-G"]},
    )
    try:
        nk_df = fetch_for_variant(
            ieee14, "get_generators", NK_VARIANT_ID,
            attributes=["connected"],
        )
        assert bool(nk_df.loc["B1-G", "connected"]) is False
        # Base intact.
        base_df = fetch_for_variant(
            ieee14, "get_generators", INITIAL_VARIANT_ID,
            attributes=["connected"],
        )
        assert bool(base_df.loc["B1-G", "connected"]) is True
    finally:
        drop_variant(ieee14)


def test_build_contingency_variant_supports_mixed_types(ieee14):
    """A single contingency mixing lines + generators is split by type
    inside the builder and applied through the matching ``update_*`` call."""
    build_contingency_variant(
        ieee14,
        {"id": "mixed", "element_ids": ["L1-2-1", "B1-G"]},
    )
    try:
        lines = fetch_for_variant(
            ieee14, "get_lines", NK_VARIANT_ID, attributes=["connected1"],
        )
        gens = fetch_for_variant(
            ieee14, "get_generators", NK_VARIANT_ID, attributes=["connected"],
        )
        assert bool(lines.loc["L1-2-1", "connected1"]) is False
        assert bool(gens.loc["B1-G", "connected"]) is False
    finally:
        drop_variant(ieee14)


def test_build_contingency_variant_rejects_empty_ids(ieee14):
    with pytest.raises(ValueError, match="at least one element id"):
        build_contingency_variant(ieee14, {"id": "x", "element_ids": []})


def test_build_contingency_variant_rejects_unknown_id(ieee14):
    """An unknown element id must blow up cleanly — and the variant
    manager must not gain a stray N-K entry from the failed call."""
    with pytest.raises(ValueError, match="Unknown element ids"):
        build_contingency_variant(
            ieee14, {"id": "x", "element_ids": ["DOES_NOT_EXIST"]},
        )
    assert NK_VARIANT_ID not in list_variants(ieee14)
    assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID


# ---------------------------------------------------------------------------
# run_loadflow_on_variant
# ---------------------------------------------------------------------------
def test_run_loadflow_on_variant_only_affects_target(ieee14):
    """A load flow on the N-K variant must not change the N variant's
    terminal ``p``/``q`` — even after the LF leaves the variants in
    different physical states."""
    # Seed the base variant with a known LF result.
    from iidm_viewer.loadflow import run_ac
    run_ac(ieee14)
    base_p_before = fetch_for_variant(
        ieee14, "get_lines", INITIAL_VARIANT_ID, attributes=["p1"],
    )["p1"].copy()

    # Build N-K with a single-line outage and run the N-K LF.
    build_contingency_variant(
        ieee14, {"id": "single_line", "element_ids": ["L1-2-1"]},
    )
    try:
        result = run_loadflow_on_variant(ieee14, NK_VARIANT_ID)
        assert result.converged is True
        # Working variant restored.
        assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID

        # Base variant's p1 unchanged.
        base_p_after = fetch_for_variant(
            ieee14, "get_lines", INITIAL_VARIANT_ID, attributes=["p1"],
        )["p1"]
        pd.testing.assert_series_equal(
            base_p_before, base_p_after, check_names=False,
        )

        # N-K variant's p1 differs — at least one line shifts.
        nk_p = fetch_for_variant(
            ieee14, "get_lines", NK_VARIANT_ID, attributes=["p1"],
        )["p1"]
        assert not nk_p.equals(base_p_after)
    finally:
        drop_variant(ieee14)


# ---------------------------------------------------------------------------
# drop_variant
# ---------------------------------------------------------------------------
def test_drop_variant_clears_nk(ieee14):
    build_contingency_variant(
        ieee14, {"id": "x", "element_ids": ["L1-2-1"]},
    )
    assert NK_VARIANT_ID in list_variants(ieee14)
    drop_variant(ieee14)
    assert NK_VARIANT_ID not in list_variants(ieee14)


def test_drop_variant_is_idempotent(ieee14):
    """Calling :func:`drop_variant` when the variant doesn't exist
    must be a no-op so hosts can call it unconditionally on cleanup."""
    drop_variant(ieee14)  # variant absent
    drop_variant(ieee14)  # still absent
    assert list_variants(ieee14) == [INITIAL_VARIANT_ID]


def test_drop_variant_handles_currently_working_variant(ieee14):
    """If the working variant happens to be ``target_variant`` (e.g.
    a bug or a partial recovery) :func:`drop_variant` must restore
    InitialState first so pypowsybl doesn't refuse the removal."""
    raw = _raw(ieee14)
    run(lambda: (
        raw.clone_variant(INITIAL_VARIANT_ID, NK_VARIANT_ID),
        raw.set_working_variant(NK_VARIANT_ID),
    ))
    drop_variant(ieee14)
    assert NK_VARIANT_ID not in list_variants(ieee14)
    assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID
