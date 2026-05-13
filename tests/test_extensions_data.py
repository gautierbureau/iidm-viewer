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
