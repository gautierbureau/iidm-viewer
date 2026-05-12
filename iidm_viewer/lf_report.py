"""Framework-agnostic parser for the pypowsybl LoadFlow ``report_json``.

``LoadFlowResult.report_json`` is a JSON document with a ``reportRoot``
tree of nodes and a ``dictionaries`` map of message templates. Each
node carries a ``messageKey`` whose template is interpolated against
``values`` to produce the human-readable message — and an optional
``reportSeverity`` value that gates which lines a UI should surface.

This module exposes:

* :data:`SEVERITY_LEVELS` / :data:`SEVERITY_ORDER` / :data:`SEVERITY_ICON`
  for use by the rendering layer.
* :func:`interpolate`, :func:`node_message`, :func:`node_severity`,
  :func:`subtree_max_severity_level` — the four pure helpers the
  Streamlit dialog used inline.
* :func:`parse_report_to_tree` — the high-level entry point: takes
  the raw JSON string + a minimum severity level and returns a list
  of plain-dict tree nodes (``{message, severity, severity_level, icon,
  expanded, children}``) that the Streamlit / PySide6 / NiceGUI
  dialogs walk to build their tree widget.

The shared parser does no IO and no UI work; tests below exercise it
on a real ``run_ac()`` report from the IEEE14 demo.
"""
from __future__ import annotations

import json
import re
from typing import Any

SEVERITY_LEVELS: list[str] = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR"]
SEVERITY_ORDER: dict[str, int] = {s: i for i, s in enumerate(SEVERITY_LEVELS)}
SEVERITY_ICON: dict[str, str] = {
    "TRACE": "🔍",
    "DEBUG": "🐛",
    "INFO": "ℹ️",
    "WARN": "⚠️",
    "ERROR": "🔴",
}
DEFAULT_MIN_SEVERITY: str = "INFO"


def interpolate(template: str, values: dict) -> str:
    """Substitute ``${key}`` placeholders in ``template`` using ``values``.

    ``values`` is the report node's ``values`` map: each entry's
    ``value`` is the replacement. Unknown keys are left intact so the
    user can still tell that a key was referenced.
    """
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        entry = values.get(key, {})
        return str(entry.get("value", m.group(0)))

    return re.sub(r"\$\{([^}]+)\}", _sub, template)


def node_message(node: dict, dictionaries: dict) -> str:
    """Resolve ``node.messageKey`` against ``dictionaries['default']``
    and interpolate the result. Falls back to the raw key when no
    template is defined for it."""
    key = node.get("messageKey", "")
    template = dictionaries.get("default", {}).get(key, key)
    return interpolate(template, node.get("values", {}))


def node_severity(node: dict) -> str:
    """Pull the ``reportSeverity`` value off a node; default ``INFO``."""
    sev = node.get("values", {}).get("reportSeverity", {})
    return sev.get("value", "INFO") if sev else "INFO"


def subtree_max_severity_level(node: dict) -> int:
    """Highest :data:`SEVERITY_ORDER` value across ``node`` + descendants.

    Used to decide whether to expand a parent when filtering by severity:
    if the subtree contains a WARN/ERROR, the parent should open.
    """
    level = SEVERITY_ORDER.get(node_severity(node), SEVERITY_ORDER["INFO"])
    for child in node.get("children", []):
        level = max(level, subtree_max_severity_level(child))
    return level


def _build_subtree(
    node: dict, dictionaries: dict, min_level: int,
) -> dict | None:
    """Recursively translate a raw report node into the shared tree
    shape. Returns ``None`` when neither the node nor its subtree
    reaches ``min_level``."""
    sev = node_severity(node)
    sev_level = SEVERITY_ORDER.get(sev, SEVERITY_ORDER["INFO"])
    message = node_message(node, dictionaries)
    icon = SEVERITY_ICON.get(sev, "")
    children_raw = node.get("children", [])

    if not children_raw:
        if sev_level < min_level:
            return None
        return {
            "message": message,
            "severity": sev,
            "severity_level": sev_level,
            "icon": icon,
            "expanded": False,
            "children": [],
        }

    subtree_max = subtree_max_severity_level(node)
    if subtree_max < min_level and sev_level < min_level:
        return None

    children: list[dict] = []
    for child in children_raw:
        sub = _build_subtree(child, dictionaries, min_level)
        if sub is not None:
            children.append(sub)

    # Expand parents that contain WARN/ERROR — same UX rule as the
    # Streamlit dialog. Lone leaves are always "expanded" because
    # there's nothing to fold.
    expanded = subtree_max >= SEVERITY_ORDER["WARN"]
    return {
        "message": message,
        "severity": sev,
        "severity_level": sev_level,
        "icon": icon,
        "expanded": expanded,
        "children": children,
    }


def parse_report_to_tree(
    report_json: str, min_severity: str = DEFAULT_MIN_SEVERITY,
) -> list[dict]:
    """Translate the raw report JSON into a list of tree nodes.

    Each node is a plain dict — no NiceGUI / PySide6 / Streamlit types
    inside — so the renderer is free to map it to whatever widget the
    framework provides. ``min_severity`` filters out subtrees whose
    severity (and that of every descendant) is strictly below it.

    Raises :class:`ValueError` on a malformed payload so callers can
    surface a clean error in the dialog.
    """
    if not report_json:
        return []
    try:
        data = json.loads(report_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse report JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Report JSON top-level must be a dict")

    dictionaries = data.get("dictionaries", {}) or {}
    root: Any = data.get("reportRoot") or data
    min_level = SEVERITY_ORDER.get(min_severity.upper(), SEVERITY_ORDER["INFO"])

    nodes: list[dict] = []
    # The top-level ``reportRoot`` is itself a node; if it has children
    # we walk them. Both shapes show up depending on pypowsybl version.
    children = root.get("children") if isinstance(root, dict) else None
    if children:
        for child in children:
            sub = _build_subtree(child, dictionaries, min_level)
            if sub is not None:
                nodes.append(sub)
    else:
        sub = _build_subtree(root, dictionaries, min_level)
        if sub is not None:
            nodes.append(sub)
    return nodes
