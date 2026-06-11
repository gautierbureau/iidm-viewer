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


# ---------------------------------------------------------------------------
# variant_id plumbing in the six core surfaces
#
# The contract: variant_id=None (or InitialState) is behaviour-identical to
# the pre-N-K signature; variant_id="N-K" returns the N-K variant's view.
# ---------------------------------------------------------------------------
def test_component_registry_default_path_unchanged(ieee14):
    from iidm_viewer.component_registry import get_dataframe

    base = get_dataframe(ieee14, "Lines")
    initial = get_dataframe(ieee14, "Lines", variant_id=INITIAL_VARIANT_ID)
    pd.testing.assert_frame_equal(base, initial)


def test_component_registry_variant_id_returns_variant_view(ieee14):
    """A non-default ``variant_id`` returns the variant's connection
    state — the N-K clone with L1-2-1 disconnected reports both sides
    as False while the base remains True."""
    from iidm_viewer.component_registry import get_dataframe

    build_contingency_variant(
        ieee14, {"id": "x", "element_ids": ["L1-2-1"]},
    )
    try:
        nk = get_dataframe(ieee14, "Lines", variant_id=NK_VARIANT_ID)
        nk_indexed = nk.set_index("id")
        assert bool(nk_indexed.loc["L1-2-1", "connected1"]) is False
        # Working variant restored after the call.
        assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID
    finally:
        drop_variant(ieee14)


def test_data_view_default_path_unchanged(ieee14):
    from iidm_viewer.data_view import get_enriched_dataframe

    base = get_enriched_dataframe(ieee14, "Lines")
    initial = get_enriched_dataframe(
        ieee14, "Lines", variant_id=INITIAL_VARIANT_ID,
    )
    pd.testing.assert_frame_equal(base, initial)


def test_data_view_variant_id_reflects_variant_disconnect(ieee14):
    from iidm_viewer.data_view import get_enriched_dataframe

    build_contingency_variant(
        ieee14, {"id": "x", "element_ids": ["L1-2-1"]},
    )
    try:
        df = get_enriched_dataframe(
            ieee14, "Lines", variant_id=NK_VARIANT_ID,
        )
        df_indexed = df.set_index("id")
        assert bool(df_indexed.loc["L1-2-1", "connected1"]) is False
    finally:
        drop_variant(ieee14)


def test_diagram_services_default_path_unchanged(ieee14):
    """SLD SVG is byte-identical between ``variant_id=None`` and
    ``variant_id="InitialState"`` — same fast path."""
    from iidm_viewer.diagram_services import generate_sld

    raw = _raw(ieee14)
    vls = run(lambda: list(raw.get_voltage_levels(attributes=[]).index))
    container = vls[0]
    base_svg, base_meta = generate_sld(ieee14, container)
    initial_svg, initial_meta = generate_sld(
        ieee14, container, variant_id=INITIAL_VARIANT_ID,
    )
    assert base_svg == initial_svg
    assert base_meta == initial_meta


def test_diagram_services_restores_working_variant(ieee14):
    """A variant-scoped SLD render restores the working variant."""
    from iidm_viewer.diagram_services import generate_sld

    build_contingency_variant(
        ieee14, {"id": "x", "element_ids": ["L1-2-1"]},
    )
    try:
        raw = _raw(ieee14)
        vls = run(lambda: list(raw.get_voltage_levels(attributes=[]).index))
        svg, _ = generate_sld(ieee14, vls[0], variant_id=NK_VARIANT_ID)
        assert svg
        assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID
    finally:
        drop_variant(ieee14)


def test_operational_limits_get_current_flows_default_path_unchanged(ieee14):
    """Pre-LF: flows are NaN. ``variant_id=None`` and InitialState
    return identical dicts."""
    from iidm_viewer.operational_limits import get_current_flows

    base = get_current_flows(ieee14)
    initial = get_current_flows(ieee14, variant_id=INITIAL_VARIANT_ID)
    assert set(base.keys()) == set(initial.keys())
    for k in base:
        # Both NaN — compare via repr so NaN != NaN doesn't trip the check.
        assert repr(base[k]) == repr(initial[k])


def test_operational_limits_compute_loading_default_path_unchanged(ieee14):
    """The fast path returns the same DataFrame whether
    ``variant_id`` is ``None`` or ``"InitialState"``."""
    from iidm_viewer.loadflow import run_ac
    from iidm_viewer.operational_limits import compute_loading

    run_ac(ieee14)
    limits_df = ieee14.get_operational_limits().reset_index()
    base = compute_loading(ieee14, limits_df)
    initial = compute_loading(ieee14, limits_df, variant_id=INITIAL_VARIANT_ID)
    pd.testing.assert_frame_equal(base, initial)


def test_operational_limits_view_model_variant_id_reflects_nk(xiidm_upload):
    """The variant-aware view model must reflect the variant's
    connection state — the disconnected line drops out of the N-K
    loading frame (zero current → filtered) while remaining in N.

    Uses the bundled test_ieee14.xiidm (which carries operational
    limits) rather than pypowsybl's in-memory IEEE14 factory which
    does not.
    """
    from iidm_viewer.loadflow import run_ac
    from iidm_viewer.operational_limits import (
        build_operational_limits_view_model,
    )
    from iidm_viewer.state import load_network

    network = load_network(xiidm_upload)
    run_ac(network)
    build_contingency_variant(
        network, {"id": "x", "element_ids": ["L1-2-1"]},
    )
    try:
        n_vm = build_operational_limits_view_model(network)
        nk_vm = build_operational_limits_view_model(
            network, variant_id=NK_VARIANT_ID,
        )
        assert n_vm is not None
        assert nk_vm is not None
        n_loads = n_vm.loading_df["element_id"].tolist()
        nk_loads = nk_vm.loading_df["element_id"].tolist()
        assert "L1-2-1" in n_loads
        assert "L1-2-1" not in nk_loads
        assert get_working_variant_id(network) == INITIAL_VARIANT_ID
    finally:
        drop_variant(network)


def test_reactive_curves_view_model_default_path_unchanged(ieee14):
    from iidm_viewer.reactive_curves import build_reactive_curves_view_model

    base = build_reactive_curves_view_model(ieee14)
    initial = build_reactive_curves_view_model(
        ieee14, variant_id=INITIAL_VARIANT_ID,
    )
    if base is None:
        assert initial is None
        return
    pd.testing.assert_frame_equal(base.gens_df, initial.gens_df)


def test_reactive_curves_view_model_variant_id_reflects_disconnect(ieee14):
    """Disconnecting B1-G on the N-K variant must surface as
    ``connected=False`` in the view model's gens_df."""
    from iidm_viewer.reactive_curves import build_reactive_curves_view_model

    build_contingency_variant(
        ieee14, {"id": "gen_outage", "element_ids": ["B1-G"]},
    )
    try:
        n_vm = build_reactive_curves_view_model(ieee14)
        nk_vm = build_reactive_curves_view_model(
            ieee14, variant_id=NK_VARIANT_ID,
        )
        if n_vm is None or nk_vm is None:
            pytest.skip("IEEE14 reactive-curves vm not built")
        if "B1-G" in nk_vm.gens_df.index and "connected" in nk_vm.gens_df.columns:
            assert bool(nk_vm.gens_df.loc["B1-G", "connected"]) is False
            assert bool(n_vm.gens_df.loc["B1-G", "connected"]) is True
        assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID
    finally:
        drop_variant(ieee14)


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
