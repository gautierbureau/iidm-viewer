"""Data Explorer Extensions tab: extension list, filter, and empty states."""
import pytest
from streamlit.testing.v1 import AppTest

from iidm_viewer.state import create_extension, load_network


def _prepare(xiidm_upload):
    at = AppTest.from_file("iidm_viewer/app.py")
    at.run(timeout=30)
    at.session_state["network"] = load_network(xiidm_upload)
    at.session_state["_last_file"] = xiidm_upload.name
    at.run(timeout=30)
    return at


def test_extension_selectbox_is_populated(xiidm_upload):
    at = _prepare(xiidm_upload)
    options = at.selectbox(key="extension_type_select").options
    assert "slackTerminal" in options
    assert "position" in options  # known extension, not present in IEEE14


def test_slack_terminal_extension_shows_rows(xiidm_upload):
    """IEEE14 carries a slackTerminal extension; the dataframe must surface it."""
    at = _prepare(xiidm_upload)
    at.selectbox(key="extension_type_select").select("slackTerminal").run(timeout=30)
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("1" in c and "slackTerminal" in c for c in captions)


def test_absent_extension_renders_empty_info(xiidm_upload):
    """Selecting an extension type with no instances must render an info, not error."""
    at = _prepare(xiidm_upload)
    at.selectbox(key="extension_type_select").select("position").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any("No 'position' extensions found" in i for i in infos)


def test_id_filter_narrows_rows(xiidm_upload):
    at = _prepare(xiidm_upload)
    at.selectbox(key="extension_type_select").select("slackTerminal").run(timeout=30)
    # slackTerminal row for IEEE14 is indexed by the voltage level / terminal id "VL1_0".
    at.text_input(key="id_filter_ext_slackTerminal").set_value("VL1").run(timeout=30)
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("1 of 1" in c for c in captions)


def test_id_filter_no_match_renders_info(xiidm_upload):
    at = _prepare(xiidm_upload)
    at.selectbox(key="extension_type_select").select("slackTerminal").run(timeout=30)
    at.text_input(key="id_filter_ext_slackTerminal").set_value("ZZZZZZ").run(timeout=30)
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any("match ID filter" in i for i in infos)


def test_extension_detail_caption_rendered(xiidm_upload):
    """When the extension has a description in get_extensions_information, show it."""
    at = _prepare(xiidm_upload)
    at.selectbox(key="extension_type_select").select("position").run(timeout=30)
    captions = [c.value for c in at.caption]
    # The 'position' extension description mentions connectable position.
    assert any("position" in c.lower() for c in captions)


def _dataframe_ids(at):
    """Return the widget id on each rendered dataframe/data_editor.

    AppTest reports ``st.data_editor`` as a ``Dataframe`` element; the key
    (if any) is encoded in the proto's ``id`` field as
    ``$$ID-<hash>-<key>``.
    """
    return [d.proto.id for d in at.dataframe]


def test_editable_extension_shows_data_editor(xiidm_upload):
    """Selecting an editable extension (activePowerControl) must render a data_editor."""
    at = _prepare(xiidm_upload)
    # Seed the network with an activePowerControl row on B1-G.
    create_extension(
        at.session_state["network"], "activePowerControl", "B1-G",
        {"participate": True, "droop": 4.0},
    )
    at.selectbox(key="extension_type_select").select(
        "activePowerControl"
    ).run(timeout=30)
    assert not at.exception
    ids = _dataframe_ids(at)
    assert any("ext_editor_activePowerControl" in pid for pid in ids)


def test_readonly_extension_falls_back_to_dataframe(xiidm_upload):
    """Non-editable extensions (e.g. slackTerminal) keep the static dataframe view."""
    at = _prepare(xiidm_upload)
    at.selectbox(key="extension_type_select").select(
        "slackTerminal"
    ).run(timeout=30)
    assert not at.exception
    ids = _dataframe_ids(at)
    assert all("ext_editor_slackTerminal" not in pid for pid in ids)


def test_extensions_tab_present_and_components_renamed(xiidm_upload):
    """App must expose both 'Data Explorer Components' and 'Data Explorer Extensions'."""
    at = _prepare(xiidm_upload)
    labels = []
    for tab in at.tabs:
        labels.append(tab.label)
    assert "Data Explorer Components" in labels
    assert "Data Explorer Extensions" in labels
    assert "Data Explorer" not in labels  # the old standalone label is gone
