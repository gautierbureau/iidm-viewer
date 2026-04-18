import json
import re
import streamlit as st

_SEVERITY_ORDER = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "ERROR": 4}
_SEVERITY_ICON = {"TRACE": "🔍", "DEBUG": "🐛", "INFO": "ℹ️", "WARN": "⚠️", "ERROR": "🔴"}


def _interpolate(template: str, values: dict) -> str:
    def _sub(m):
        key = m.group(1)
        entry = values.get(key, {})
        return str(entry.get("value", m.group(0)))
    return re.sub(r"\$\{([^}]+)\}", _sub, template)


def _node_message(node: dict, dictionaries: dict) -> str:
    key = node.get("messageKey", "")
    template = dictionaries.get("default", {}).get(key, key)
    return _interpolate(template, node.get("values", {}))


def _node_severity(node: dict) -> str:
    sev = node.get("values", {}).get("reportSeverity", {})
    return sev.get("value", "INFO") if sev else "INFO"


def _subtree_max_severity_level(node: dict) -> int:
    level = _SEVERITY_ORDER.get(_node_severity(node), 2)
    for child in node.get("children", []):
        level = max(level, _subtree_max_severity_level(child))
    return level


def _render_node(node: dict, dictionaries: dict, min_level: int) -> None:
    children = node.get("children", [])
    message = _node_message(node, dictionaries)
    sev = _node_severity(node)
    sev_level = _SEVERITY_ORDER.get(sev, 2)
    icon = _SEVERITY_ICON.get(sev, "")

    if not children:
        if sev_level >= min_level:
            st.markdown(f"{icon} {message}")
        return

    subtree_max = _subtree_max_severity_level(node)
    if subtree_max < min_level and sev_level < min_level:
        return

    expanded = subtree_max >= _SEVERITY_ORDER.get("WARN", 3)
    label = f"{icon} {message}" if icon else message
    with st.expander(label, expanded=expanded):
        for child in children:
            _render_node(child, dictionaries, min_level)


@st.dialog("Load Flow Logs", width="large")
def show_lf_report_dialog() -> None:
    report_json = st.session_state.get("_lf_report_json")
    if not report_json:
        st.info("No load flow report available. Run a load flow first.")
        return

    try:
        data = json.loads(report_json)
    except Exception as exc:
        st.error(f"Failed to parse report: {exc}")
        return

    dictionaries = data.get("dictionaries", {})
    root = data.get("reportRoot", {})

    severity_options = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR"]
    selected = st.multiselect(
        "Minimum severity",
        options=severity_options,
        default=["INFO", "WARN", "ERROR"],
        key="_lf_report_severity_filter",
    )
    if not selected:
        st.warning("Select at least one severity level.")
        return

    min_level = min(_SEVERITY_ORDER.get(s, 2) for s in selected)

    st.divider()
    for child in root.get("children", [root]):
        _render_node(child, dictionaries, min_level)
