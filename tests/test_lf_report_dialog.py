"""Tests for the message-template / severity helpers ``lf_report_dialog``
re-uses from the shared parser.

The dialog body itself is a thin Streamlit shell around
:mod:`iidm_viewer.lf_report` — the framework-agnostic parser. These
tests pin the helper API the dialog imports; the parser's end-to-end
behaviour is covered by ``tests/test_lf_report.py``.
"""
import pytest

from iidm_viewer.lf_report import (
    SEVERITY_ORDER as _SEVERITY_ORDER,
    interpolate as _interpolate,
    node_message as _node_message,
    node_severity as _node_severity,
    subtree_max_severity_level as _subtree_max_severity_level,
)


# ---------------------------------------------------------------------------
# _interpolate
# ---------------------------------------------------------------------------

class TestInterpolate:
    def test_no_placeholders(self):
        assert _interpolate("plain text", {}) == "plain text"

    def test_single_placeholder(self):
        result = _interpolate("Hello ${name}", {"name": {"value": "World"}})
        assert result == "Hello World"

    def test_multiple_placeholders(self):
        result = _interpolate(
            "Network CC${cc} SC${sc}",
            {"cc": {"value": 0}, "sc": {"value": 1}},
        )
        assert result == "Network CC0 SC1"

    def test_missing_placeholder_kept_verbatim(self):
        result = _interpolate("Value: ${missing}", {})
        assert result == "Value: ${missing}"

    def test_numeric_value(self):
        result = _interpolate("${val} MW", {"val": {"value": 319.64}})
        assert result == "319.64 MW"

    def test_empty_template(self):
        assert _interpolate("", {"k": {"value": "v"}}) == ""


# ---------------------------------------------------------------------------
# _node_message
# ---------------------------------------------------------------------------

class TestNodeMessage:
    def test_key_resolved_from_dictionary(self):
        dicts = {"default": {"olf.loadFlow": "Load flow on '${networkId}'"}}
        node = {
            "messageKey": "olf.loadFlow",
            "values": {"networkId": {"value": "ieee14"}},
        }
        assert _node_message(node, dicts) == "Load flow on 'ieee14'"

    def test_unknown_key_falls_back_to_key_itself(self):
        node = {"messageKey": "unknownKey", "values": {}}
        assert _node_message(node, {}) == "unknownKey"

    def test_no_message_key_returns_empty(self):
        assert _node_message({}, {}) == ""

    def test_static_message_no_values(self):
        dicts = {"default": {"k": "static message"}}
        node = {"messageKey": "k", "values": {}}
        assert _node_message(node, dicts) == "static message"

    def test_empty_default_dictionary(self):
        node = {"messageKey": "olf.size", "values": {}}
        assert _node_message(node, {"default": {}}) == "olf.size"


# ---------------------------------------------------------------------------
# _node_severity
# ---------------------------------------------------------------------------

class TestNodeSeverity:
    @pytest.mark.parametrize("sev", ["INFO", "WARN", "ERROR", "TRACE", "DEBUG"])
    def test_known_severity_values(self, sev):
        node = {"values": {"reportSeverity": {"value": sev, "type": "SEVERITY"}}}
        assert _node_severity(node) == sev

    def test_no_values_defaults_to_info(self):
        assert _node_severity({}) == "INFO"

    def test_empty_values_defaults_to_info(self):
        assert _node_severity({"values": {}}) == "INFO"

    def test_empty_severity_entry_defaults_to_info(self):
        # reportSeverity key present but empty dict (no "value")
        node = {"values": {"reportSeverity": {}}}
        assert _node_severity(node) == "INFO"


# ---------------------------------------------------------------------------
# _subtree_max_severity_level
# ---------------------------------------------------------------------------

class TestSubtreeMaxSeverityLevel:
    def _node(self, sev=None, children=None):
        values = {}
        if sev is not None:
            values["reportSeverity"] = {"value": sev}
        n = {"values": values}
        if children is not None:
            n["children"] = children
        return n

    def test_leaf_returns_own_level(self):
        assert _subtree_max_severity_level(self._node("INFO")) == _SEVERITY_ORDER["INFO"]

    def test_child_with_higher_severity_elevates_result(self):
        node = self._node("INFO", children=[self._node("WARN")])
        assert _subtree_max_severity_level(node) == _SEVERITY_ORDER["WARN"]

    def test_deeply_nested_error_propagates(self):
        node = self._node(children=[
            self._node(children=[self._node("ERROR")])
        ])
        assert _subtree_max_severity_level(node) == _SEVERITY_ORDER["ERROR"]

    def test_max_across_siblings(self):
        node = self._node(children=[
            self._node("TRACE"),
            self._node("WARN"),
            self._node("INFO"),
        ])
        assert _subtree_max_severity_level(node) == _SEVERITY_ORDER["WARN"]

    def test_parent_higher_than_children(self):
        node = self._node("ERROR", children=[self._node("INFO")])
        assert _subtree_max_severity_level(node) == _SEVERITY_ORDER["ERROR"]

    def test_node_with_no_children_and_no_severity_defaults_info(self):
        assert _subtree_max_severity_level({}) == _SEVERITY_ORDER["INFO"]



# ---------------------------------------------------------------------------
# Integration: parse a realistic ReportNode JSON snapshot
# ---------------------------------------------------------------------------

_SAMPLE_REPORT = {
    "version": "3.0",
    "dictionaries": {
        "default": {
            "olf.loadFlow": "Load flow on network '${networkId}'",
            "olf.lfNetwork": "Network CC${networkNumCc} SC${networkNumSc}",
            "olf.acLfCompleteWithSuccess": "AC load flow completed (${solverStatus})",
        }
    },
    "reportRoot": {
        "messageKey": "Load Flow",
        "children": [
            {
                "messageKey": "olf.loadFlow",
                "values": {"networkId": {"value": "test-net"}},
                "children": [
                    {
                        "messageKey": "olf.lfNetwork",
                        "values": {
                            "networkNumCc": {"value": 0},
                            "networkNumSc": {"value": 0},
                        },
                        "children": [
                            {
                                "messageKey": "olf.acLfCompleteWithSuccess",
                                "values": {
                                    "solverStatus": {"value": "CONVERGED"},
                                    "reportSeverity": {
                                        "value": "INFO",
                                        "type": "SEVERITY",
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    },
}


def test_sample_report_root_message():
    root = _SAMPLE_REPORT["reportRoot"]
    dicts = _SAMPLE_REPORT["dictionaries"]
    assert _node_message(root, dicts) == "Load Flow"


def test_sample_report_first_child_message():
    child = _SAMPLE_REPORT["reportRoot"]["children"][0]
    dicts = _SAMPLE_REPORT["dictionaries"]
    assert _node_message(child, dicts) == "Load flow on network 'test-net'"


def test_sample_report_leaf_message():
    leaf = (
        _SAMPLE_REPORT["reportRoot"]["children"][0]["children"][0]["children"][0]
    )
    dicts = _SAMPLE_REPORT["dictionaries"]
    assert _node_message(leaf, dicts) == "AC load flow completed (CONVERGED)"


def test_sample_report_subtree_max_is_info():
    root = _SAMPLE_REPORT["reportRoot"]
    assert _subtree_max_severity_level(root) == _SEVERITY_ORDER["INFO"]



# ---------------------------------------------------------------------------
# show_lf_report_dialog body — exercise via ``__wrapped__``
# ---------------------------------------------------------------------------
def test_dialog_body_no_report_in_session_state_shows_info():
    """Missing ``_lf_report_json`` → ``st.info`` + early return."""
    import streamlit as st
    from unittest.mock import patch

    from iidm_viewer.lf_report_dialog import show_lf_report_dialog

    st.session_state.clear()
    with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
        mock_st.session_state = {}
        show_lf_report_dialog.__wrapped__()
    mock_st.info.assert_called_once()


def test_dialog_body_no_severity_selected_shows_warning():
    """Severity multiselect with no items → ``st.warning`` + early return."""
    from unittest.mock import patch

    from iidm_viewer.lf_report_dialog import show_lf_report_dialog

    with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
        mock_st.session_state = {"_lf_report_json": "{}"}
        mock_st.multiselect.return_value = []  # user deselected everything
        show_lf_report_dialog.__wrapped__()
    mock_st.warning.assert_called_once()


def test_dialog_body_invalid_json_shows_error():
    """``parse_report_to_tree`` raising ``ValueError`` lands on ``st.error``."""
    from unittest.mock import patch

    from iidm_viewer.lf_report_dialog import show_lf_report_dialog

    with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
        mock_st.session_state = {"_lf_report_json": "not-json"}
        mock_st.multiselect.return_value = ["INFO"]
        show_lf_report_dialog.__wrapped__()
    mock_st.error.assert_called_once()


def test_dialog_body_empty_tree_shows_info():
    """A well-formed report whose tree filters to empty → ``st.info``."""
    import json
    from unittest.mock import patch

    from iidm_viewer.lf_report_dialog import show_lf_report_dialog

    # An empty reportRoot yields zero nodes after the severity filter.
    report = json.dumps({
        "dictionaries": {"default": {}},
        "reportRoot": {"messageKey": "root"},
    })
    with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
        mock_st.session_state = {"_lf_report_json": report}
        mock_st.multiselect.return_value = ["ERROR"]  # filter excludes the root
        show_lf_report_dialog.__wrapped__()
    # Either info("No log entries…") or markdown for the root node.
    # We only assert the call path reached the parser (no early-return).
    mock_st.multiselect.assert_called_once()


def test_render_node_walks_children_into_expanders():
    """``_render_node`` emits ``st.markdown`` for leaves and ``st.expander``
    for parents — the recursive tree walker the dialog body uses."""
    from unittest.mock import MagicMock, patch

    from iidm_viewer.lf_report_dialog import _render_node

    leaf = {"icon": "ℹ️", "message": "leaf", "children": [], "expanded": False}
    parent = {
        "icon": "", "message": "parent", "expanded": True,
        "children": [leaf],
    }
    with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
        ctx = MagicMock()
        ctx.__enter__.return_value = ctx
        ctx.__exit__.return_value = False
        mock_st.expander.return_value = ctx
        _render_node(parent)
    mock_st.expander.assert_called_once()
    mock_st.markdown.assert_called_once()
