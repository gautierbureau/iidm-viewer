"""Session Script dialog — preview + download the HMI-mirror script.

Opened from the sidebar's "View live Script" button. Reads the op log
written by ``script_recorder`` and renders the script produced by
``script_generator``. Provides a download button, a Recording
pause/resume toggle, a net-state vs full-transcript toggle, and a
clear-log button.
"""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from iidm_viewer import script_recorder
from iidm_viewer.script_generator import generate_script


def _on_recording_change() -> None:
    """Mirror the toggle widget's value into the recorder's pause flag.

    Streamlit's session_state for the widget is always up-to-date by
    the time on_change fires, so we just read it back inverted.
    """
    is_recording = bool(st.session_state.get(script_recorder.RECORDING_WIDGET_KEY, True))
    script_recorder.set_paused(not is_recording)


@st.dialog("Session Script", width="large")
def show_session_script_dialog() -> None:
    st.caption(
        "A runnable Python script that replays the operations you have "
        "performed in this session against any pypowsybl-loadable network."
    )

    ops = script_recorder.get_log()
    source_filename = script_recorder.get_source_filename()

    col_record, col_revert = st.columns([1, 1])
    with col_record:
        st.toggle(
            "Recording",
            value=not script_recorder.is_paused(),
            key=script_recorder.RECORDING_WIDGET_KEY,
            on_change=_on_recording_change,
            help=(
                "When off, new operations are not added to the log. The "
                "log itself is preserved. Loading a new network always "
                "re-enables recording."
            ),
        )
    with col_revert:
        include_reverted = st.toggle(
            "Include reverted edits",
            value=False,
            key="_session_script_include_reverted",
            help=(
                "Off: the script reproduces the net state — reverted edits "
                "are dropped. On: every recorded operation is emitted in "
                "order, including reverts (full transcript)."
            ),
        )

    if script_recorder.is_paused():
        st.warning(
            "Recording is paused — new operations will not be captured."
        )

    script = generate_script(
        ops,
        include_reverted=include_reverted,
        source_filename=source_filename,
    )

    visible_count = sum(1 for o in ops if include_reverted or not o.get("reverted"))
    total = len(ops)
    reverted = total - sum(1 for o in ops if not o.get("reverted"))
    src_blurb = f" — source: `{source_filename}`" if source_filename else ""
    rev_blurb = f" ({reverted} reverted)" if reverted else ""
    st.caption(f"{visible_count} of {total} operation(s) emitted{rev_blurb}{src_blurb}")

    st.code(script, language="python")

    ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    col_dl, col_clear = st.columns([1, 1])
    with col_dl:
        st.download_button(
            "Download script",
            data=script.encode("utf-8"),
            file_name=f"session_{ts_tag}.py",
            mime="text/x-python",
            key="_session_script_download",
            use_container_width=True,
        )
    with col_clear:
        if st.button(
            "Clear log",
            key="_session_script_clear",
            help="Drop every recorded operation. Cannot be undone.",
            use_container_width=True,
        ):
            script_recorder.clear_log()
            st.rerun()
