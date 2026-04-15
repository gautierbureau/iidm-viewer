"""Regression tests for iidm_viewer.state.load_network.

Guards against the pypowsybl 1.14 segfault where networks built via
load_from_string / pn.load(path) crash on subsequent Streamlit reruns.
The fix routes every upload through load_from_binary_buffer, so these
tests assert the network is usable *and* survives a second render pass.
"""
from streamlit.testing.v1 import AppTest

from iidm_viewer.state import load_network


def test_load_xiidm_upload(xiidm_upload):
    net = load_network(xiidm_upload)
    assert net is not None
    assert len(net.get_voltage_levels()) == 14
    assert len(net.get_substations()) == 11


def test_load_zip_upload(zip_upload):
    net = load_network(zip_upload)
    assert net is not None
    assert len(net.get_voltage_levels()) == 14


def test_xiidm_network_survives_streamlit_rerun(xiidm_upload):
    """Full app flow: load xiidm, rerun, call methods on the cached network."""
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)

    at.session_state["network"] = load_network(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.run(timeout=30)

    assert not at.exception
    net = at.session_state["network"]
    # Exercising the network after a rerun is what used to segfault.
    assert len(net.get_voltage_levels()) == 14
    assert len(net.get_lines()) == 17


def test_zip_network_survives_streamlit_rerun(zip_upload):
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)

    at.session_state["network"] = load_network(zip_upload)
    at.session_state["_last_file"] = zip_upload.name
    at.run(timeout=30)

    assert not at.exception
    net = at.session_state["network"]
    assert len(net.get_voltage_levels()) == 14
