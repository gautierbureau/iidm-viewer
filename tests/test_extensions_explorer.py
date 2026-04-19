"""Data Explorer Extensions tab: extension list, filter, edit tracking, and removal."""
import pandas as pd
import pytest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
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


def test_readonly_extension_uses_data_editor_for_removal(xiidm_upload):
    """Non-editable extensions (e.g. slackTerminal) now use data_editor so rows
    can be marked for removal, even though no property columns are editable."""
    at = _prepare(xiidm_upload)
    at.selectbox(key="extension_type_select").select(
        "slackTerminal"
    ).run(timeout=30)
    assert not at.exception
    ids = _dataframe_ids(at)
    assert any("ext_editor_slackTerminal" in pid for pid in ids)


@contextmanager
def _mock_ext_state(module):
    """Replace st.session_state in the given module with a plain dict."""
    with patch(f"{module}.st") as mock_st:
        mock_st.session_state = {}
        yield mock_st.session_state


# ---------------------------------------------------------------------------
# _add_to_ext_change_log unit tests
# ---------------------------------------------------------------------------


def test_add_to_ext_change_log_creates_entry_with_before_and_after():
    from iidm_viewer.extensions_explorer import _add_to_ext_change_log

    orig = pd.DataFrame({"droop": [4.0]}, index=pd.Index(["B1-G"]))
    changes = pd.DataFrame({"droop": [6.0]}, index=pd.Index(["B1-G"]))

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_change_log("activePowerControl", changes, orig)

    log = fake_state["_ext_change_log_activePowerControl"]
    assert len(log) == 1
    assert log[0]["element_id"] == "B1-G"
    assert log[0]["property"] == "droop"
    assert log[0]["before"] == 4.0
    assert log[0]["after"] == 6.0


def test_add_to_ext_change_log_updates_after_on_second_edit():
    from iidm_viewer.extensions_explorer import _add_to_ext_change_log

    orig = pd.DataFrame({"droop": [4.0]}, index=pd.Index(["B1-G"]))
    changes1 = pd.DataFrame({"droop": [6.0]}, index=pd.Index(["B1-G"]))
    changes2 = pd.DataFrame({"droop": [8.0]}, index=pd.Index(["B1-G"]))

    fake_state = {}
    with patch("iidm_explorer.extensions_explorer.st.session_state", fake_state) if False else \
         patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_change_log("activePowerControl", changes1, orig)
        _add_to_ext_change_log("activePowerControl", changes2, orig)

    log = fake_state["_ext_change_log_activePowerControl"]
    assert len(log) == 1
    assert log[0]["before"] == 4.0
    assert log[0]["after"] == 8.0


def test_add_to_ext_change_log_removes_entry_when_reverted_to_original():
    from iidm_viewer.extensions_explorer import _add_to_ext_change_log

    orig = pd.DataFrame({"droop": [4.0]}, index=pd.Index(["B1-G"]))
    changes_fwd = pd.DataFrame({"droop": [6.0]}, index=pd.Index(["B1-G"]))
    changes_back = pd.DataFrame({"droop": [4.0]}, index=pd.Index(["B1-G"]))

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_change_log("activePowerControl", changes_fwd, orig)
        _add_to_ext_change_log("activePowerControl", changes_back, orig)

    log = fake_state["_ext_change_log_activePowerControl"]
    assert log == []


def test_add_to_ext_change_log_skips_nan_new_values():
    import numpy as np
    from iidm_viewer.extensions_explorer import _add_to_ext_change_log

    orig = pd.DataFrame({"droop": [4.0]}, index=pd.Index(["B1-G"]))
    changes = pd.DataFrame({"droop": [float("nan")]}, index=pd.Index(["B1-G"]))

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_change_log("activePowerControl", changes, orig)

    log = fake_state.get("_ext_change_log_activePowerControl", [])
    assert log == []


def test_add_to_ext_change_log_handles_nonscalar_that_raises_in_isna():
    """When pd.isna(value) raises TypeError (e.g. for a list), the value is
    treated as non-NaN and the entry is recorded — covers the except branch."""
    from iidm_viewer.extensions_explorer import _add_to_ext_change_log

    # pd.isna([1, 2]) returns an array; using it in a boolean context raises
    # ValueError, which triggers the except (TypeError, ValueError): pass branch.
    orig = pd.DataFrame({"code": [None]}, index=pd.Index(["S1"]))
    changes = pd.DataFrame({"code": [[1, 2]]}, index=pd.Index(["S1"]))

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_change_log("entsoeArea", changes, orig)

    log = fake_state.get("_ext_change_log_entsoeArea", [])
    # The list value is non-NaN so an entry must have been created
    assert len(log) == 1
    assert log[0]["element_id"] == "S1"


def test_add_to_ext_change_log_multiple_properties_and_elements():
    from iidm_viewer.extensions_explorer import _add_to_ext_change_log

    orig = pd.DataFrame(
        {"droop": [4.0, 3.0], "participate": [True, False]},
        index=pd.Index(["G1", "G2"]),
    )
    changes = pd.DataFrame(
        {"droop": [5.0, 2.0]},
        index=pd.Index(["G1", "G2"]),
    )

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_change_log("activePowerControl", changes, orig)

    log = fake_state["_ext_change_log_activePowerControl"]
    assert len(log) == 2
    entries_by_id = {e["element_id"]: e for e in log}
    assert entries_by_id["G1"]["after"] == 5.0
    assert entries_by_id["G2"]["after"] == 2.0


def test_add_to_ext_change_log_separate_keys_per_extension():
    from iidm_viewer.extensions_explorer import _add_to_ext_change_log

    orig_a = pd.DataFrame({"droop": [4.0]}, index=pd.Index(["G1"]))
    changes_a = pd.DataFrame({"droop": [5.0]}, index=pd.Index(["G1"]))
    orig_b = pd.DataFrame({"slope": [0.01]}, index=pd.Index(["SVC1"]))
    changes_b = pd.DataFrame({"slope": [0.02]}, index=pd.Index(["SVC1"]))

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_change_log("activePowerControl", changes_a, orig_a)
        _add_to_ext_change_log("voltagePerReactivePowerControl", changes_b, orig_b)

    assert "_ext_change_log_activePowerControl" in fake_state
    assert "_ext_change_log_voltagePerReactivePowerControl" in fake_state
    assert len(fake_state["_ext_change_log_activePowerControl"]) == 1
    assert len(fake_state["_ext_change_log_voltagePerReactivePowerControl"]) == 1


# ---------------------------------------------------------------------------
# _add_to_ext_removal_log unit tests
# ---------------------------------------------------------------------------


def test_add_to_ext_removal_log_stores_element_id_and_snapshot():
    from iidm_viewer.extensions_explorer import _add_to_ext_removal_log

    snapshot = pd.DataFrame(
        {"droop": [4.0], "participate": [True]},
        index=pd.Index(["G1"]),
    )

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_removal_log("activePowerControl", ["G1"], snapshot)

    log = fake_state["_ext_removal_log_activePowerControl"]
    assert len(log) == 1
    assert log[0]["element_id"] == "G1"
    assert log[0]["snapshot"]["droop"] == 4.0
    assert log[0]["snapshot"]["participate"] is True


def test_add_to_ext_removal_log_deduplicates_repeated_ids():
    from iidm_viewer.extensions_explorer import _add_to_ext_removal_log

    snapshot = pd.DataFrame({"droop": [4.0]}, index=pd.Index(["G1"]))

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_removal_log("activePowerControl", ["G1"], snapshot)
        _add_to_ext_removal_log("activePowerControl", ["G1"], snapshot)

    assert len(fake_state["_ext_removal_log_activePowerControl"]) == 1


def test_add_to_ext_removal_log_unknown_id_gets_empty_snapshot():
    from iidm_viewer.extensions_explorer import _add_to_ext_removal_log

    snapshot = pd.DataFrame({"droop": [4.0]}, index=pd.Index(["G1"]))

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_removal_log("activePowerControl", ["G1", "G_UNKNOWN"], snapshot)

    log = fake_state["_ext_removal_log_activePowerControl"]
    by_id = {e["element_id"]: e for e in log}
    assert by_id["G1"]["snapshot"]["droop"] == 4.0
    assert by_id["G_UNKNOWN"]["snapshot"] == {}


def test_add_to_ext_removal_log_separate_keys_per_extension():
    from iidm_viewer.extensions_explorer import _add_to_ext_removal_log

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_removal_log("activePowerControl", ["G1"], pd.DataFrame())
        _add_to_ext_removal_log("voltagePerReactivePowerControl", ["SVC1"], pd.DataFrame())

    assert "_ext_removal_log_activePowerControl" in fake_state
    assert "_ext_removal_log_voltagePerReactivePowerControl" in fake_state
    assert fake_state["_ext_removal_log_activePowerControl"][0]["element_id"] == "G1"
    assert fake_state["_ext_removal_log_voltagePerReactivePowerControl"][0]["element_id"] == "SVC1"


# ---------------------------------------------------------------------------
# remove_extension — integration with pypowsybl
# ---------------------------------------------------------------------------


def test_remove_extension_removes_row_from_network(node_breaker_network):
    from iidm_viewer.state import remove_extension

    create_extension(
        node_breaker_network, "activePowerControl", "GH1",
        {"participate": True, "droop": 4.0},
    )
    before = node_breaker_network.get_extensions("activePowerControl")
    assert "GH1" in before.index

    with patch("iidm_viewer.state.st") as mock_st:
        mock_st.session_state = {}
        remove_extension(node_breaker_network, "activePowerControl", ["GH1"])

    after = node_breaker_network.get_extensions("activePowerControl")
    assert after is None or "GH1" not in after.index


def test_remove_extension_clears_vl_lookup_cache(node_breaker_network):
    from iidm_viewer.state import remove_extension

    create_extension(
        node_breaker_network, "activePowerControl", "GH1",
        {"participate": True, "droop": 4.0},
    )

    with patch("iidm_viewer.state.st") as mock_st:
        mock_st.session_state = {"_vl_lookup_cache": "stale"}
        remove_extension(node_breaker_network, "activePowerControl", ["GH1"])
        assert "_vl_lookup_cache" not in mock_st.session_state


# ---------------------------------------------------------------------------
# load_network / create_empty_network — ext log clearing
# ---------------------------------------------------------------------------


class _FakeSessionState(dict):
    """Dict subclass with attribute-style access, mimicking st.session_state."""
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


def test_load_network_clears_ext_change_log(xiidm_upload):
    from iidm_viewer.state import load_network

    with patch("iidm_viewer.state.st") as mock_st:
        mock_st.session_state = _FakeSessionState({
            "_ext_change_log_activePowerControl": [{"element_id": "G1"}],
            "_ext_change_log_entsoeCategory": [{"element_id": "G2"}],
        })
        load_network(xiidm_upload)
        remaining = [k for k in mock_st.session_state if k.startswith("_ext_change_log_")]
        assert remaining == []


def test_load_network_clears_ext_removal_log(xiidm_upload):
    from iidm_viewer.state import load_network

    with patch("iidm_viewer.state.st") as mock_st:
        mock_st.session_state = _FakeSessionState({
            "_ext_removal_log_activePowerControl": [{"element_id": "G1"}],
        })
        load_network(xiidm_upload)
        remaining = [k for k in mock_st.session_state if k.startswith("_ext_removal_log_")]
        assert remaining == []


def test_load_network_preserves_non_log_keys(xiidm_upload):
    """load_network must not wipe unrelated session_state keys."""
    from iidm_viewer.state import load_network

    with patch("iidm_viewer.state.st") as mock_st:
        mock_st.session_state = _FakeSessionState({
            "_ext_change_log_activePowerControl": [{}],
            "nad_depth": 2,
        })
        load_network(xiidm_upload)
        assert "nad_depth" in mock_st.session_state


def test_create_empty_network_clears_ext_logs():
    from iidm_viewer.state import create_empty_network

    with patch("iidm_viewer.state.st") as mock_st:
        mock_st.session_state = _FakeSessionState({
            "_ext_change_log_activePowerControl": [{"element_id": "G1"}],
            "_ext_removal_log_entsoeCategory": [{"element_id": "G2"}],
        })
        create_empty_network("test_net")
        remaining_change = [k for k in mock_st.session_state if k.startswith("_ext_change_log_")]
        remaining_removal = [k for k in mock_st.session_state if k.startswith("_ext_removal_log_")]
        assert remaining_change == []
        assert remaining_removal == []


# ---------------------------------------------------------------------------
# Existing structural tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _add_to_ext_change_log — exception in before==after comparison (lines 62-63)
# ---------------------------------------------------------------------------


def test_add_to_ext_change_log_handles_exception_in_before_after_comparison():
    """Lines 62-63: when existing['before'] == existing['after'] raises (e.g.
    because the value is a numpy array), the exception is swallowed and the
    log entry is kept."""
    import numpy as np
    from iidm_viewer.extensions_explorer import _add_to_ext_change_log

    # First call: creates a normal entry {before: 4.0, after: 6.0}
    orig = pd.DataFrame({"droop": [4.0]}, index=pd.Index(["G1"]))
    changes1 = pd.DataFrame({"droop": [6.0]}, index=pd.Index(["G1"]))
    # Second call: tries to set after=array; 4.0 == array raises ValueError
    changes2 = pd.DataFrame({"droop": [np.array([1, 2])]}, index=pd.Index(["G1"]))

    fake_state = {}
    with patch("iidm_viewer.extensions_explorer.st.session_state", fake_state):
        _add_to_ext_change_log("myExt", changes1, orig)
        _add_to_ext_change_log("myExt", changes2, orig)

    log = fake_state.get("_ext_change_log_myExt", [])
    assert len(log) == 1  # entry stays because comparison raised


# ---------------------------------------------------------------------------
# _render_ext_change_log (lines 74-104)
# ---------------------------------------------------------------------------


def _col_mocks(revert_clicked=False):
    cols = [MagicMock() for _ in range(5)]
    cols[4].button.return_value = revert_clicked
    return cols


def test_render_ext_change_log_non_empty_renders_header_and_rows():
    """Lines 74-86: markdown header + column headers + row text rendered."""
    from iidm_viewer.extensions_explorer import _render_ext_change_log

    log = [{"element_id": "G1", "property": "droop", "before": 4.0, "after": 6.0}]
    cols = _col_mocks(revert_clicked=False)

    with patch("iidm_viewer.extensions_explorer.st") as mock_st:
        mock_st.session_state = {"_ext_change_log_myExt": log}
        mock_st.columns.return_value = cols
        _render_ext_change_log(MagicMock(), "myExt")

    mock_st.markdown.assert_called()
    cols[0].text.assert_called_with("G1")
    cols[1].text.assert_called_with("droop")


def test_render_ext_change_log_revert_with_none_before_shows_error():
    """Lines 87-92: before=None → st.error (cannot revert)."""
    from iidm_viewer.extensions_explorer import _render_ext_change_log

    log = [{"element_id": "G1", "property": "droop", "before": None, "after": 6.0}]
    cols = _col_mocks(revert_clicked=True)

    with patch("iidm_viewer.extensions_explorer.st") as mock_st:
        mock_st.session_state = {"_ext_change_log_myExt": list(log)}
        mock_st.columns.return_value = cols
        _render_ext_change_log(MagicMock(), "myExt")

    mock_st.error.assert_called_once()


def test_render_ext_change_log_revert_success_calls_update_and_rerun():
    """Lines 93-102: valid before → update_extension called, rerun triggered."""
    from iidm_viewer.extensions_explorer import _render_ext_change_log

    log = [{"element_id": "G1", "property": "droop", "before": 4.0, "after": 6.0}]
    cols = _col_mocks(revert_clicked=True)

    with patch("iidm_viewer.extensions_explorer.st") as mock_st, \
         patch("iidm_viewer.extensions_explorer.update_extension") as mock_upd:
        mock_st.session_state = {"_ext_change_log_myExt": list(log)}
        mock_st.columns.return_value = cols
        _render_ext_change_log(MagicMock(), "myExt")

    mock_upd.assert_called_once()
    mock_st.rerun.assert_called_once()


def test_render_ext_change_log_revert_failure_shows_error():
    """Lines 103-104: update_extension raises → st.error displayed."""
    from iidm_viewer.extensions_explorer import _render_ext_change_log

    log = [{"element_id": "G1", "property": "droop", "before": 4.0, "after": 6.0}]
    cols = _col_mocks(revert_clicked=True)

    with patch("iidm_viewer.extensions_explorer.st") as mock_st, \
         patch("iidm_viewer.extensions_explorer.update_extension",
               side_effect=RuntimeError("network error")):
        mock_st.session_state = {"_ext_change_log_myExt": list(log)}
        mock_st.columns.return_value = cols
        _render_ext_change_log(MagicMock(), "myExt")

    mock_st.error.assert_called_once()


# ---------------------------------------------------------------------------
# _render_ext_removal_log (lines 125-128, 134-135)
# ---------------------------------------------------------------------------


def test_render_ext_removal_log_shows_header_and_items():
    """Lines 125-128 and 134-135: non-empty log → markdown + captions."""
    from iidm_viewer.extensions_explorer import _render_ext_removal_log

    log = [{"element_id": "G1", "snapshot": {}}, {"element_id": "G2", "snapshot": {}}]
    with patch("iidm_viewer.extensions_explorer.st") as mock_st:
        mock_st.session_state = {"_ext_removal_log_myExt": log}
        _render_ext_removal_log("myExt")

    mock_st.markdown.assert_called_once()
    assert mock_st.caption.call_count == 2


# ---------------------------------------------------------------------------
# render_extensions_explorer — error handler (lines 255-256)
# ---------------------------------------------------------------------------


def test_render_extensions_explorer_handles_get_extensions_error():
    """Lines 255-256: network.get_extensions() raises → st.error displayed."""
    from iidm_viewer.extensions_explorer import render_extensions_explorer

    spinner_cm = MagicMock()
    spinner_cm.__enter__ = MagicMock(return_value=None)
    spinner_cm.__exit__ = MagicMock(return_value=False)

    with patch("iidm_viewer.extensions_explorer.st") as mock_st, \
         patch("iidm_viewer.extensions_explorer._extensions_names",
               return_value=["myExt"]), \
         patch("iidm_viewer.extensions_explorer._extensions_information",
               return_value=pd.DataFrame()):
        mock_st.session_state = {}
        mock_st.selectbox.return_value = "myExt"
        mock_st.text_input.return_value = ""
        mock_st.spinner.return_value = spinner_cm

        net = MagicMock()
        net.get_extensions.side_effect = RuntimeError("broken")
        render_extensions_explorer(net)

    mock_st.error.assert_called()


# ---------------------------------------------------------------------------
# Existing structural tests
# ---------------------------------------------------------------------------

def test_extensions_tab_present_and_components_renamed(xiidm_upload):
    """App must expose both 'Data Explorer Components' and 'Data Explorer Extensions'."""
    at = _prepare(xiidm_upload)
    labels = []
    for tab in at.tabs:
        labels.append(tab.label)
    assert "Data Explorer Components" in labels
    assert "Data Explorer Extensions" in labels
    assert "Data Explorer" not in labels  # the old standalone label is gone
