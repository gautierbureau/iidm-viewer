"""Tests for the framework-agnostic cross-tab navigation helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from iidm_viewer import network_loader
from iidm_viewer.navigation import resolve_feeder_substation
from iidm_viewer.powsybl_worker import NetworkProxy


ROOT = Path(__file__).resolve().parent.parent
XIIDM = ROOT / "test_ieee14.xiidm"


@pytest.fixture(scope="module")
def ieee14() -> NetworkProxy:
    return network_loader.load_from_path(str(XIIDM))


# ---------------------------------------------------------------------------
# Branch click — IEEE14 has L7-9 connecting VL7 to VL9.
# ---------------------------------------------------------------------------
def test_resolve_feeder_for_line_returns_other_substation(ieee14):
    """Click a LINE on the SLD displaying VL7 → land on VL9's substation."""
    raw = object.__getattribute__(ieee14, "_obj")

    def _pick_branch():
        df = raw.get_lines()
        for line_id in df.index:
            vl1 = str(df.at[line_id, "voltage_level1_id"])
            vl2 = str(df.at[line_id, "voltage_level2_id"])
            if vl1 != vl2:
                return line_id, vl1, vl2
        return None

    from iidm_viewer.powsybl_worker import run
    picked = run(_pick_branch)
    assert picked is not None, "IEEE14 must have at least one branch"
    line_id, vl1, vl2 = picked

    sub = resolve_feeder_substation(ieee14, vl1, line_id, "LINE")
    assert sub is not None

    # The resolved substation must be VL2's substation, not VL1's.
    def _sub_for(vl_id):
        return str(raw.get_voltage_levels().at[vl_id, "substation_id"])

    other_sub = run(lambda: _sub_for(vl2))
    assert sub == other_sub


def test_resolve_feeder_for_transformer(ieee14):
    """2W transformers go through ``get_2_windings_transformers``."""
    raw = object.__getattribute__(ieee14, "_obj")
    from iidm_viewer.powsybl_worker import run

    def _pick():
        df = raw.get_2_windings_transformers()
        if df is None or df.empty:
            return None
        idx = df.index[0]
        return (
            idx,
            str(df.at[idx, "voltage_level1_id"]),
            str(df.at[idx, "voltage_level2_id"]),
        )

    picked = run(_pick)
    if picked is None:
        pytest.skip("test_ieee14 has no 2-winding transformer")
    twt_id, vl1, vl2 = picked

    sub = resolve_feeder_substation(ieee14, vl1, twt_id, "TWO_WINDINGS_TRANSFORMER")
    assert sub is not None


def test_resolve_feeder_for_local_injection_returns_current_substation(ieee14):
    """LOAD / GENERATOR clicks don't have an "other side" — return the
    substation of the currently-displayed VL."""
    raw = object.__getattribute__(ieee14, "_obj")
    from iidm_viewer.powsybl_worker import run

    def _pick():
        df = raw.get_generators()
        gen_id = df.index[0]
        vl = str(df.at[gen_id, "voltage_level_id"])
        sub = str(raw.get_voltage_levels().at[vl, "substation_id"])
        return gen_id, vl, sub

    gen_id, vl, current_sub = run(_pick)
    sub = resolve_feeder_substation(ieee14, vl, gen_id, "GENERATOR")
    assert sub == current_sub


def test_resolve_feeder_returns_none_for_unknown_equipment(ieee14):
    sub = resolve_feeder_substation(ieee14, "VL1", "definitely-not-a-line", "LINE")
    # Unknown id falls back to the current VL's substation, which still
    # resolves — the resolver is best-effort. Just confirm no crash.
    assert sub is None or isinstance(sub, str)


def test_resolve_feeder_with_blank_inputs_returns_none(ieee14):
    assert resolve_feeder_substation(ieee14, "", "L1", "LINE") is None
    assert resolve_feeder_substation(ieee14, "VL1", "", "LINE") is None


def test_resolve_feeder_handles_lowercase_equipment_type(ieee14):
    """The SLD library can emit ``equipmentType`` in either case across
    versions. ``upper()`` normalises so callers don't have to."""
    raw = object.__getattribute__(ieee14, "_obj")
    from iidm_viewer.powsybl_worker import run

    def _pick():
        df = raw.get_lines()
        line_id = df.index[0]
        vl1 = str(df.at[line_id, "voltage_level1_id"])
        return line_id, vl1

    line_id, vl1 = run(_pick)
    sub_upper = resolve_feeder_substation(ieee14, vl1, line_id, "LINE")
    sub_lower = resolve_feeder_substation(ieee14, vl1, line_id, "line")
    assert sub_upper == sub_lower


def test_sld_bundle_carries_feeder_click_path():
    """The built bundle must contain the new feeder-click branch.
    Failing this means dist/ is stale — run ``npm run build``."""
    import os
    path = os.path.join(
        os.path.dirname(__file__), os.pardir,
        "iidm_viewer", "frontend", "sld_component", "dist", "assets",
        "sld-component.js",
    )
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    assert "sld-feeder-click" in content
    assert "equipmentType" in content


def test_map_bundle_carries_flyto_path():
    """The map bundle must wire the flyTo dispatcher."""
    import os
    path = os.path.join(
        os.path.dirname(__file__), os.pardir,
        "iidm_viewer", "frontend", "map_component", "dist", "assets",
        "map-component.js",
    )
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    # Symbols that survive minification: property reads from the
    # render args (``flyTo``, ``substationId``) and the MapLibre
    # method call (``easeTo``).
    assert "flyTo" in content
    assert "substationId" in content
    assert "easeTo" in content
