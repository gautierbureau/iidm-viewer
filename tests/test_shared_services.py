"""Tests for the framework-agnostic shared modules.

* ``iidm_viewer.diagram_services`` — generate_sld / generate_nad /
  extract_map_data.
* ``iidm_viewer.network_loader`` — load_from_path / load_from_bytes /
  pick_default_vl / get_import_extensions / get_export_formats.

These modules are imported by the Streamlit, PySide6 and NiceGUI
front-ends. They have no UI-framework imports, so the tests don't
need any of them either — just pypowsybl + pandas.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from iidm_viewer import diagram_services, network_loader
from iidm_viewer.powsybl_worker import NetworkProxy


ROOT = Path(__file__).resolve().parent.parent
XIIDM = ROOT / "test_ieee14.xiidm"


@pytest.fixture(scope="module")
def ieee14() -> NetworkProxy:
    return network_loader.load_from_path(str(XIIDM))


# ---------------------------------------------------------------------------
# network_loader
# ---------------------------------------------------------------------------
def test_load_from_path_returns_networkproxy(ieee14):
    assert isinstance(ieee14, NetworkProxy)


def test_load_from_bytes_handles_bare_xiidm():
    raw = XIIDM.read_bytes()
    net = network_loader.load_from_bytes("test_ieee14.xiidm", raw)
    assert isinstance(net, NetworkProxy)
    vls = net.get_voltage_levels()
    assert vls.shape[0] == 14  # IEEE14


def test_load_from_bytes_handles_zip_archive():
    import io
    import zipfile

    raw = XIIDM.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("test_ieee14.xiidm", raw)
    net = network_loader.load_from_bytes("network.zip", buf.getvalue())
    vls = net.get_voltage_levels()
    assert vls.shape[0] == 14


def test_create_empty_returns_blank_network():
    net = network_loader.create_empty("blank")
    assert net.get_voltage_levels().empty


def test_pick_default_vl_chooses_highest_voltage(ieee14):
    """IEEE14 has VLs at 13.8 kV and 230 kV — the picker must land on a
    230 kV VL (idxmax tie-break is deterministic).
    """
    pick = network_loader.pick_default_vl(ieee14)
    assert pick is not None
    vls = ieee14.get_voltage_levels()
    pick_nominal = vls.loc[pick, "nominal_v"]
    assert pick_nominal == vls["nominal_v"].max()


def test_pick_default_vl_returns_none_for_empty_network():
    net = network_loader.create_empty("empty")
    assert network_loader.pick_default_vl(net) is None


def test_import_extensions_contains_zip_and_xiidm():
    exts = network_loader.get_import_extensions()
    assert "zip" in exts
    # ``xiidm`` is the canonical IIDM extension; whichever way pypowsybl
    # names it, the registry must surface something XIIDM-shaped.
    assert any("iidm" in e or "xml" in e for e in exts)


def test_export_formats_non_empty():
    fmts = network_loader.get_export_formats()
    assert isinstance(fmts, list) and len(fmts) > 0


# ---------------------------------------------------------------------------
# diagram_services
# ---------------------------------------------------------------------------
def test_generate_sld_returns_svg_and_metadata(ieee14):
    svg, metadata = diagram_services.generate_sld(ieee14, "VL1")
    assert isinstance(svg, str) and ("<svg" in svg or svg.lstrip().startswith("<?xml"))
    assert isinstance(metadata, str) and metadata.strip().startswith("{")


def test_generate_nad_returns_svg_and_metadata(ieee14):
    svg, metadata = diagram_services.generate_nad(ieee14, "VL1", depth=1)
    assert isinstance(svg, str) and ("<svg" in svg or svg.lstrip().startswith("<?xml"))
    assert isinstance(metadata, str) and metadata.strip().startswith("{")


def test_extract_map_data_returns_geometry(ieee14):
    data = diagram_services.extract_map_data(ieee14)
    assert data is not None
    substations, positions, lines, line_positions = data
    assert len(substations) > 0
    assert len(positions) > 0
    assert len(lines) > 0
    assert isinstance(line_positions, list)


def test_extract_map_data_returns_none_for_empty_network():
    """A blank pypowsybl network has no substation positions; the
    extractor must surface that as ``None`` rather than raising.
    """
    blank = network_loader.create_empty("blank")
    assert diagram_services.extract_map_data(blank) is None
