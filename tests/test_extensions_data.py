"""Tests for the framework-agnostic
:mod:`iidm_viewer.extensions_data` helpers.

Pure constants + filter helper + worker-routed pypowsybl wrappers,
exercised end-to-end against the IEEE14 demo.
"""
from __future__ import annotations

import pandas as pd
import pytest

from iidm_viewer.extensions_data import (
    EDITABLE_EXTENSIONS,
    READONLY_EXTENSIONS,
    ExtensionsExplorerViewModel,
    filter_by_id_substring,
    get_extension_df,
    get_extensions_information,
    list_extension_names,
    remove_extension,
    update_extension,
)
from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
def test_editable_extensions_registry_carries_known_ones():
    assert "activePowerControl" in EDITABLE_EXTENSIONS
    assert "voltageRegulation" in EDITABLE_EXTENSIONS
    # Each entry must list at least one column.
    for name, cols in EDITABLE_EXTENSIONS.items():
        assert cols, f"{name} has no editable columns"


def test_readonly_extensions_covers_geographical_positions():
    assert "substationPosition" in READONLY_EXTENSIONS
    assert "linePosition" in READONLY_EXTENSIONS


# ---------------------------------------------------------------------------
# Pure filter helper
# ---------------------------------------------------------------------------
def test_filter_by_id_substring_returns_input_when_empty_text():
    df = pd.DataFrame({"x": [1, 2]}, index=["AAA", "BBB"])
    assert filter_by_id_substring(df, "") is df


def test_filter_by_id_substring_matches_case_insensitive():
    df = pd.DataFrame({"x": [1, 2, 3]}, index=["GH1", "GH2", "LD1"])
    result = filter_by_id_substring(df, "gh")
    assert list(result.index) == ["GH1", "GH2"]


def test_filter_by_id_substring_handles_no_match():
    df = pd.DataFrame({"x": [1, 2]}, index=["AAA", "BBB"])
    result = filter_by_id_substring(df, "ZZZ")
    assert result.empty


def test_filter_by_id_substring_empty_df_passes_through():
    df = pd.DataFrame()
    assert filter_by_id_substring(df, "anything") is df


# ---------------------------------------------------------------------------
# Worker-routed pypowsybl probes
# ---------------------------------------------------------------------------
def test_list_extension_names_returns_sorted_list():
    names = list_extension_names()
    assert isinstance(names, list)
    assert names, "expected at least one extension name"
    assert names == sorted(names)
    # Known pypowsybl extension that should always be present.
    assert "activePowerControl" in names


def test_get_extensions_information_returns_a_dataframe():
    df = get_extensions_information()
    assert isinstance(df, pd.DataFrame)
    if not df.empty:
        # ``detail`` is the column the dialogs surface as a caption.
        assert "detail" in df.columns or df.index.name


def test_get_extension_df_on_ieee14_returns_dataframe():
    """``activePowerControl`` lives on generators; create the IEEE14
    fixture and confirm the helper returns a DataFrame (empty or
    populated, depending on the pypowsybl defaults)."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    df = get_extension_df(net, "activePowerControl")
    assert isinstance(df, pd.DataFrame)


def test_get_extension_df_returns_empty_for_unknown_extension():
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    df = get_extension_df(net, "GHOST_EXTENSION_XYZ")
    assert isinstance(df, pd.DataFrame)
    assert df.empty


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------
def test_update_extension_rejects_non_editable_name():
    """``substationPosition`` lives in :data:`READONLY_EXTENSIONS` and
    is absent from :data:`EDITABLE_EXTENSIONS`; the shared helper
    rejects it with a ``ValueError`` so callers can surface a clean
    message rather than ship a no-op payload to pypowsybl."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    df = pd.DataFrame({"x": [1.0]}, index=["GH1"])
    with pytest.raises(ValueError, match="not editable"):
        update_extension(net, "substationPosition", df)


def test_update_extension_no_op_on_empty_df():
    """``update_extension`` should silently early-return when
    ``changes_df`` is empty so the dialog can call it without a guard."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    # No exception, no pypowsybl call.
    update_extension(net, "activePowerControl", pd.DataFrame())


def test_remove_extension_no_op_on_empty_ids():
    """Same shape: empty list of ids should be a no-op."""
    import pypowsybl.network as pn
    net = NetworkProxy(run(pn.create_ieee14))
    remove_extension(net, "activePowerControl", [])


# ---------------------------------------------------------------------------
# ExtensionsExplorerViewModel
# ---------------------------------------------------------------------------
def _sample_df():
    return pd.DataFrame(
        {"droop": [4.0, 6.0], "participate": [True, False]},
        index=["GEN1", "GEN2"],
    )


def _info_df():
    return pd.DataFrame(
        {"detail": ["Active-power control description", "Voltage reg desc"]},
        index=["activePowerControl", "voltageRegulation"],
    )


def test_view_model_defaults_are_empty():
    vm = ExtensionsExplorerViewModel()
    assert vm.info_df.empty
    assert vm.current_extension == ""
    assert vm.current_df.empty
    assert vm.pending_edits == {}
    assert vm.pending_removals == set()
    assert vm.detail() == ""
    assert vm.is_readonly() is False
    assert vm.editable_cols() == []
    assert vm.has_edits() is False
    assert vm.has_removals() is False


def test_view_model_set_info_and_detail():
    vm = ExtensionsExplorerViewModel()
    vm.set_info(_info_df())
    vm.set_data("activePowerControl", _sample_df())
    assert vm.detail() == "Active-power control description"


def test_view_model_detail_unknown_extension_returns_empty():
    vm = ExtensionsExplorerViewModel()
    vm.set_info(_info_df())
    vm.set_data("activePowerControl", _sample_df())
    vm.current_extension = "notARealExtension"
    assert vm.detail() == ""


def test_view_model_set_data_preserves_pending():
    """Same-extension refreshes (filter change, post-apply re-render) must
    keep the in-progress edits + ticks intact."""
    vm = ExtensionsExplorerViewModel()
    vm.set_data("activePowerControl", _sample_df())
    vm.add_edit("GEN1", "droop", 7.0)
    vm.tick_remove("GEN2", True)
    vm.set_data("activePowerControl", _sample_df())
    assert vm.get_edit("GEN1", "droop") == 7.0
    assert vm.is_ticked("GEN2") is True


def test_view_model_reset_pending_drops_state():
    vm = ExtensionsExplorerViewModel()
    vm.set_data("activePowerControl", _sample_df())
    vm.add_edit("GEN1", "droop", 7.0)
    vm.tick_remove("GEN2", True)
    vm.reset_pending()
    assert vm.pending_edits == {}
    assert vm.pending_removals == set()


def test_view_model_clear_resets_everything():
    vm = ExtensionsExplorerViewModel()
    vm.set_info(_info_df())
    vm.set_data("activePowerControl", _sample_df())
    vm.add_edit("GEN1", "droop", 7.0)
    vm.tick_remove("GEN2", True)
    vm.clear()
    assert vm.info_df.empty
    assert vm.current_extension == ""
    assert vm.current_df.empty
    assert vm.pending_edits == {}
    assert vm.pending_removals == set()


def test_view_model_is_readonly_for_position_extensions():
    vm = ExtensionsExplorerViewModel()
    vm.set_data("substationPosition", pd.DataFrame())
    assert vm.is_readonly() is True


def test_view_model_editable_cols_intersects_with_df():
    vm = ExtensionsExplorerViewModel()
    vm.set_data("activePowerControl", _sample_df())
    # Sample df has droop + participate; the registry includes more but
    # we only return the intersection.
    assert set(vm.editable_cols()) == {"droop", "participate"}


def test_view_model_editable_cols_empty_for_readonly_extension():
    vm = ExtensionsExplorerViewModel()
    vm.set_data(
        "substationPosition",
        pd.DataFrame({"x": [0.0]}, index=["S1"]),
    )
    assert vm.editable_cols() == []


def test_view_model_editable_cols_explicit_df_arg():
    vm = ExtensionsExplorerViewModel()
    vm.current_extension = "activePowerControl"
    extra = pd.DataFrame({"droop": [1.0]}, index=["GEN1"])
    assert vm.editable_cols(extra) == ["droop"]


def test_view_model_filtered_view_id_substring():
    vm = ExtensionsExplorerViewModel()
    df = pd.DataFrame(
        {"droop": [1.0, 2.0, 3.0]},
        index=["GENA", "GENB", "OTHER"],
    )
    vm.set_data("activePowerControl", df)
    out = vm.filtered_view("gen")
    assert list(out.index) == ["GENA", "GENB"]
    # Empty text returns everything.
    assert list(vm.filtered_view("").index) == list(df.index)


def test_view_model_tick_remove_and_is_ticked():
    vm = ExtensionsExplorerViewModel()
    vm.tick_remove("GEN1", True)
    vm.tick_remove("GEN2", True)
    assert vm.is_ticked("GEN1") is True
    assert vm.is_ticked("GEN2") is True
    vm.tick_remove("GEN1", False)
    assert vm.is_ticked("GEN1") is False


def test_view_model_add_edit_and_get_edit():
    vm = ExtensionsExplorerViewModel()
    vm.add_edit("GEN1", "droop", 7.0)
    vm.add_edit("GEN1", "participate", True)
    assert vm.get_edit("GEN1", "droop") == 7.0
    assert vm.get_edit("GEN1", "participate") is True
    assert vm.get_edit("GEN2", "droop") is None


def test_view_model_edits_changes_df_shape():
    vm = ExtensionsExplorerViewModel()
    vm.add_edit("GEN1", "droop", 7.0)
    vm.add_edit("GEN2", "droop", 8.0)
    vm.add_edit("GEN2", "participate", False)
    df = vm.edits_changes_df()
    assert set(df.index) == {"GEN1", "GEN2"}
    assert "droop" in df.columns
    assert "participate" in df.columns
    assert df.at["GEN2", "droop"] == 8.0


def test_view_model_edits_changes_df_empty_when_no_edits():
    assert ExtensionsExplorerViewModel().edits_changes_df().empty


def test_view_model_removals_list_is_sorted():
    vm = ExtensionsExplorerViewModel()
    vm.tick_remove("GEN3", True)
    vm.tick_remove("GEN1", True)
    vm.tick_remove("GEN2", True)
    assert vm.removals_list() == ["GEN1", "GEN2", "GEN3"]


def test_view_model_clear_edits_drops_all_edits():
    vm = ExtensionsExplorerViewModel()
    vm.add_edit("GEN1", "droop", 7.0)
    vm.clear_edits()
    assert vm.pending_edits == {}


def test_view_model_drop_edits_for_specific_ids():
    vm = ExtensionsExplorerViewModel()
    vm.add_edit("GEN1", "droop", 7.0)
    vm.add_edit("GEN2", "droop", 8.0)
    vm.drop_edits_for(["GEN1"])
    assert "GEN1" not in vm.pending_edits
    assert "GEN2" in vm.pending_edits


def test_view_model_clear_removals_drops_all_ticks():
    vm = ExtensionsExplorerViewModel()
    vm.tick_remove("GEN1", True)
    vm.tick_remove("GEN2", True)
    vm.clear_removals()
    assert vm.pending_removals == set()


# ---------------------------------------------------------------------------
# Streamlit drift guard
# ---------------------------------------------------------------------------
def test_streamlit_state_re_exports_shared_extensions_constants():
    pytest.importorskip("streamlit")
    from iidm_viewer.extensions_data import (
        EDITABLE_EXTENSIONS as SHARED_EDIT,
        READONLY_EXTENSIONS as SHARED_RO,
    )
    from iidm_viewer.state import (
        EDITABLE_EXTENSIONS as ST_EDIT,
        READONLY_EXTENSIONS as ST_RO,
    )
    assert ST_EDIT is SHARED_EDIT
    assert ST_RO is SHARED_RO


def test_streamlit_extensions_explorer_uses_shared_module():
    pytest.importorskip("streamlit")
    import inspect
    from iidm_viewer import extensions_explorer

    src = inspect.getsource(extensions_explorer)
    # Should reference the shared module, not redefine the constants.
    assert "iidm_viewer.extensions_data" in src
    assert "frozenset({\"substationPosition\"" not in src
