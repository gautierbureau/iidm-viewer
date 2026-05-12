"""Streamlit "Load Flow Logs" dialog.

The actual parsing — message-template interpolation, severity filter,
"expand subtrees containing WARN/ERROR" heuristic — lives in the
framework-agnostic :mod:`iidm_viewer.lf_report` module so the PySide6
and NiceGUI prototypes share it. This file holds only the Streamlit
rendering glue.
"""
from __future__ import annotations

import streamlit as st

from iidm_viewer.lf_report import (
    SEVERITY_LEVELS,
    SEVERITY_ORDER,
    parse_report_to_tree,
)


def _render_node(node: dict) -> None:
    """Walk one branch of the tree returned by ``parse_report_to_tree``."""
    icon = node["icon"]
    message = node["message"]
    children = node["children"]

    if not children:
        st.markdown(f"{icon} {message}" if icon else message)
        return

    label = f"{icon} {message}" if icon else message
    with st.expander(label, expanded=node["expanded"]):
        for child in children:
            _render_node(child)


@st.dialog("Load Flow Logs", width="large")
def show_lf_report_dialog() -> None:
    report_json = st.session_state.get("_lf_report_json")
    if not report_json:
        st.info("No load flow report available. Run a load flow first.")
        return

    selected = st.multiselect(
        "Minimum severity",
        options=SEVERITY_LEVELS,
        default=["INFO", "WARN", "ERROR"],
        key="_lf_report_severity_filter",
    )
    if not selected:
        st.warning("Select at least one severity level.")
        return

    min_severity = min(selected, key=lambda s: SEVERITY_ORDER.get(s, 2))

    try:
        nodes = parse_report_to_tree(report_json, min_severity=min_severity)
    except ValueError as exc:
        st.error(str(exc))
        return

    st.divider()
    if not nodes:
        st.info("No log entries match the selected severity filter.")
        return
    for node in nodes:
        _render_node(node)
