"""NAD and SLD tab rendering."""
from streamlit.testing.v1 import AppTest

from iidm_viewer.state import load_network


def _prepare(xiidm_upload, selected_vl=None):
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = load_network(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    if selected_vl is not None:
        at.session_state["selected_vl"] = selected_vl
    at.run(timeout=30)
    return at


def test_nad_tab_info_when_no_vl_selected(xiidm_upload):
    """Empty VL filter -> vl_selector returns None -> NAD tab shows its info."""
    at = _prepare(xiidm_upload)
    at.text_input(key="vl_filter_text").set_value("ZZZZZZ").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any("Network Area Diagram" in i for i in infos)


def test_sld_tab_info_when_no_vl_selected(xiidm_upload):
    at = _prepare(xiidm_upload)
    at.text_input(key="vl_filter_text").set_value("ZZZZZZ").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any("Single Line Diagram" in i for i in infos)


def test_nad_and_sld_render_without_exception_for_valid_vl(xiidm_upload):
    at = _prepare(xiidm_upload, selected_vl="VL1")
    assert not at.exception
