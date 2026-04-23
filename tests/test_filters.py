"""Unit tests for whitelist filters and VL/substation join enrichment."""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from iidm_viewer.filters import FILTERS, build_vl_lookup, enrich_with_joins
from iidm_viewer.network_info import COMPONENT_TYPES
from iidm_viewer.state import load_network


# ---------- pure pandas tests (no Streamlit involved) ----------

@pytest.fixture
def vl_lookup():
    return pd.DataFrame(
        {
            "id": ["VL1", "VL2", "VL3"],
            "substation_id": ["S1", "S1", "S2"],
            "nominal_v": [400.0, 225.0, 90.0],
            "country": ["FR", "FR", "BE"],
        }
    )


def test_enrich_joins_nominal_v_onto_generator_like_df(vl_lookup):
    df = pd.DataFrame(
        {"voltage_level_id": ["VL1", "VL3"], "min_p": [0.0, 10.0]},
        index=pd.Index(["G1", "G2"], name="id"),
    )
    out = enrich_with_joins(df, vl_lookup)
    assert list(out.index) == ["G1", "G2"]
    assert out.loc["G1", "nominal_v"] == 400.0
    assert out.loc["G2", "country"] == "BE"


def test_enrich_joins_country_onto_voltage_level_df(vl_lookup):
    """Voltage Levels already carry nominal_v; we add country via substation."""
    df = pd.DataFrame(
        {"substation_id": ["S1", "S2"], "nominal_v": [400.0, 90.0]},
        index=pd.Index(["VL1", "VL3"], name="id"),
    )
    out = enrich_with_joins(df, vl_lookup)
    assert list(out["country"]) == ["FR", "BE"]
    assert list(out["nominal_v"]) == [400.0, 90.0]  # not overwritten


def test_enrich_joins_two_sided_branch_df(vl_lookup):
    df = pd.DataFrame(
        {"voltage_level1_id": ["VL1"], "voltage_level2_id": ["VL3"]},
        index=pd.Index(["L1"], name="id"),
    )
    out = enrich_with_joins(df, vl_lookup)
    assert out.loc["L1", "nominal_v1"] == 400.0
    assert out.loc["L1", "nominal_v2"] == 90.0
    assert out.loc["L1", "country1"] == "FR"
    assert out.loc["L1", "country2"] == "BE"


def test_enrich_noop_when_no_join_columns(vl_lookup):
    df = pd.DataFrame({"foo": [1, 2]}, index=pd.Index(["a", "b"], name="id"))
    out = enrich_with_joins(df, vl_lookup)
    assert list(out.columns) == ["foo"]


def test_filters_registry_keys_are_in_component_types():
    assert set(FILTERS).issubset(set(COMPONENT_TYPES))


# ---------- AppTest-driven filter behaviour ----------

def _prepare(xiidm_upload):
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = load_network(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.session_state["active_tab_sync"] = 4  # Data Explorer Components
    at.run(timeout=30)
    return at


def _select(at, component):
    at.selectbox(key="component_type_select").select(component).run(timeout=30)


def test_generators_nominal_v_slider_narrows_rows(xiidm_upload):
    """IEEE14 has 3 generators at 135 kV and 2 at <=20 kV. Restricting the
    slider to 120–135 kV must keep exactly the 3 high-voltage ones."""
    at = _prepare(xiidm_upload)
    _select(at, "Generators")

    at.slider(key="flt_get_generators_nominal_v").set_range(120.0, 135.0).run(timeout=30)
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("3 of 5 generators" in c for c in captions)


def test_constant_numeric_column_is_skipped(xiidm_upload):
    """All IEEE14 loads have load type UNDEFINED → the 'type' whitelist
    filter has only one option, effectively constant, so no slider is shown
    for p0 when all values happen to be distinct. Here we check that the
    'connected' boolean filter (all True) yields no slider since it's
    constant."""
    at = _prepare(xiidm_upload)
    _select(at, "Loads")
    slider_keys = {s.key for s in at.slider}
    # connected is boolean and all True → constant, should not produce a slider
    assert "flt_get_loads_connected" not in slider_keys


def test_varying_numeric_column_gets_slider(xiidm_upload):
    """target_p varies across IEEE14 generators → slider must exist."""
    at = _prepare(xiidm_upload)
    _select(at, "Generators")
    slider_keys = {s.key for s in at.slider}
    assert "flt_get_generators_target_p" in slider_keys


def test_lines_have_both_sided_nominal_v_sliders(xiidm_upload):
    """Lines carry voltage_level1_id/2_id so both join columns must surface."""
    at = _prepare(xiidm_upload)
    _select(at, "Lines")
    slider_keys = {s.key for s in at.slider}
    assert "flt_get_lines_nominal_v1" in slider_keys
    assert "flt_get_lines_nominal_v2" in slider_keys


def test_voltage_levels_nominal_v_filter_narrows(xiidm_upload):
    """Filtering Voltage Levels by nominal_v range."""
    at = _prepare(xiidm_upload)
    _select(at, "Voltage Levels")
    at.slider(key="flt_get_voltage_levels_nominal_v").set_range(100.0, 135.0).run(timeout=30)
    assert not at.exception
    # VL1-5 are 135 kV → 5 of 14.
    captions = [c.value for c in at.caption]
    assert any("5 of 14 voltage levels" in c for c in captions)


def test_id_filter_and_whitelist_filter_compose(xiidm_upload):
    """ID substring and whitelist filter must AND, not replace each other."""
    at = _prepare(xiidm_upload)
    _select(at, "Generators")
    # Keep only 135 kV generators (B1-G, B2-G, B3-G).
    at.slider(key="flt_get_generators_nominal_v").set_range(120.0, 135.0).run(timeout=30)
    # ID substring narrows further to B2-G.
    at.text_input(key="id_filter_get_generators").set_value("B2").run(timeout=30)
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("1 of 5 generators" in c for c in captions)


def test_all_filters_neutral_shows_full_count(xiidm_upload):
    """Rendering the filter widgets without touching them must not narrow."""
    at = _prepare(xiidm_upload)
    _select(at, "Generators")
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("5 generators" in c for c in captions)


# ---------- blank-network regression tests (float64 vs object dtype) ----------
# pypowsybl returns float64 index columns when a DataFrame is empty.
# Merging those against object-dtype columns from component DataFrames must
# not raise ValueError.  These tests guard against that regression.

def test_build_vl_lookup_blank_network_no_error(blank_network):
    """build_vl_lookup must not raise on a network with no substations or VLs."""
    lookup = build_vl_lookup(blank_network)
    assert lookup.empty or set(lookup.columns) >= {"id", "substation_id", "nominal_v"}


def test_enrich_joins_with_empty_vl_lookup_no_error():
    """enrich_with_joins must not raise when vl_lookup is an empty DataFrame
    (which has float64 dtype on ID columns, as returned by pypowsybl)."""
    empty_lookup = pd.DataFrame(
        {"id": pd.Series(dtype="float64"),
         "substation_id": pd.Series(dtype="float64"),
         "nominal_v": pd.Series(dtype="float64"),
         "country": pd.Series(dtype="object")}
    )
    df = pd.DataFrame(
        {"voltage_level_id": pd.Series(dtype="object")},
        index=pd.Index([], name="id"),
    )
    result = enrich_with_joins(df, empty_lookup)
    assert result.empty


def test_enrich_joins_sided_with_empty_vl_lookup_no_error():
    """Same regression for two-sided (branch) DataFrames."""
    empty_lookup = pd.DataFrame(
        {"id": pd.Series(dtype="float64"),
         "substation_id": pd.Series(dtype="float64"),
         "nominal_v": pd.Series(dtype="float64"),
         "country": pd.Series(dtype="object")}
    )
    df = pd.DataFrame(
        {"voltage_level1_id": pd.Series(dtype="object"),
         "voltage_level2_id": pd.Series(dtype="object")},
        index=pd.Index([], name="id"),
    )
    result = enrich_with_joins(df, empty_lookup)
    assert result.empty


def test_build_vl_lookup_and_enrich_join_blank_network_no_error(blank_network):
    """Full round-trip: build lookup from blank network, then enrich an empty
    component DataFrame — must not raise ValueError."""
    lookup = build_vl_lookup(blank_network)
    df = pd.DataFrame(
        {"voltage_level_id": pd.Series(dtype="object")},
        index=pd.Index([], name="id"),
    )
    result = enrich_with_joins(df, lookup)
    assert result.empty
