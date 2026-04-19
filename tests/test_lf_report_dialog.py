"""Tests for lf_report_dialog helpers and dialog rendering."""
import json
from unittest.mock import MagicMock, patch

import pytest

from iidm_viewer.lf_report_dialog import (
    _SEVERITY_ORDER,
    _interpolate,
    _node_message,
    _node_severity,
    _subtree_max_severity_level,
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
# _render_node
# ---------------------------------------------------------------------------

class TestRenderNode:
    """Tests for _render_node branching logic.

    All Streamlit widget calls are patched out — we only verify which
    rendering paths are taken (markdown vs expander, expanded flag).
    """

    def _node(self, sev=None, children=None, msg_key="k"):
        values = {}
        if sev is not None:
            values["reportSeverity"] = {"value": sev}
        n = {"messageKey": msg_key, "values": values}
        if children is not None:
            n["children"] = children
        return n

    def _expander_cm(self, mock_st):
        """Return a MagicMock that works as a context manager for st.expander."""
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=None)
        cm.__exit__ = MagicMock(return_value=False)
        mock_st.expander.return_value = cm
        return cm

    def test_leaf_at_or_above_min_level_renders_markdown(self):
        from iidm_viewer.lf_report_dialog import _render_node

        node = self._node("WARN")
        with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
            _render_node(node, {}, min_level=_SEVERITY_ORDER["INFO"])
            mock_st.markdown.assert_called_once()
            mock_st.expander.assert_not_called()

    def test_leaf_below_min_level_renders_nothing(self):
        from iidm_viewer.lf_report_dialog import _render_node

        node = self._node("TRACE")
        with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
            _render_node(node, {}, min_level=_SEVERITY_ORDER["ERROR"])
            mock_st.markdown.assert_not_called()

    def test_leaf_message_interpolated_via_dictionaries(self):
        from iidm_viewer.lf_report_dialog import _render_node

        node = {
            "messageKey": "greet",
            "values": {
                "name": {"value": "World"},
                "reportSeverity": {"value": "INFO"},
            },
        }
        dicts = {"default": {"greet": "Hello ${name}"}}
        with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
            _render_node(node, dicts, min_level=0)
            args = mock_st.markdown.call_args[0][0]
            assert "Hello World" in args

    def test_parent_skipped_when_entire_subtree_below_min_level(self):
        from iidm_viewer.lf_report_dialog import _render_node

        child = self._node("TRACE")
        node = self._node("TRACE", children=[child])
        with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
            _render_node(node, {}, min_level=_SEVERITY_ORDER["ERROR"])
            mock_st.expander.assert_not_called()
            mock_st.markdown.assert_not_called()

    def test_parent_with_warn_child_renders_expanded_expander(self):
        from iidm_viewer.lf_report_dialog import _render_node

        child = self._node("WARN")
        node = self._node("INFO", children=[child])
        with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
            self._expander_cm(mock_st)
            _render_node(node, {}, min_level=_SEVERITY_ORDER["INFO"])
            mock_st.expander.assert_called_once()
            _, kwargs = mock_st.expander.call_args
            assert kwargs.get("expanded") is True

    def test_parent_with_info_only_child_renders_collapsed_expander(self):
        from iidm_viewer.lf_report_dialog import _render_node

        child = self._node("INFO")
        node = self._node("INFO", children=[child])
        with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
            self._expander_cm(mock_st)
            _render_node(node, {}, min_level=_SEVERITY_ORDER["INFO"])
            mock_st.expander.assert_called_once()
            _, kwargs = mock_st.expander.call_args
            assert kwargs.get("expanded") is False

    def test_parent_icon_and_message_used_as_expander_label(self):
        from iidm_viewer.lf_report_dialog import _render_node, _SEVERITY_ICON

        child = self._node("INFO")
        node = self._node("INFO", children=[child], msg_key="net")
        dicts = {"default": {"net": "Network"}}
        with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
            self._expander_cm(mock_st)
            _render_node(node, dicts, min_level=0)
            label_arg = mock_st.expander.call_args[0][0]
            assert "Network" in label_arg
            assert _SEVERITY_ICON["INFO"] in label_arg

    def test_render_node_recurses_into_children(self):
        """Children of the expander are rendered via recursive calls."""
        from iidm_viewer.lf_report_dialog import _render_node

        leaf = self._node("INFO")
        node = self._node("INFO", children=[leaf])
        with patch("iidm_viewer.lf_report_dialog.st") as mock_st:
            self._expander_cm(mock_st)
            _render_node(node, {}, min_level=0)
            # The leaf child renders via st.markdown inside the expander
            mock_st.markdown.assert_called_once()


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
