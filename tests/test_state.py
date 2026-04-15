"""Tests for session state, dataframe helpers, and filter logic."""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from iidm_viewer.state import (
    filter_voltage_levels,
    get_voltage_levels_df,
    load_network,
)


def _vls_df(rows):
    df = pd.DataFrame(rows)
    df["display"] = df.apply(lambda r: r["name"] or r["id"], axis=1)
    return df


def test_filter_voltage_levels_returns_input_when_no_text():
    df = _vls_df([{"id": "VL1", "name": ""}, {"id": "VL2", "name": "Alpha"}])
    assert filter_voltage_levels(df, "").equals(df)
    assert filter_voltage_levels(df, None).equals(df)


def test_filter_voltage_levels_matches_substring():
    df = _vls_df([
        {"id": "VL1", "name": "Alpha"},
        {"id": "VL2", "name": "Beta"},
        {"id": "VL3", "name": "Alphabet"},
    ])
    out = filter_voltage_levels(df, "Alpha")
    assert set(out["id"]) == {"VL1", "VL3"}


def test_filter_voltage_levels_is_case_insensitive():
    df = _vls_df([
        {"id": "VL1", "name": "Alpha"},
        {"id": "VL2", "name": "Beta"},
    ])
    out = filter_voltage_levels(df, "alpha")
    assert list(out["id"]) == ["VL1"]


def test_filter_voltage_levels_uses_literal_not_regex():
    df = _vls_df([
        {"id": "VL1", "name": "A.B"},
        {"id": "VL2", "name": "AXB"},
    ])
    out = filter_voltage_levels(df, ".")
    assert list(out["id"]) == ["VL1"], "dot should match literally, not as regex"


def test_filter_voltage_levels_falls_back_to_id_when_name_blank():
    df = _vls_df([
        {"id": "VL_BLANK", "name": ""},
        {"id": "VL_OTHER", "name": "named"},
    ])
    out = filter_voltage_levels(df, "BLANK")
    assert list(out["id"]) == ["VL_BLANK"]


def test_get_voltage_levels_df_columns_and_sort(xiidm_upload):
    net = load_network(xiidm_upload)
    df = get_voltage_levels_df(net)
    assert {"id", "name", "substation_id", "nominal_v", "display"}.issubset(df.columns)
    # sorted ascending by display
    assert list(df["display"]) == sorted(df["display"])


def test_init_state_populates_defaults():
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    assert at.session_state["network"] is None
    assert at.session_state["selected_vl"] is None
    assert at.session_state["nad_depth"] == 1
    assert at.session_state["component_type"] == "Voltage Levels"


def test_init_state_does_not_overwrite_existing_keys():
    at = AppTest.from_file("iidm_viewer/app.py")
    at.session_state["nad_depth"] = 7
    at.run(timeout=30)
    assert at.session_state["nad_depth"] == 7


def test_load_network_returns_proxy(xiidm_upload):
    from iidm_viewer.powsybl_worker import NetworkProxy

    net = load_network(xiidm_upload)
    assert isinstance(net, NetworkProxy)


def test_get_network_returns_none_without_upload():
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    assert at.session_state["network"] is None


def test_filter_voltage_levels_no_matches_returns_empty():
    df = _vls_df([{"id": "VL1", "name": "Alpha"}])
    assert filter_voltage_levels(df, "ZZZ").empty


def test_get_voltage_levels_df_display_prefers_name_over_id(xiidm_upload):
    """If a VL has a non-empty name, display is the name; otherwise the id."""
    net = load_network(xiidm_upload)
    df = get_voltage_levels_df(net)
    for _, row in df.iterrows():
        expected = row["name"] if row["name"] else row["id"]
        assert row["display"] == expected
