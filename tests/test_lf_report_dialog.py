"""Tests for lf_report_dialog helpers and dialog rendering."""
import json

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
