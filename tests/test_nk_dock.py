"""Tests for the Streamlit N-K dock state wrappers in
:mod:`iidm_viewer.state` and the view-mode helper in
:mod:`iidm_viewer.components`.

The widget-rendering pieces (`render_manual_contingency_picker_streamlit`,
the sidebar expander itself) are exercised by the AppTest smoke runs
in other test files; this file unit-tests the pure-state helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import pypowsybl.network as pn

from iidm_viewer import cache_backend
from iidm_viewer.powsybl_worker import NetworkProxy, run
from iidm_viewer.state import (
    build_nk_variant,
    clear_nk_variant,
    run_nk_loadflow,
)
from iidm_viewer.variants import (
    INITIAL_VARIANT_ID,
    NK_VARIANT_ID,
    get_working_variant_id,
    list_variants,
)


@pytest.fixture
def ieee14() -> NetworkProxy:
    return NetworkProxy(run(pn.create_ieee14))


# ---------------------------------------------------------------------------
# build_nk_variant
# ---------------------------------------------------------------------------
def test_build_nk_variant_creates_variant_and_sets_session_keys(ieee14):
    """A successful build must:

    * call :func:`variants.build_contingency_variant`,
    * stash the contingency + variant id in the dock's session keys,
    * leave the working variant restored to InitialState.
    """
    fake_session: dict = {}
    with patch("iidm_viewer.state.st") as state_st:
        state_st.session_state = fake_session
        build_nk_variant(
            ieee14, {"id": "x", "element_ids": ["L1-2-1"]},
        )
    try:
        assert NK_VARIANT_ID in list_variants(ieee14)
        assert get_working_variant_id(ieee14) == INITIAL_VARIANT_ID
        assert fake_session[cache_backend.NK_CONTINGENCY] == {
            "id": "x", "element_ids": ["L1-2-1"],
        }
        assert fake_session[cache_backend.NK_VARIANT_ID] == NK_VARIANT_ID
        assert fake_session[cache_backend.NK_LF_STATUS] == "NEVER"
        assert fake_session[cache_backend.NK_LF_REPORT_JSON] is None
    finally:
        # Tidy up so subsequent tests get a fresh variant manager.
        from iidm_viewer.variants import drop_variant
        drop_variant(ieee14)


def test_build_nk_variant_resets_nk_lf_counter_on_rebuild(ieee14):
    """Rebuilding the N-K variant after a previous LF must drop the
    stale N-K counter so the per-variant cache keys start clean."""
    from iidm_viewer.caches import backend as _backend
    from iidm_viewer.variants import drop_variant

    fake_session: dict = {}
    # Seed an old N-K counter that should be wiped by the rebuild.
    _backend.set(cache_backend.LF_GEN, {"InitialState": 0, NK_VARIANT_ID: 5})
    with patch("iidm_viewer.state.st") as state_st:
        state_st.session_state = fake_session
        try:
            build_nk_variant(
                ieee14, {"id": "x", "element_ids": ["L1-2-1"]},
            )
            gens = _backend.get(cache_backend.LF_GEN)
            assert NK_VARIANT_ID not in gens
            assert gens.get(INITIAL_VARIANT_ID, 0) == 0
        finally:
            drop_variant(ieee14)
            _backend.set(cache_backend.LF_GEN, {"InitialState": 0})


# ---------------------------------------------------------------------------
# run_nk_loadflow
# ---------------------------------------------------------------------------
def test_run_nk_loadflow_returns_none_without_variant():
    """When the N-K variant has not been built the LF wrapper must
    short-circuit (no worker call, no session-key update)."""
    network = MagicMock()
    fake_session: dict = {}
    with patch("iidm_viewer.state.st") as state_st:
        state_st.session_state = fake_session
        result = run_nk_loadflow(network)
    assert result is None
    assert cache_backend.NK_LF_STATUS not in fake_session


def test_run_nk_loadflow_updates_status_and_bumps_nk_counter(ieee14):
    """A successful N-K LF must surface its status into the dock keys
    and bump only the N-K variant's LF counter."""
    from iidm_viewer.caches import backend as _backend
    from iidm_viewer.variants import drop_variant

    fake_session: dict = {}
    with patch("iidm_viewer.state.st") as state_st:
        state_st.session_state = fake_session
        build_nk_variant(
            ieee14, {"id": "x", "element_ids": ["L1-2-1"]},
        )
        base_before = _backend.get(cache_backend.LF_GEN, {}).get(
            INITIAL_VARIANT_ID, 0,
        )
        try:
            with patch(
                "iidm_viewer.lf_parameters.get_lf_parameters",
                return_value=({}, {}),
            ):
                result = run_nk_loadflow(ieee14)
            assert result is not None
            assert fake_session[cache_backend.NK_LF_STATUS] == result.status
            assert fake_session[cache_backend.NK_LF_REPORT_JSON] == result.report_json
            # InitialState counter unchanged; N-K bumped.
            gens = _backend.get(cache_backend.LF_GEN, {}) or {}
            assert gens.get(INITIAL_VARIANT_ID, 0) == base_before
            assert gens.get(NK_VARIANT_ID, 0) >= 1
        finally:
            drop_variant(ieee14)
            _backend.set(cache_backend.LF_GEN, {"InitialState": 0})


# ---------------------------------------------------------------------------
# clear_nk_variant
# ---------------------------------------------------------------------------
def test_clear_nk_variant_drops_variant_and_keys(ieee14):
    from iidm_viewer.caches import backend as _backend
    from iidm_viewer.variants import drop_variant

    fake_session: dict = {}
    with patch("iidm_viewer.state.st") as state_st:
        state_st.session_state = fake_session
        build_nk_variant(
            ieee14, {"id": "x", "element_ids": ["L1-2-1"]},
        )
        _backend.set(
            cache_backend.LF_GEN,
            {"InitialState": 1, NK_VARIANT_ID: 2},
        )
        try:
            clear_nk_variant(ieee14)
            assert NK_VARIANT_ID not in list_variants(ieee14)
            for key in cache_backend.NK_CACHE_KEYS:
                assert key not in fake_session
            gens = _backend.get(cache_backend.LF_GEN, {}) or {}
            assert NK_VARIANT_ID not in gens
            assert gens.get(INITIAL_VARIANT_ID, 0) == 1
        finally:
            drop_variant(ieee14)
            _backend.set(cache_backend.LF_GEN, {"InitialState": 0})


def test_clear_nk_variant_is_idempotent():
    """No N-K state present → ``clear_nk_variant`` is a no-op."""
    network = MagicMock()
    fake_session: dict = {}
    with patch("iidm_viewer.state.st") as state_st:
        state_st.session_state = fake_session
        clear_nk_variant(network)
    # No exceptions, no spurious session keys.
    assert fake_session == {}


# ---------------------------------------------------------------------------
# components.render_view_mode_radio
# ---------------------------------------------------------------------------
def test_render_view_mode_radio_disabled_until_variant_exists():
    """The radio must be disabled (forced to ``"N"``) when the N-K
    variant has not been built — proves the only-discoverable-not-usable
    UX contract from the plan."""
    from streamlit.testing.v1 import AppTest

    app_text = (
        "import streamlit as st\n"
        "from iidm_viewer.components import render_view_mode_radio\n"
        "st.session_state.setdefault('mode_key', 'N')\n"
        "render_view_mode_radio('mode_key')\n"
    )
    at = AppTest.from_string(app_text)
    at.run(timeout=10)
    assert not at.exception
    # Active value forced to N when the dock has not built a variant.
    assert at.session_state["mode_key"] == "N"


# ---------------------------------------------------------------------------
# Per-tab N-K rollout — Reactive Curves
# ---------------------------------------------------------------------------
def test_rcc_tab_renders_view_mode_radio_n_only_when_no_variant(xiidm_upload):
    """With no N-K variant in session, the Reactive Curves tab's
    view-mode radio is disabled and the active mode stays ``"N"``."""
    from streamlit.testing.v1 import AppTest
    from iidm_viewer.state import load_network as _load

    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = _load(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.run(timeout=30)
    assert not at.exception
    # The radio default is "N" — _nk_variant_id is None so any stale
    # pick is forced back to "N" by render_view_mode_radio.
    try:
        active = at.session_state["_rcc_view_mode"]
    except KeyError:
        active = "N"
    assert active == "N"


def test_data_explorer_renders_side_by_side_when_variant_built(xiidm_upload):
    """Side-by-side mode for the Data Explorer must render both panes
    without raising. The N-K pane is read-only (no Apply/Remove buttons)."""
    from streamlit.testing.v1 import AppTest
    from iidm_viewer.state import load_network as _load

    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    network = _load(xiidm_upload)
    at.session_state["network"] = network
    at.session_state["_last_file"] = xiidm_upload.name
    fake_session = at.session_state
    with patch("iidm_viewer.state.st") as state_st:
        state_st.session_state = fake_session
        from iidm_viewer.state import build_nk_variant
        build_nk_variant(
            network, {"id": "x", "element_ids": ["L1-2-1"]},
        )
    at.session_state["_de_view_mode"] = "Side-by-side"
    try:
        at.run(timeout=60)
        assert not at.exception
        assert at.session_state["_de_view_mode"] == "Side-by-side"
    finally:
        from iidm_viewer.variants import drop_variant
        try:
            drop_variant(network)
        except Exception:
            pass


def test_oplim_tab_renders_side_by_side_when_variant_built(xiidm_upload):
    """Building the N-K variant must let the Operational Limits tab
    render side-by-side without raising — exercises the per-variant
    LOADING cache slot + the variant-aware get_current_flows /
    get_branch_losses paths."""
    from streamlit.testing.v1 import AppTest
    from iidm_viewer.state import load_network as _load

    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    network = _load(xiidm_upload)
    at.session_state["network"] = network
    at.session_state["_last_file"] = xiidm_upload.name
    fake_session = at.session_state
    with patch("iidm_viewer.state.st") as state_st:
        state_st.session_state = fake_session
        from iidm_viewer.state import build_nk_variant
        build_nk_variant(
            network, {"id": "x", "element_ids": ["L1-2-1"]},
        )
    at.session_state["_oplim_view_mode"] = "Side-by-side"
    try:
        at.run(timeout=60)
        assert not at.exception
        assert at.session_state["_oplim_view_mode"] == "Side-by-side"
    finally:
        from iidm_viewer.variants import drop_variant
        try:
            drop_variant(network)
        except Exception:
            pass


def test_rcc_tab_renders_side_by_side_when_variant_built(xiidm_upload):
    """Building the N-K variant must let the Reactive Curves tab render
    side-by-side without raising."""
    from streamlit.testing.v1 import AppTest
    from iidm_viewer.state import load_network as _load

    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    network = _load(xiidm_upload)
    at.session_state["network"] = network
    at.session_state["_last_file"] = xiidm_upload.name
    # Build the N-K variant directly via the state helper to bypass
    # the picker form (forms need a session-driven submit).
    fake_session = at.session_state
    with patch("iidm_viewer.state.st") as state_st:
        state_st.session_state = fake_session
        from iidm_viewer.state import build_nk_variant
        build_nk_variant(
            network, {"id": "x", "element_ids": ["L1-2-1"]},
        )
    at.session_state["_rcc_view_mode"] = "Side-by-side"
    try:
        at.run(timeout=60)
        assert not at.exception
        # Both panes share the same view-mode key.
        assert at.session_state["_rcc_view_mode"] == "Side-by-side"
    finally:
        from iidm_viewer.variants import drop_variant
        try:
            drop_variant(network)
        except Exception:
            pass


def test_render_view_mode_radio_enables_when_variant_set():
    """Once ``_nk_variant_id`` is set the radio enables N-K +
    Side-by-side; the active value can flip to either option."""
    from streamlit.testing.v1 import AppTest

    app_text = (
        "import streamlit as st\n"
        "from iidm_viewer.components import render_view_mode_radio\n"
        "render_view_mode_radio('mode_key')\n"
    )
    at = AppTest.from_string(app_text)
    at.session_state["_nk_variant_id"] = "N-K"
    at.session_state["mode_key"] = "N-K"
    at.run(timeout=10)
    assert not at.exception
    assert at.session_state["mode_key"] == "N-K"
