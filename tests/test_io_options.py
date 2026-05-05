"""Tests for iidm_viewer.io_options and the extended load/export parameter support."""
import io
import zipfile

import pandas as pd
import pytest
import streamlit as st

from iidm_viewer.io_options import (
    ext_to_format,
    get_format_parameters,
    get_import_formats,
    render_parameters_form,
)
from iidm_viewer.state import export_network, load_network


# ---------------------------------------------------------------------------
# ext_to_format
# ---------------------------------------------------------------------------


def test_ext_to_format_xiidm():
    assert ext_to_format("xiidm") == "XIIDM"
    assert ext_to_format("iidm") == "XIIDM"
    assert ext_to_format("xml") == "XIIDM"


def test_ext_to_format_ucte():
    assert ext_to_format("uct") == "UCTE"
    assert ext_to_format("ucte") == "UCTE"


def test_ext_to_format_unknown_returns_none():
    assert ext_to_format("csv") is None
    assert ext_to_format("") is None


def test_ext_to_format_case_insensitive():
    assert ext_to_format("XIIDM") == "XIIDM"
    assert ext_to_format("UCT") == "UCTE"


# ---------------------------------------------------------------------------
# render_parameters_form — unit tests using Streamlit's AppTest harness
# ---------------------------------------------------------------------------


def _make_params_df(rows: list[dict]) -> pd.DataFrame:
    """Build a parameters DataFrame in the same shape pypowsybl returns."""
    df = pd.DataFrame(rows)
    if "name" in df.columns:
        df = df.set_index("name")
    return df


def test_render_parameters_form_empty_df_returns_empty_dict():
    """Empty DataFrame → no widgets, empty result."""
    at = __import__("streamlit.testing.v1", fromlist=["AppTest"]).AppTest
    # We just call it directly since it has no side effects on empty input.
    result = render_parameters_form(pd.DataFrame(), "pfx")
    assert result == {}


def test_render_parameters_form_none_returns_empty_dict():
    result = render_parameters_form(None, "pfx")
    assert result == {}


def test_render_parameters_form_only_returns_non_defaults():
    """Values equal to their pypowsybl default must NOT appear in the output dict."""
    df = _make_params_df([
        {"name": "skip_ext", "description": "Skip extensions", "type": "BOOLEAN",
         "default": "false"},
    ])
    # Widget is not interacted with → value equals default → should not be in result
    st.session_state.clear()
    result = render_parameters_form(df, "test_pfx")
    # "false" matches default "false" → omitted
    assert "skip_ext" not in result


# ---------------------------------------------------------------------------
# get_import_formats
# ---------------------------------------------------------------------------


def test_get_import_formats_returns_list_of_strings():
    st.session_state.clear()
    fmts = get_import_formats()
    assert isinstance(fmts, list)
    assert len(fmts) > 0
    assert all(isinstance(f, str) for f in fmts)


def test_get_import_formats_includes_xiidm():
    st.session_state.clear()
    fmts = get_import_formats()
    assert "XIIDM" in fmts


def test_get_import_formats_cached():
    st.session_state.clear()
    fmts1 = get_import_formats()
    fmts2 = get_import_formats()
    assert fmts1 is fmts2  # same object from session state


# ---------------------------------------------------------------------------
# get_format_parameters
# ---------------------------------------------------------------------------


def test_get_format_parameters_export_xiidm_returns_dataframe():
    st.session_state.clear()
    df = get_format_parameters("export", "XIIDM")
    assert isinstance(df, pd.DataFrame)


def test_get_format_parameters_import_xiidm_returns_dataframe():
    st.session_state.clear()
    df = get_format_parameters("import", "XIIDM")
    assert isinstance(df, pd.DataFrame)


def test_get_format_parameters_cached():
    st.session_state.clear()
    df1 = get_format_parameters("export", "XIIDM")
    df2 = get_format_parameters("export", "XIIDM")
    assert df1 is df2


def test_get_format_parameters_bad_format_returns_empty_df():
    st.session_state.clear()
    df = get_format_parameters("import", "NONEXISTENT_FORMAT_XYZ")
    assert isinstance(df, pd.DataFrame)
    assert df.empty


# ---------------------------------------------------------------------------
# load_network with parameters
# ---------------------------------------------------------------------------


def test_load_network_with_empty_parameters(xiidm_upload):
    """Passing an empty dict must not break loading."""
    net = load_network(xiidm_upload, parameters={})
    assert net is not None
    assert len(net.get_voltage_levels()) == 14


def test_load_network_stores_file_bytes(xiidm_upload):
    """Raw file bytes must be stored for the 'reload with options' feature."""
    load_network(xiidm_upload)
    assert "_last_file_bytes" in st.session_state
    assert st.session_state["_last_file_bytes"] == xiidm_upload.getvalue()


def test_load_network_with_none_parameters(xiidm_upload):
    net = load_network(xiidm_upload, parameters=None)
    assert len(net.get_voltage_levels()) == 14


# ---------------------------------------------------------------------------
# export_network with parameters
# ---------------------------------------------------------------------------


def test_export_network_with_empty_parameters(xiidm_upload):
    """Passing an empty dict must not break exporting."""
    net = load_network(xiidm_upload)
    data, ext = export_network(net, "XIIDM", parameters={})
    assert len(data) > 0
    assert ext in ("xiidm", "iidm", "xml", "zip")


def test_export_network_with_none_parameters(xiidm_upload):
    net = load_network(xiidm_upload)
    data, ext = export_network(net, "XIIDM", parameters=None)
    assert len(data) > 0


def test_export_network_default_and_explicit_produce_same_bytes(xiidm_upload):
    """Passing no parameters and an empty dict should yield identical output."""
    net = load_network(xiidm_upload)
    data_default, _ = export_network(net, "XIIDM")
    data_explicit, _ = export_network(net, "XIIDM", parameters={})
    assert data_default == data_explicit
