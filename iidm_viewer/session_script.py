"""Session Script tab — preview + download the HMI-mirror script.

Reads the op log written by ``script_recorder`` and renders the script
produced by ``script_generator``. Provides a download button and a
toggle to switch between net-state and full-transcript output.
"""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from iidm_viewer import script_recorder
from iidm_viewer.script_generator import generate_script


def render_session_script_tab() -> None:
    st.subheader("Session Script")
    st.caption(
        "A runnable Python script that replays the operations you have "
        "performed in this session against any pypowsybl-loadable network."
    )

    ops = script_recorder.get_log()
    source_filename = script_recorder.get_source_filename()

    include_reverted = st.toggle(
        "Include reverted edits",
        value=False,
        key="_session_script_include_reverted",
        help=(
            "Off: the script reproduces the net state — reverted edits are "
            "dropped. On: every recorded operation is emitted in order, "
            "including reverts (full transcript)."
        ),
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
            help="Drop every recorded operation. This cannot be undone.",
            use_container_width=True,
        ):
            _confirm_clear_dialog()


@st.dialog("Clear session script log?")
def _confirm_clear_dialog() -> None:
    st.write(
        "This drops every recorded operation. The session script will be "
        "empty until you load a network or run a new operation."
    )
    col_cancel, col_ok = st.columns(2)
    if col_cancel.button("Cancel", key="_session_script_clear_cancel", use_container_width=True):
        st.rerun()
    if col_ok.button(
        "Clear", key="_session_script_clear_ok", type="primary", use_container_width=True
    ):
        script_recorder.clear_log()
        st.rerun()
