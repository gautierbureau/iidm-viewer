"""Tests for the framework-agnostic
:mod:`iidm_viewer.lf_parameters_schema` helpers.

These cover the four pure pieces of logic shared by the Streamlit,
PySide6 and NiceGUI dialogs: generic / provider type coercion,
option-string parsing, category grouping, and the "changed vs
default" filter.
"""
from __future__ import annotations

import pandas as pd
import pytest

from iidm_viewer.lf_parameters_schema import (
    coerce_generic_value,
    coerce_provider_value,
    filter_changed_generic_params,
    filter_changed_provider_params,
    group_provider_params_by_category,
    parse_provider_options,
)
from iidm_viewer.loadflow import GENERIC_PARAMETERS


# ---------------------------------------------------------------------------
# Generic
# ---------------------------------------------------------------------------
def _generic_def(name: str) -> tuple:
    for d in GENERIC_PARAMETERS:
        if d[0] == name:
            return d
    raise KeyError(name)


def test_coerce_generic_bool_handles_strings_and_booleans():
    p = _generic_def("distributed_slack")
    assert coerce_generic_value(p, "true") is True
    assert coerce_generic_value(p, "0") is False
    assert coerce_generic_value(p, True) is True
    assert coerce_generic_value(p, 0) is False


def test_coerce_generic_enum_validates_against_options_and_falls_back():
    p = _generic_def("voltage_init_mode")
    assert coerce_generic_value(p, "PREVIOUS_VALUES") == "PREVIOUS_VALUES"
    # Unknown values fall back to the default.
    assert coerce_generic_value(p, "BOGUS") == p[2]


def test_coerce_generic_float_handles_garbage():
    p = _generic_def("dc_power_factor")
    assert coerce_generic_value(p, "2.5") == 2.5
    assert coerce_generic_value(p, "not-a-number") == p[2]


def test_filter_changed_generic_keeps_only_overrides():
    """Defaults are dropped so the run_ac payload stays minimal."""
    # All-defaults → empty dict.
    defaults = {d[0]: d[2] for d in GENERIC_PARAMETERS}
    assert filter_changed_generic_params(defaults) == {}

    # Toggle one bool — only that one survives.
    flipped = dict(defaults)
    flipped["distributed_slack"] = not defaults["distributed_slack"]
    out = filter_changed_generic_params(flipped)
    assert set(out) == {"distributed_slack"}
    assert out["distributed_slack"] is not defaults["distributed_slack"]


# ---------------------------------------------------------------------------
# Provider — options parsing
# ---------------------------------------------------------------------------
def test_parse_provider_options_handles_bracket_string():
    assert parse_provider_options("[A, B, C]") == ["A", "B", "C"]


def test_parse_provider_options_handles_iterable():
    assert parse_provider_options(["A", "B"]) == ["A", "B"]


def test_parse_provider_options_handles_plain_string():
    assert parse_provider_options("SINGLE") == ["SINGLE"]


def test_parse_provider_options_empty_and_none():
    assert parse_provider_options(None) == []
    assert parse_provider_options("") == []
    assert parse_provider_options("[]") == []


# ---------------------------------------------------------------------------
# Provider — coercion
# ---------------------------------------------------------------------------
def test_coerce_provider_value_boolean():
    assert coerce_provider_value("BOOLEAN", "true") is True
    assert coerce_provider_value("BOOLEAN", "FALSE") is False
    assert coerce_provider_value("BOOLEAN", True) is True


def test_coerce_provider_value_integer_falls_back_to_default():
    assert coerce_provider_value("INTEGER", "42") == 42
    assert coerce_provider_value("INTEGER", "x", default="7") == 7


def test_coerce_provider_value_double_falls_back_to_default():
    assert coerce_provider_value("DOUBLE", "1.5") == 1.5
    assert coerce_provider_value("DOUBLE", "x", default="2.0") == 2.0


def test_coerce_provider_value_string_default():
    assert coerce_provider_value("STRING", None) == ""
    assert coerce_provider_value("STRING", 123) == "123"


# ---------------------------------------------------------------------------
# Provider — grouping + filtering
# ---------------------------------------------------------------------------
def _info_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "category_key": ["A", "B", "A"],
            "type": ["BOOLEAN", "DOUBLE", "STRING"],
            "default": ["true", "1.5", "X"],
            "description": ["", "", ""],
            "possible_values": ["", "", "[X, Y]"],
        },
        index=["alpha", "beta", "gamma"],
    )


def test_group_provider_params_by_category_returns_sorted_groups():
    groups = group_provider_params_by_category(_info_df())
    assert [g[0] for g in groups] == ["A", "B"]
    a_names = list(groups[0][1].index)
    assert set(a_names) == {"alpha", "gamma"}


def test_group_provider_params_empty_df_returns_empty_list():
    assert group_provider_params_by_category(pd.DataFrame()) == []


def test_filter_changed_provider_drops_defaults():
    out = filter_changed_provider_params(
        {"alpha": "true", "beta": "1.5", "gamma": "Y"},
        _info_df(),
    )
    # alpha / beta match defaults; only gamma was changed.
    assert out == {"gamma": "Y"}


def test_filter_changed_provider_is_case_insensitive():
    """pypowsybl stringifies values; we compare case-insensitively so
    ``"TRUE"`` vs ``"true"`` isn't flagged as changed."""
    out = filter_changed_provider_params(
        {"alpha": "TRUE"}, _info_df(),
    )
    assert out == {}


def test_filter_changed_provider_skips_unknown_keys():
    out = filter_changed_provider_params(
        {"alpha": "true", "ghost": "x"}, _info_df(),
    )
    assert "ghost" not in out
