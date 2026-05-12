"""PySide6 "Load Flow Logs" dialog.

Parsing — message-template interpolation, severity filter, and the
"expand subtrees containing WARN/ERROR" heuristic — lives in
:mod:`iidm_viewer.lf_report` so the Streamlit and NiceGUI prototypes
share it. This file is just the Qt rendering glue: a ``QDialog`` with
a severity multiselect + a ``QTreeWidget`` of the report nodes.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from iidm_viewer.lf_report import (
    SEVERITY_LEVELS,
    SEVERITY_ORDER,
    parse_report_to_tree,
)


_DEFAULT_SEVERITY = ("INFO", "WARN", "ERROR")


class LFReportDialog(QDialog):
    """Modal dialog showing the formatted ``LoadFlowResult.report_json``.

    The full report is parsed once on open via
    :func:`iidm_viewer.lf_report.parse_report_to_tree` and rebuilt on
    every severity-filter change. Nodes whose subtree contains a
    WARN/ERROR open by default — mirrors the Streamlit dialog UX.
    """

    def __init__(self, report_json: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Load Flow Logs")
        self.resize(720, 520)
        self._report_json = report_json or ""

        info = QLabel(
            "Filter by severity. Subtrees containing a WARN or ERROR "
            "open by default."
        )
        info.setWordWrap(True)

        # Severity multiselect — one checkbox per level.
        self._sev_checks: dict[str, QCheckBox] = {}
        sev_row = QHBoxLayout()
        sev_row.addWidget(QLabel("Show:"))
        for level in SEVERITY_LEVELS:
            box = QCheckBox(level)
            box.setChecked(level in _DEFAULT_SEVERITY)
            box.stateChanged.connect(self._rebuild_tree)
            self._sev_checks[level] = box
            sev_row.addWidget(box)
        sev_row.addStretch(1)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(False)
        self._tree.setWordWrap(True)

        self._empty_label = QLabel("")
        self._empty_label.setVisible(False)
        self._empty_label.setStyleSheet("color: #555; padding: 8px;")

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(sev_row)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._empty_label)
        layout.addLayout(btn_row)

        self._rebuild_tree()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _min_severity(self) -> Optional[str]:
        selected = [
            level for level, box in self._sev_checks.items()
            if box.isChecked()
        ]
        if not selected:
            return None
        return min(selected, key=lambda s: SEVERITY_ORDER.get(s, 2))

    def _rebuild_tree(self) -> None:
        self._tree.clear()
        if not self._report_json:
            self._show_empty("No load flow report available. Run a load flow first.")
            return
        min_severity = self._min_severity()
        if min_severity is None:
            self._show_empty("Select at least one severity level.")
            return
        try:
            nodes = parse_report_to_tree(self._report_json, min_severity=min_severity)
        except ValueError as exc:
            self._show_empty(f"Failed to parse report: {exc}")
            return
        if not nodes:
            self._show_empty("No log entries match the selected severity filter.")
            return
        self._empty_label.setVisible(False)
        self._tree.setVisible(True)
        for node in nodes:
            self._tree.addTopLevelItem(self._build_item(node))

    def _build_item(self, node: dict) -> QTreeWidgetItem:
        text = f"{node['icon']} {node['message']}" if node["icon"] else node["message"]
        item = QTreeWidgetItem([text])
        for child in node["children"]:
            item.addChild(self._build_item(child))
        if node["expanded"]:
            item.setExpanded(True)
        return item

    def _show_empty(self, text: str) -> None:
        self._tree.setVisible(False)
        self._empty_label.setText(text)
        self._empty_label.setVisible(True)
