"""Tests for iidm_viewer.lf_parameters."""
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
