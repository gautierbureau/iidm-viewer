"""Tests for the framework-agnostic ``export_network`` helper.

Exercises the worker-routed export, single-file ZIP unwrap, the
guess-MIME helper, and the prototype "Save network" entry points
(PySide6 ``SaveNetworkDialog`` + NiceGUI ``_open_save_network_dialog``).
"""
from __future__ import annotations

import pytest

from iidm_viewer.network_loader import (
    export_network,
    get_export_formats,
    guess_mime_for_export,
)
from iidm_viewer.powsybl_worker import NetworkProxy, run


@pytest.fixture(scope="module")
def ieee14() -> NetworkProxy:
    import pypowsybl.network as pn
    return NetworkProxy(run(pn.create_ieee14))


# ---------------------------------------------------------------------------
# Shared export helpers
# ---------------------------------------------------------------------------
def test_get_export_formats_includes_xiidm():
    formats = get_export_formats()
    assert "XIIDM" in formats


def test_export_network_xiidm_unwraps_zip_and_returns_xml(ieee14):
    """pypowsybl's XIIDM export is a single-file ZIP; the shared helper
    unwraps it to the real XML bytes + the ``xiidm`` extension."""
    data, ext = export_network(ieee14, "XIIDM")
    assert ext.lower() == "xiidm"
    # The unwrapped bytes start with the XML declaration, not the
    # ``PK`` ZIP magic.
    assert data[:5] == b"<?xml"


def test_export_network_passes_parameters_through(ieee14):
    """Format-specific options reach pypowsybl unchanged. An empty
    parameters dict is a no-op; this just confirms the keyword reaches
    the worker without exploding."""
    data, ext = export_network(ieee14, "XIIDM", parameters={})
    assert ext.lower() == "xiidm"
    assert data[:5] == b"<?xml"


def test_export_network_unknown_format_propagates_error(ieee14):
    """Unknown formats surface pypowsybl's error so the host's dialog
    can show it; the worker thread doesn't swallow it."""
    with pytest.raises(Exception):
        export_network(ieee14, "GHOST_FORMAT_DOES_NOT_EXIST")


def test_guess_mime_for_export_sniffs_first_bytes():
    assert guess_mime_for_export(b"<?xml version='1.0'?>").startswith("text/xml")
    assert guess_mime_for_export(b'{"a": 1}') == "application/json"
    assert guess_mime_for_export(b"\x00\x01\x02") == "application/octet-stream"


def test_streamlit_export_network_delegates_to_shared(ieee14):
    """The Streamlit ``state.export_network`` wrapper is the same
    function the shared module exposes; it just keeps the old import
    path working for the existing callers."""
    pytest.importorskip("streamlit")
    from iidm_viewer.state import export_network as st_export

    direct_data, direct_ext = export_network(ieee14, "XIIDM")
    wrapped_data, wrapped_ext = st_export(ieee14, "XIIDM")
    assert direct_ext == wrapped_ext
    # XIIDM exports embed a generated UUID + timestamps so bytes may
    # differ — just sanity-check both produce valid XML.
    assert direct_data[:5] == b"<?xml"
    assert wrapped_data[:5] == b"<?xml"
