"""Tests for the framework-agnostic
:mod:`iidm_viewer.io_options_schema` helpers.

Pure helpers (``ext_to_format``, ``parse_possible_values``,
``csv_split``, ``coerce_param_value``, ``filter_changed_params``) +
worker-routed pypowsybl fetches (``get_import_formats``,
``get_import_post_processors``, ``get_format_parameters``).
"""
from __future__ import annotations

import pandas as pd
import pytest

from iidm_viewer.io_options_schema import (
    EXT_TO_FORMAT,
    FALLBACK_IMPORT_FORMATS,
    coerce_param_value,
    csv_split,
    ext_to_format,
    filter_changed_params,
    get_format_parameters,
    get_import_formats,
    get_import_post_processors,
    parse_possible_values,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_ext_to_format_maps_known_extensions():
    assert ext_to_format("xiidm") == "XIIDM"
    assert ext_to_format(".XML") == "XIIDM"
    assert ext_to_format("uct") == "UCTE"
    assert ext_to_format("mat") == "MATPOWER"


def test_ext_to_format_returns_none_for_unknown():
    assert ext_to_format("bogus") is None
    assert ext_to_format("") is None


def test_fallback_formats_include_common_ones():
    assert "XIIDM" in FALLBACK_IMPORT_FORMATS
    assert "UCTE" in FALLBACK_IMPORT_FORMATS


def test_ext_to_format_table_keys_are_lowercase():
    # The lookup downcases the input; the table must too.
    assert all(k == k.lower() for k in EXT_TO_FORMAT)


def test_parse_possible_values_handles_bracket_string():
    assert parse_possible_values("[A, B, C]") == ["A", "B", "C"]


def test_parse_possible_values_handles_iterable():
    assert parse_possible_values(["A", "B"]) == ["A", "B"]


def test_parse_possible_values_handles_plain_string_and_empty():
    assert parse_possible_values("SINGLE") == ["SINGLE"]
    assert parse_possible_values("") == []
    assert parse_possible_values(None) == []
    assert parse_possible_values("[]") == []


def test_csv_split_basic():
    assert csv_split("a,b, c , d") == ["a", "b", "c", "d"]
    assert csv_split("") == []
    assert csv_split(None) == []


def test_coerce_param_value_boolean():
    assert coerce_param_value("BOOLEAN", True) == "true"
    assert coerce_param_value("BOOLEAN", "TRUE") == "true"
    assert coerce_param_value("BOOLEAN", "no") == "false"


def test_coerce_param_value_integer_falls_back_to_default():
    assert coerce_param_value("INTEGER", "42") == "42"
    assert coerce_param_value("INTEGER", "x", default="7") == "7"


def test_coerce_param_value_double_falls_back_to_default():
    assert coerce_param_value("DOUBLE", "1.5") == "1.5"
    assert coerce_param_value("DOUBLE", "x", default="2.0") == "2.0"


def test_coerce_param_value_string_default():
    assert coerce_param_value("STRING", None) == ""
    assert coerce_param_value("STRING", 123) == "123"


# ---------------------------------------------------------------------------
# Filter helper
# ---------------------------------------------------------------------------
def _info_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "type": ["BOOLEAN", "DOUBLE", "STRING"],
            "default": ["true", "1.5", "X"],
            "description": ["", "", ""],
            "possible_values": ["", "", "[X, Y]"],
        },
        index=["alpha", "beta", "gamma"],
    )


def test_filter_changed_params_drops_defaults():
    out = filter_changed_params(
        {"alpha": "true", "beta": "1.5", "gamma": "Y"},
        _info_df(),
    )
    # alpha + beta match defaults — only gamma was changed.
    assert out == {"gamma": "Y"}


def test_filter_changed_params_skips_unknown_keys():
    out = filter_changed_params({"alpha": "true", "ghost": "x"}, _info_df())
    assert "ghost" not in out


def test_filter_changed_params_empty_df_returns_empty():
    assert filter_changed_params({"alpha": "true"}, pd.DataFrame()) == {}


# ---------------------------------------------------------------------------
# Worker-routed fetches
# ---------------------------------------------------------------------------
def test_get_import_formats_returns_non_empty_list():
    formats = get_import_formats()
    assert isinstance(formats, list)
    assert formats, "expected at least one import format"
    # XIIDM is always supported by pypowsybl.
    assert "XIIDM" in formats


def test_get_import_post_processors_returns_a_list():
    pp = get_import_post_processors()
    assert isinstance(pp, list)


def test_get_format_parameters_unknown_format_returns_empty_df():
    df = get_format_parameters("import", "GHOST_FORMAT_XYZ")
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_get_format_parameters_xiidm_returns_non_empty():
    df = get_format_parameters("import", "XIIDM")
    # XIIDM has at least a few configurable import parameters.
    assert isinstance(df, pd.DataFrame)
    assert not df.empty


# ---------------------------------------------------------------------------
# Streamlit drift guard
# ---------------------------------------------------------------------------
def test_streamlit_io_options_uses_shared_helpers():
    """The Streamlit ``io_options`` module must delegate every
    non-UI piece (constants, worker-routed fetches, possible-values
    parsing) to :mod:`iidm_viewer.io_options_schema`."""
    pytest.importorskip("streamlit")
    import inspect
    from iidm_viewer import io_options

    src = inspect.getsource(io_options)
    # No inline pypowsybl probes — must go through the shared module.
    assert "pn.get_import_formats" not in src
    assert "pn.get_import_post_processors" not in src
    assert "pn.get_import_parameters" not in src
    assert "pn.get_export_parameters" not in src
    # Re-exports from the shared module must be present.
    assert "io_options_schema" in src
