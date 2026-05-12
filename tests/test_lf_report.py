"""Tests for the framework-agnostic :mod:`iidm_viewer.lf_report` parser.

These run against a real ``LoadFlowResult.report_json`` from the IEEE14
demo via the powsybl worker, so the parser is exercised on the exact
shape the Streamlit / PySide6 / NiceGUI dialogs see.
"""
from __future__ import annotations

import json

import pytest

from iidm_viewer.lf_report import (
    SEVERITY_ICON,
    SEVERITY_LEVELS,
    SEVERITY_ORDER,
    interpolate,
    node_message,
    node_severity,
    parse_report_to_tree,
    subtree_max_severity_level,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
def test_severity_constants_align():
    assert SEVERITY_LEVELS == ["TRACE", "DEBUG", "INFO", "WARN", "ERROR"]
    assert SEVERITY_ORDER["TRACE"] < SEVERITY_ORDER["ERROR"]
    # Every level must carry an icon (used by the dialogs to prefix labels).
    for level in SEVERITY_LEVELS:
        assert level in SEVERITY_ICON


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_interpolate_substitutes_placeholders():
    template = "Hello ${who}, you have ${count} new messages"
    values = {
        "who": {"value": "Ada"},
        "count": {"value": 3},
    }
    assert interpolate(template, values) == "Hello Ada, you have 3 new messages"


def test_interpolate_leaves_unknown_placeholders_intact():
    """An unknown key should be left as-is so the reader can spot the
    referenced placeholder rather than seeing a confusing empty string."""
    assert interpolate("${a}-${b}", {"a": {"value": "X"}}) == "X-${b}"


def test_node_message_resolves_via_dictionaries():
    dictionaries = {"default": {"key.A": "warm: ${temp}"}}
    node = {
        "messageKey": "key.A",
        "values": {"temp": {"value": "42"}},
    }
    assert node_message(node, dictionaries) == "warm: 42"


def test_node_message_falls_back_to_key():
    """When the dictionary has no entry, the messageKey itself is returned."""
    assert node_message({"messageKey": "ghost"}, {"default": {}}) == "ghost"


def test_node_severity_default_is_info():
    assert node_severity({}) == "INFO"
    assert node_severity({"values": {}}) == "INFO"


def test_node_severity_reads_report_severity():
    node = {"values": {"reportSeverity": {"value": "WARN"}}}
    assert node_severity(node) == "WARN"


def test_subtree_max_severity_finds_worst_descendant():
    node = {
        "values": {"reportSeverity": {"value": "INFO"}},
        "children": [
            {"values": {"reportSeverity": {"value": "DEBUG"}}, "children": []},
            {
                "values": {"reportSeverity": {"value": "INFO"}},
                "children": [
                    {"values": {"reportSeverity": {"value": "ERROR"}}, "children": []},
                ],
            },
        ],
    }
    assert subtree_max_severity_level(node) == SEVERITY_ORDER["ERROR"]


# ---------------------------------------------------------------------------
# parse_report_to_tree — end-to-end against the IEEE14 LF report
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ieee14_report_json() -> str:
    """Run AC LF on the IEEE14 demo and return the resulting report JSON."""
    import pypowsybl.network as pn
    from iidm_viewer.loadflow import run_ac
    from iidm_viewer.powsybl_worker import NetworkProxy, run

    net = NetworkProxy(run(pn.create_ieee14))
    result = run_ac(net)
    assert result is not None
    return result.report_json


def test_parse_report_returns_a_list_of_nodes(ieee14_report_json):
    nodes = parse_report_to_tree(ieee14_report_json)
    assert isinstance(nodes, list)
    assert nodes, "expected at least one log entry"
    # Each node has the documented dict shape.
    for n in nodes:
        assert {"message", "severity", "severity_level", "icon", "expanded", "children"} <= set(n)


def test_parse_report_empty_string_returns_empty_list():
    assert parse_report_to_tree("") == []


def test_parse_report_invalid_json_raises_value_error():
    with pytest.raises(ValueError, match="parse"):
        parse_report_to_tree("{not json")


def test_parse_report_filters_by_severity(ieee14_report_json):
    """Tightening the filter to ERROR drops INFO/DEBUG entries — the
    output is a subset of the broader INFO-level walk."""
    info_nodes = parse_report_to_tree(ieee14_report_json, min_severity="INFO")
    error_nodes = parse_report_to_tree(ieee14_report_json, min_severity="ERROR")

    def _flatten(nodes):
        out = []
        for n in nodes:
            out.append(n["severity_level"])
            out.extend(_flatten(n["children"]))
        return out

    info_levels = _flatten(info_nodes)
    error_levels = _flatten(error_nodes)
    # Tighter filter never produces *more* nodes than the looser one.
    assert len(error_levels) <= len(info_levels)
    # Every retained leaf at ERROR-level must clear the threshold.
    error_threshold = SEVERITY_ORDER["ERROR"]
    for level in error_levels:
        assert level >= error_threshold or any(
            sub >= error_threshold for sub in error_levels
        )


def test_parse_report_expansion_heuristic_marks_warn_subtrees(ieee14_report_json):
    """Subtrees containing a WARN/ERROR should open by default — same UX
    rule used by all three dialogs."""
    nodes = parse_report_to_tree(ieee14_report_json, min_severity="INFO")

    def _walk(ns):
        for n in ns:
            if n["children"]:
                # subtree_max_severity_level rule mirrored in the parser:
                # ``expanded`` is True when any descendant is >= WARN.
                expected = subtree_max_severity_level({
                    "values": {"reportSeverity": {"value": n["severity"]}},
                    "children": [
                        {"values": {"reportSeverity": {"value": c["severity"]}}, "children": []}
                        for c in n["children"]
                    ],
                }) >= SEVERITY_ORDER["WARN"]
                # We can't reconstruct the full original tree from the
                # flattened output, but we can at least sanity-check:
                # if a child carries WARN/ERROR, ``expanded`` must be True.
                if any(c["severity_level"] >= SEVERITY_ORDER["WARN"] for c in n["children"]):
                    assert n["expanded"], n
            _walk(n["children"])

    _walk(nodes)


# ---------------------------------------------------------------------------
# Streamlit dialog drift guard
# ---------------------------------------------------------------------------
def test_streamlit_dialog_uses_the_shared_parser():
    """``iidm_viewer.lf_report_dialog`` must funnel everything through
    :func:`parse_report_to_tree` so the parsing stays in one place."""
    pytest.importorskip("streamlit")
    import inspect
    from iidm_viewer import lf_report_dialog

    src = inspect.getsource(lf_report_dialog)
    assert "parse_report_to_tree" in src
    # The Streamlit module should NOT redefine the constants — they live
    # in the shared module.
    assert "SEVERITY_ORDER =" not in src
    assert "SEVERITY_ICON =" not in src
