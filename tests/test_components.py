"""Sidebar voltage-level selector behavior."""
from streamlit.testing.v1 import AppTest

from iidm_viewer.state import load_network


def _prepare(xiidm_upload):
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = load_network(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.run(timeout=30)
    return at


def test_vl_selector_populates_session_state(xiidm_upload):
    at = _prepare(xiidm_upload)
    assert at.session_state["selected_vl"] is not None
    assert at.session_state["selected_vl"] in at.selectbox(key="vl_selectbox_0").options


def test_vl_filter_no_match_renders_info(xiidm_upload):
    at = _prepare(xiidm_upload)
    at.text_input(key="vl_filter_text_0").set_value("ZZZZZZZ").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any("No voltage levels match" in i for i in infos)


def test_vl_filter_narrows_selectbox_options(xiidm_upload):
    at = _prepare(xiidm_upload)
    at.text_input(key="vl_filter_text_0").set_value("VL1").run(timeout=30)
    assert not at.exception
    options = at.selectbox(key="vl_selectbox_0").options
    assert all("VL1" in o or "vl1" in o.lower() for o in options)
