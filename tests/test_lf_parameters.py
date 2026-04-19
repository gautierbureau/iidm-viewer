"""Tests for iidm_viewer.lf_parameters."""
import pandas as pd
from unittest.mock import patch, MagicMock

from iidm_viewer.lf_parameters import _GENERIC_PARAMS
from iidm_viewer.powsybl_worker import run


def test_generic_params_defined():
    assert len(_GENERIC_PARAMS) > 0


def test_generic_params_have_expected_fields():
    for param in _GENERIC_PARAMS:
        name, ptype, default, desc = param[0], param[1], param[2], param[3]
        assert isinstance(name, str)
        assert ptype in ("bool", "enum", "float")
        assert isinstance(desc, str)
        if ptype == "enum":
            assert len(param) == 5  # has options list


def test_provider_parameters_loadable():
    """get_provider_parameters() should return a non-empty DataFrame."""
    def _fetch():
        import pypowsybl.loadflow as lf
        return lf.get_provider_parameters()

    df = run(_fetch)
    assert not df.empty
    assert "type" in df.columns
    assert "default" in df.columns


def test_provider_params_enum_values_are_parseable():
    """STRING params with possible_values like '[VAL1, VAL2]' should be parseable."""
    def _fetch():
        import pypowsybl.loadflow as lf
        return lf.get_provider_parameters()

    df = run(_fetch)
    for name, row in df.iterrows():
        if row["type"] == "STRING":
            pv = row.get("possible_values", "")
            if isinstance(pv, str) and pv.startswith("[") and pv.endswith("]"):
                options = [v.strip() for v in pv[1:-1].split(",") if v.strip()]
                assert len(options) >= 2, f"{name} should have at least 2 options"


def test_dc_approximation_type_has_options():
    """dcApproximationType should be detected as an enum with options."""
    def _fetch():
        import pypowsybl.loadflow as lf
        return lf.get_provider_parameters()

    df = run(_fetch)
    assert "dcApproximationType" in df.index
    row = df.loc["dcApproximationType"]
    assert row["type"] == "STRING"
    pv = row["possible_values"]
    assert "IGNORE_R" in pv
    assert "IGNORE_G" in pv


# ---------------------------------------------------------------------------
# _get_provider_params_info — session-state caching
# ---------------------------------------------------------------------------


def _fake_params_df():
    return pd.DataFrame(
        {"type": ["BOOLEAN", "INTEGER"], "default": ["true", "10"],
         "description": ["a bool", "an int"], "category_key": ["cat", "cat"],
         "possible_values": [None, None]},
        index=["flagParam", "sizeParam"],
    )


def test_get_provider_params_info_fetches_when_cache_empty():
    """When _lf_provider_info is absent from session state, run() is called once."""
    from iidm_viewer.lf_parameters import _get_provider_params_info

    mock_df = _fake_params_df()
    with patch("iidm_viewer.lf_parameters.st") as mock_st:
        mock_st.session_state = {}
        with patch("iidm_viewer.lf_parameters.run", return_value=mock_df):
            result = _get_provider_params_info()

    assert result is mock_df
    assert mock_st.session_state["_lf_provider_info"]["df"] is mock_df


def test_get_provider_params_info_returns_cached_df():
    """When the cache is already populated, run() must NOT be called."""
    from iidm_viewer.lf_parameters import _get_provider_params_info

    cached_df = _fake_params_df()
    with patch("iidm_viewer.lf_parameters.st") as mock_st:
        mock_st.session_state = {"_lf_provider_info": {"df": cached_df}}
        with patch("iidm_viewer.lf_parameters.run") as mock_run:
            result = _get_provider_params_info()
            mock_run.assert_not_called()

    assert result is cached_df


def test_get_provider_params_info_populates_cache_for_subsequent_calls():
    """A second call inside the same session_state dict uses the cached value."""
    from iidm_viewer.lf_parameters import _get_provider_params_info

    mock_df = _fake_params_df()
    call_count = 0

    def _run(fn):
        nonlocal call_count
        call_count += 1
        return mock_df

    fake_state = {}
    with patch("iidm_viewer.lf_parameters.st") as mock_st:
        mock_st.session_state = fake_state
        with patch("iidm_viewer.lf_parameters.run", side_effect=_run):
            _get_provider_params_info()
            _get_provider_params_info()

    assert call_count == 1  # only fetched once


# ---------------------------------------------------------------------------
# get_lf_parameters — session-state read
# ---------------------------------------------------------------------------


def test_get_lf_parameters_returns_empty_dicts_when_nothing_stored():
    from iidm_viewer.lf_parameters import get_lf_parameters

    with patch("iidm_viewer.lf_parameters.st") as mock_st:
        mock_st.session_state = {}
        generic, provider = get_lf_parameters()

    assert generic == {}
    assert provider == {}


def test_get_lf_parameters_returns_stored_generic_params():
    from iidm_viewer.lf_parameters import get_lf_parameters

    stored = {"distributed_slack": False, "balance_type": "PROPORTIONAL_TO_LOAD"}
    with patch("iidm_viewer.lf_parameters.st") as mock_st:
        mock_st.session_state = {"_lf_generic_params": stored}
        generic, provider = get_lf_parameters()

    assert generic == stored
    assert provider == {}


def test_get_lf_parameters_returns_stored_provider_params():
    from iidm_viewer.lf_parameters import get_lf_parameters

    stored_provider = {"maxNewtonIterations": 15}
    with patch("iidm_viewer.lf_parameters.st") as mock_st:
        mock_st.session_state = {"_lf_provider_params": stored_provider}
        generic, provider = get_lf_parameters()

    assert generic == {}
    assert provider == stored_provider


def test_get_lf_parameters_returns_both_when_both_stored():
    from iidm_viewer.lf_parameters import get_lf_parameters

    g = {"distributed_slack": True}
    p = {"maxNewtonIterations": 5}
    with patch("iidm_viewer.lf_parameters.st") as mock_st:
        mock_st.session_state = {"_lf_generic_params": g, "_lf_provider_params": p}
        generic, provider = get_lf_parameters()

    assert generic == g
    assert provider == p


# ---------------------------------------------------------------------------
# _get_provider_params_info — exercises inner _fetch closure (lines 11-12)
# ---------------------------------------------------------------------------


def test_get_provider_params_info_real_fetch_exercises_closure():
    """Calling without patching run() executes the inner _fetch body (lines 11-12)."""
    from iidm_viewer.lf_parameters import _get_provider_params_info

    with patch("iidm_viewer.lf_parameters.st") as mock_st:
        mock_st.session_state = {}
        result = _get_provider_params_info()

    assert not result.empty
    assert "type" in result.columns


# ---------------------------------------------------------------------------
# _render_generic_tab (lines 61-82)
# ---------------------------------------------------------------------------


def test_render_generic_tab_returns_all_param_names():
    """Every param in _GENERIC_PARAMS appears as a key in the returned dict."""
    from iidm_viewer.lf_parameters import _render_generic_tab, _GENERIC_PARAMS

    with patch("iidm_viewer.lf_parameters.st") as mock_st:
        mock_st.session_state = {}
        mock_st.checkbox.return_value = True
        mock_st.selectbox.return_value = "UNIFORM_VALUES"
        mock_st.number_input.return_value = 1.0
        result = _render_generic_tab()

    expected = {p[0] for p in _GENERIC_PARAMS}
    assert set(result.keys()) == expected


def test_render_generic_tab_uses_correct_widget_per_type():
    """bool → checkbox, enum → selectbox, float → number_input."""
    from iidm_viewer.lf_parameters import _render_generic_tab, _GENERIC_PARAMS

    bool_names = {p[0] for p in _GENERIC_PARAMS if p[1] == "bool"}
    enum_names = {p[0] for p in _GENERIC_PARAMS if p[1] == "enum"}
    float_names = {p[0] for p in _GENERIC_PARAMS if p[1] == "float"}

    with patch("iidm_viewer.lf_parameters.st") as mock_st:
        mock_st.session_state = {}
        mock_st.checkbox.return_value = False
        mock_st.selectbox.return_value = "UNIFORM_VALUES"
        mock_st.number_input.return_value = 0.0
        _render_generic_tab()

    assert mock_st.checkbox.call_count == len(bool_names)
    assert mock_st.selectbox.call_count == len(enum_names)
    assert mock_st.number_input.call_count == len(float_names)


# ---------------------------------------------------------------------------
# _render_provider_tab (lines 87-155)
# ---------------------------------------------------------------------------


def _provider_info_df():
    """Minimal provider params DataFrame covering all widget type branches."""
    return pd.DataFrame(
        {
            "type": ["BOOLEAN", "INTEGER", "DOUBLE", "STRING", "STRING", "STRING"],
            "default": ["true", "10", "1.0", "VAL1", "text", "other"],
            "description": ["d1", "d2", "d3", "d4", "d5", "d6"],
            "category_key": ["cat"] * 6,
            "possible_values": [
                None, None, None,
                "[VAL1, VAL2]",  # enum-like string list → selectbox
                "",              # empty string → no options → text_input
                None,            # None → else branch → text_input
            ],
        },
        index=["boolP", "intP", "dblP", "enumP", "strP1", "strP2"],
    )


def test_render_provider_tab_calls_correct_widgets():
    """BOOLEAN→checkbox, INTEGER→number_input(step=1), DOUBLE→number_input(%g),
    STRING with options→selectbox, STRING without options→text_input."""
    from iidm_viewer.lf_parameters import _render_provider_tab

    expander_cm = MagicMock()
    expander_cm.__enter__ = MagicMock(return_value=None)
    expander_cm.__exit__ = MagicMock(return_value=False)

    with patch("iidm_viewer.lf_parameters.st") as mock_st, \
         patch("iidm_viewer.lf_parameters._get_provider_params_info",
               return_value=_provider_info_df()):
        mock_st.session_state = {}
        mock_st.expander.return_value = expander_cm
        mock_st.checkbox.return_value = True
        mock_st.number_input.return_value = 1
        mock_st.selectbox.return_value = "VAL1"
        mock_st.text_input.return_value = "x"
        result = _render_provider_tab()

    assert "boolP" in result    # BOOLEAN → checkbox
    assert "intP" in result     # INTEGER → number_input
    assert "dblP" in result     # DOUBLE  → number_input
    assert "enumP" in result    # STRING  → selectbox (has options)
    assert "strP1" in result    # STRING  → text_input (empty possible_values)
    assert "strP2" in result    # STRING/None → else → text_input
    assert mock_st.checkbox.call_count == 1
    assert mock_st.selectbox.call_count == 1
    assert mock_st.text_input.call_count == 2


def test_render_provider_tab_iterable_possible_values():
    """STRING param with a list possible_values (non-string iterable) → selectbox."""
    from iidm_viewer.lf_parameters import _render_provider_tab

    info_df = pd.DataFrame(
        {
            "type": ["STRING"],
            "default": ["A"],
            "description": ["d1"],
            "category_key": ["cat"],
            "possible_values": [["A", "B"]],  # list, not a bracketed string
        },
        index=["listP"],
    )

    expander_cm = MagicMock()
    expander_cm.__enter__ = MagicMock(return_value=None)
    expander_cm.__exit__ = MagicMock(return_value=False)

    with patch("iidm_viewer.lf_parameters.st") as mock_st, \
         patch("iidm_viewer.lf_parameters._get_provider_params_info",
               return_value=info_df):
        mock_st.session_state = {}
        mock_st.expander.return_value = expander_cm
        mock_st.selectbox.return_value = "A"
        _render_provider_tab()

    mock_st.selectbox.assert_called_once()
