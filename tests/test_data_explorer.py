"""Data Explorer tab: ID substring filter and VL filter."""
import pytest
from streamlit.testing.v1 import AppTest

from iidm_viewer.state import load_network


def _prepare(xiidm_upload):
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = load_network(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.run(timeout=30)
    return at


def test_data_explorer_renders_generators(xiidm_upload):
    at = _prepare(xiidm_upload)
    at.selectbox(key="component_type_select").select("Generators").run(timeout=30)
    assert not at.exception
    # caption shows full count when no filter is set
    captions = [c.value for c in at.caption]
    assert any("5 generators" in c for c in captions)


def test_data_explorer_id_filter_narrows_rows(xiidm_upload):
    at = _prepare(xiidm_upload)
    at.selectbox(key="component_type_select").select("Generators").run(timeout=30)
    at.text_input(key="id_filter_get_generators").set_value("B2").run(timeout=30)
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("1 of 5 generators" in c for c in captions)


def test_data_explorer_id_filter_is_case_insensitive(xiidm_upload):
    at = _prepare(xiidm_upload)
    at.selectbox(key="component_type_select").select("Generators").run(timeout=30)
    at.text_input(key="id_filter_get_generators").set_value("b1").run(timeout=30)
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("1 of 5 generators" in c for c in captions)


def test_data_explorer_id_filter_empty_state(xiidm_upload):
    at = _prepare(xiidm_upload)
    at.selectbox(key="component_type_select").select("Generators").run(timeout=30)
    at.text_input(key="id_filter_get_generators").set_value("ZZZZZZ").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any("No generators match the current filters" in i for i in infos)


def test_data_explorer_filter_key_is_per_component(xiidm_upload):
    """Switching component types should not leak the ID filter string."""
    at = _prepare(xiidm_upload)
    at.selectbox(key="component_type_select").select("Generators").run(timeout=30)
    at.text_input(key="id_filter_get_generators").set_value("B2").run(timeout=30)

    at.selectbox(key="component_type_select").select("Lines").run(timeout=30)
    assert not at.exception
    # Lines should show full count; filter widget for generators is gone.
    captions = [c.value for c in at.caption]
    assert any("17 lines" in c for c in captions)


def test_data_explorer_empty_component_shows_info(xiidm_upload):
    """Component types absent from IEEE14 should render an info message, not an error."""
    at = _prepare(xiidm_upload)
    at.selectbox(key="component_type_select").select("Batteries").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any("No batteries found" in i for i in infos)


def test_data_explorer_vl_filter_checkbox_appears_for_generators(xiidm_upload):
    """VL_FILTERABLE components expose a 'Filter by selected VL' checkbox."""
    at = _prepare(xiidm_upload)
    at.session_state["selected_vl"] = "VL1"
    at.selectbox(key="component_type_select").select("Generators").run(timeout=30)
    assert not at.exception
    assert any(c.key == "filter_by_vl" for c in at.checkbox)


def test_data_explorer_vl_filter_checkbox_absent_for_lines(xiidm_upload):
    """Lines are not in VL_FILTERABLE; the checkbox should not appear."""
    at = _prepare(xiidm_upload)
    at.session_state["selected_vl"] = "VL1"
    at.selectbox(key="component_type_select").select("Lines").run(timeout=30)
    assert not at.exception
    assert not any(c.key == "filter_by_vl" for c in at.checkbox)


def test_data_explorer_vl_filterable_entries_map_to_registry():
    """Every VL_FILTERABLE label must exist in COMPONENT_TYPES."""
    from iidm_viewer.data_explorer import VL_FILTERABLE
    from iidm_viewer.network_info import COMPONENT_TYPES

    assert VL_FILTERABLE.issubset(set(COMPONENT_TYPES))


def test_data_explorer_create_form_shows_info_for_bus_breaker_network(xiidm_upload):
    """IEEE14 is bus-breaker, so the create-generator form should render the
    'no node-breaker voltage levels found' info message.
    """
    at = _prepare(xiidm_upload)
    at.selectbox(key="component_type_select").select("Generators").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any("node-breaker" in i.lower() for i in infos)


def test_data_explorer_create_form_hidden_for_non_generators(xiidm_upload):
    """The create-generator form is specific to Generators and must not
    appear for other component types.
    """
    at = _prepare(xiidm_upload)
    at.selectbox(key="component_type_select").select("Loads").run(timeout=30)
    assert not at.exception
    # The info string from the create-generator form is unique enough
    infos = [i.value for i in at.info]
    assert not any("node-breaker" in i.lower() for i in infos)


def test_data_explorer_create_form_renders_for_node_breaker(node_breaker_network):
    """With a node-breaker network loaded, the Generators view must render
    the creation form (no 'node-breaker' info fallback) and must not raise.
    """
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = node_breaker_network
    at.session_state["_last_file"] = "four_substations.xiidm"
    at.run(timeout=30)
    at.selectbox(key="component_type_select").select("Generators").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    # No 'no node-breaker VLs' info should show: we DO have them.
    assert not any("no node-breaker" in i.lower() for i in infos)
    # The VL selectbox for the new generator must exist.
    assert any(s.key == "new_gen_vl" for s in at.selectbox)
