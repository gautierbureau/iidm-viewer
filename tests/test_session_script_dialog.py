"""Tests for the Streamlit ``session_script`` dialog body.

The actual Session Script feature is exercised end-to-end in
``test_session_script_e2e.py``. This file focuses on the per-Streamlit-
host glue: the recording-toggle callback + the dialog body via
``__wrapped__`` so coverage picks up the per-branch behaviour without
needing a real Streamlit runtime.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import streamlit as st

from iidm_viewer import script_recorder, session_script


def setup_function(_):
    """Reset Streamlit session state + point the recorder at it on
    every test — mirrors what ``state.init_state`` does in the app."""
    st.session_state.clear()
    script_recorder.set_store(st.session_state)


def _dialog_inner():
    """The dialog body, unwrapped from ``@st.dialog``."""
    return session_script.show_session_script_dialog.__wrapped__


# ---------------------------------------------------------------------------
# _on_recording_change — small module-level callback
# ---------------------------------------------------------------------------
def test_on_recording_change_mirrors_toggle_into_paused_flag():
    """Recording widget OFF (False) → recorder paused; ON → unpaused."""
    # Default: nothing in session_state → widget treated as ON → unpaused.
    session_script._on_recording_change()
    assert script_recorder.is_paused() is False

    st.session_state[script_recorder.RECORDING_WIDGET_KEY] = False
    session_script._on_recording_change()
    assert script_recorder.is_paused() is True

    st.session_state[script_recorder.RECORDING_WIDGET_KEY] = True
    session_script._on_recording_change()
    assert script_recorder.is_paused() is False


# ---------------------------------------------------------------------------
# show_session_script_dialog body — exercise both branches
# ---------------------------------------------------------------------------
def test_dialog_body_renders_empty_log_state():
    """Empty log → caption renders ``0 of 0`` + the script body is the
    skeleton produced by ``generate_script([])``."""
    script_recorder.clear_log()
    with patch("iidm_viewer.session_script.st") as mock_st:
        mock_st.session_state = st.session_state
        mock_st.toggle.return_value = True
        # st.columns returns a list of context managers in real Streamlit;
        # the mock auto-creates child mocks that work as context managers.
        mock_st.columns.return_value = [MagicMock(), MagicMock()]
        for col in mock_st.columns.return_value:
            col.__enter__.return_value = col
            col.__exit__.return_value = False
        _dialog_inner()()
    mock_st.code.assert_called_once()
    # Caption fires twice: the intro + the count line.
    assert mock_st.caption.call_count >= 2


def test_dialog_body_paused_path_shows_warning():
    """When the recorder is paused, the body emits an ``st.warning``."""
    script_recorder.clear_log()
    script_recorder.set_paused(True)
    with patch("iidm_viewer.session_script.st") as mock_st:
        mock_st.session_state = st.session_state
        mock_st.toggle.return_value = False
        mock_st.columns.return_value = [MagicMock(), MagicMock()]
        for col in mock_st.columns.return_value:
            col.__enter__.return_value = col
            col.__exit__.return_value = False
        _dialog_inner()()
    mock_st.warning.assert_called_once()


def test_dialog_body_clear_log_button_resets_recorder():
    """When the Clear log button reports ``True``, the recorder log is
    cleared + ``st.rerun`` fires."""
    script_recorder.record_load_network("grid.xiidm", {}, [])
    script_recorder.record_run_loadflow({}, {})
    assert len(script_recorder.get_log()) == 2

    with patch("iidm_viewer.session_script.st") as mock_st:
        mock_st.session_state = st.session_state
        mock_st.toggle.return_value = True
        # Clear button → True (the user clicked); Download button → False.
        mock_st.columns.return_value = [MagicMock(), MagicMock()]
        for col in mock_st.columns.return_value:
            col.__enter__.return_value = col
            col.__exit__.return_value = False
        mock_st.button.return_value = True  # Clear-log click
        _dialog_inner()()
    assert script_recorder.get_log() == []
    mock_st.rerun.assert_called_once()


def test_dialog_body_emits_download_button_with_script_bytes():
    """The download button must receive UTF-8 script bytes + a
    deterministic ``.py`` filename."""
    script_recorder.clear_log()
    with patch("iidm_viewer.session_script.st") as mock_st:
        mock_st.session_state = st.session_state
        mock_st.toggle.return_value = True
        mock_st.columns.return_value = [MagicMock(), MagicMock()]
        for col in mock_st.columns.return_value:
            col.__enter__.return_value = col
            col.__exit__.return_value = False
        _dialog_inner()()
    mock_st.download_button.assert_called_once()
    kwargs = mock_st.download_button.call_args.kwargs
    assert isinstance(kwargs["data"], bytes)
    assert kwargs["file_name"].startswith("session_")
    assert kwargs["file_name"].endswith(".py")
    assert kwargs["mime"] == "text/x-python"
