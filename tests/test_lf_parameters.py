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
