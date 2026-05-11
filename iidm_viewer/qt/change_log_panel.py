"""Change-log panel for the PySide6 Data Explorer.

A compact ``QTableView`` over an :class:`iidm_viewer.change_log.ChangeLog`
instance, plus a "Revert all" button. Each row carries an inline
Revert button.

The panel listens to the ChangeLog's ``on_changed`` bus so it
repaints whenever the data explorer (or any other host) records or
reverts an entry — no manual refresh needed.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.change_log import ChangeLog
from iidm_viewer.powsybl_worker import NetworkProxy


_COLUMNS = ("Component", "Element", "Property", "Before", "After")


class _ChangeLogModel(QAbstractTableModel):
    """Read-only Qt model over a ChangeLog's entries list."""

    def __init__(self, change_log: ChangeLog, parent=None) -> None:
        super().__init__(parent)
        self._log = change_log

    def refresh(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._log)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        entries = self._log.entries()
        if index.row() >= len(entries):
            return None
        entry = entries[index.row()]
        col = index.column()
        if col == 0:
            return str(entry.get("component", ""))
        if col == 1:
            return str(entry.get("element_id", ""))
        if col == 2:
            return str(entry.get("property", ""))
        if col == 3:
            return _format_value(entry.get("before"))
        if col == 4:
            return _format_value(entry.get("after"))
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            try:
                return _COLUMNS[section]
            except IndexError:
                return None
        return str(section + 1)

    def entry_at(self, row: int):
        entries = self._log.entries()
        return entries[row] if 0 <= row < len(entries) else None


def _format_value(value) -> str:
    import math
    if value is None:
        return "—"
    if isinstance(value, float):
        if math.isnan(value):
            return "—"
        return format(value, ".4g")
    return str(value)


class ChangeLogPanel(QWidget):
    """Always-visible "Change Log" section under the data explorer."""

    # Emitted after one or more entries are reverted via the panel.
    # Carries the list of (component, attribute) tuples that were
    # touched so listeners can refresh their views surgically.
    reverted = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._log: Optional[ChangeLog] = None
        self._model = _ChangeLogModel(ChangeLog(), self)

        self._title = QLabel("Change Log (0)")
        self._title.setStyleSheet("font-weight: bold; padding: 4px 10px;")
        self._revert_all = QPushButton("Revert all")
        self._revert_all.clicked.connect(self._on_revert_all)
        self._revert_selected = QPushButton("Revert selected")
        self._revert_selected.clicked.connect(self._on_revert_selected)
        self._clear = QPushButton("Clear")
        self._clear.clicked.connect(self._on_clear)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 10, 0)
        header.addWidget(self._title, 1)
        header.addWidget(self._revert_selected)
        header.addWidget(self._revert_all)
        header.addWidget(self._clear)

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setDefaultSectionSize(22)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMaximumHeight(180)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(2)
        layout.addLayout(header)
        layout.addWidget(self._table)

        self._refresh_title()
        self._update_buttons()

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._update_buttons()

    def set_change_log(self, change_log: ChangeLog) -> None:
        self._log = change_log
        self._model = _ChangeLogModel(change_log, self)
        self._table.setModel(self._model)
        change_log.on_changed(self._on_log_changed)
        self._on_log_changed()

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------
    def _on_log_changed(self) -> None:
        self._model.refresh()
        self._refresh_title()
        self._update_buttons()

    def _refresh_title(self) -> None:
        n = len(self._log) if self._log is not None else 0
        self._title.setText(f"Change Log ({n})")

    def _update_buttons(self) -> None:
        has_entries = self._log is not None and len(self._log) > 0
        has_network = self._network is not None
        self._revert_all.setEnabled(has_entries and has_network)
        self._revert_selected.setEnabled(has_entries and has_network)
        self._clear.setEnabled(has_entries)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _selected_entries(self):
        if self._log is None:
            return []
        sel = self._table.selectionModel()
        if sel is None:
            return []
        out = []
        for proxy_idx in sel.selectedRows():
            entry = self._model.entry_at(proxy_idx.row())
            if entry is not None:
                out.append(entry)
        return out

    def _on_revert_selected(self) -> None:
        if self._log is None or self._network is None:
            return
        entries = self._selected_entries()
        if not entries:
            return
        touched: list[tuple[str, str]] = []
        skipped: list[str] = []
        for entry in entries:
            try:
                self._log.revert(self._network, entry)
            except Exception as exc:
                skipped.append(
                    f"{entry.get('component', '')}/{entry.get('element_id', '')}/"
                    f"{entry.get('property', '')}: {exc}"
                )
                continue
            touched.append((str(entry.get("component", "")), str(entry.get("property", ""))))
        if skipped:
            QMessageBox.warning(
                self,
                "Some entries could not be reverted",
                "\n".join(skipped[:8]) + (f"\n…(+{len(skipped) - 8} more)" if len(skipped) > 8 else ""),
            )
        if touched:
            self.reverted.emit(touched)

    def _on_revert_all(self) -> None:
        if self._log is None or self._network is None:
            return
        # Capture the affected (component, attribute) pairs *before* the
        # entries get popped, so listeners can refresh the right tab.
        targets = [(str(e.get("component", "")), str(e.get("property", "")))
                   for e in self._log.entries()]
        reverted, skipped = self._log.revert_all(self._network)
        if skipped:
            n = len(skipped)
            QMessageBox.warning(
                self,
                "Revert all — partial",
                f"Reverted {reverted} entry/entries; {n} skipped "
                f"(original value unavailable).",
            )
        if reverted and targets:
            self.reverted.emit(targets[:reverted])

    def _on_clear(self) -> None:
        if self._log is None or len(self._log) == 0:
            return
        confirm = QMessageBox.question(
            self,
            "Clear change log?",
            "Discard the log entries without reverting? The applied "
            "network changes will stay in place.",
        )
        if confirm == QMessageBox.Yes:
            self._log.clear()
