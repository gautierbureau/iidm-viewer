"""PySide6 "Data Explorer Extensions" tab.

Mirrors Streamlit's ``render_extensions_explorer``:

* An extension picker (every pypowsybl extension name).
* An ID-substring filter on the table's first column.
* The extension's DataFrame rendered in a ``QTableWidget`` — editable
  columns come from the shared :data:`EDITABLE_EXTENSIONS` map. Rows
  carry a Remove checkbox column.
* Apply / Remove buttons that route through the shared worker-routed
  wrappers in :mod:`iidm_viewer.extensions_data`.

The framework-agnostic listing + mutation helpers live in the shared
module so Streamlit, PySide6 and NiceGUI run identical pypowsybl
calls.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.extensions_data import (
    ExtensionsExplorerViewModel,
    get_extension_df,
    get_extensions_information,
    list_extension_names,
    remove_extension,
    update_extension,
)
from iidm_viewer.powsybl_worker import NetworkProxy


class ExtensionsExplorerTab(QWidget):
    """Editable / removable view of pypowsybl extensions on the active network."""

    extensions_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._vm = ExtensionsExplorerViewModel()
        # Map column index → column name for the current extension's table
        # (so cell edits can resolve back to a column name without index
        # arithmetic spread across the file).
        self._col_names: list[str] = []

        # Top row — picker + filter + counts.
        self._ext_combo = QComboBox()
        self._ext_combo.setMinimumWidth(240)
        self._ext_combo.currentTextChanged.connect(self._on_extension_changed)
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter by ID (substring)")
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        self._summary_lbl = QLabel("")
        self._summary_lbl.setStyleSheet("color: #555;")

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Extension:"))
        top_row.addWidget(self._ext_combo)
        top_row.addSpacing(8)
        top_row.addWidget(self._filter_edit, 1)
        top_row.addWidget(self._summary_lbl)

        # Description caption (filled from pypowsybl's
        # ``get_extensions_information``).
        self._detail_lbl = QLabel("")
        self._detail_lbl.setWordWrap(True)
        self._detail_lbl.setStyleSheet("color: #555; font-style: italic;")

        # Table.
        self._table = QTableWidget()
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._table.itemChanged.connect(self._on_item_changed)

        # Action row.
        self._apply_btn = QPushButton("Apply changes")
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        self._remove_btn = QPushButton("Remove ticked rows")
        self._remove_btn.clicked.connect(self._on_remove_clicked)
        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)

        action_row = QHBoxLayout()
        action_row.addWidget(self._apply_btn)
        action_row.addWidget(self._remove_btn)
        action_row.addWidget(self._status_lbl, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(top_row)
        layout.addWidget(self._detail_lbl)
        layout.addWidget(self._table, 1)
        layout.addLayout(action_row)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._vm.clear()
        if network is None:
            self._populate_ext_combo([])
            self._set_table_empty("No network loaded.")
            return
        try:
            names = list_extension_names()
        except Exception:
            names = []
        self._populate_ext_combo(names)
        try:
            self._vm.set_info(get_extensions_information())
        except Exception:
            self._vm.set_info(pd.DataFrame())
        self._refresh_table()

    # ------------------------------------------------------------------
    # Internals — combo + filter
    # ------------------------------------------------------------------
    def _populate_ext_combo(self, names: list[str]) -> None:
        self._ext_combo.blockSignals(True)
        self._ext_combo.clear()
        for n in names:
            self._ext_combo.addItem(n)
        self._ext_combo.blockSignals(False)

    def _current_extension(self) -> str:
        return self._ext_combo.currentText() or ""

    def _on_extension_changed(self, _txt: str) -> None:
        # Drop pending edits / removals tied to the previously-shown
        # extension — they don't apply to the new one anyway.
        self._vm.reset_pending()
        self._refresh_table()

    def _on_filter_changed(self, _txt: str) -> None:
        self._refresh_table(preserve_pending=True)

    # ------------------------------------------------------------------
    # Internals — table render
    # ------------------------------------------------------------------
    def _set_table_empty(self, message: str) -> None:
        self._table.blockSignals(True)
        self._table.clear()
        self._table.setRowCount(0)
        self._table.setColumnCount(0)
        self._table.blockSignals(False)
        self._summary_lbl.setText(message)
        self._detail_lbl.setText("")
        self._apply_btn.setEnabled(False)
        self._remove_btn.setEnabled(False)

    def _refresh_table(self, preserve_pending: bool = False) -> None:
        if not preserve_pending:
            self._status_lbl.setText("")
            self._status_lbl.setStyleSheet("")
        ext = self._current_extension()
        if not ext or self._network is None:
            self._set_table_empty("Pick an extension.")
            return
        try:
            df = get_extension_df(self._network, ext)
        except Exception as exc:
            self._set_table_empty(f"Failed to load {ext!r}: {exc}")
            return
        self._vm.set_data(ext, df if df is not None else pd.DataFrame())
        self._detail_lbl.setText(self._vm.detail())
        if self._vm.current_df.empty:
            self._set_table_empty(f"No {ext!r} extensions found.")
            return
        total = len(self._vm.current_df)
        view = self._vm.filtered_view(self._filter_edit.text() or "")
        if view.empty:
            self._set_table_empty(
                f"No {ext!r} extensions match the filter.",
            )
            return
        editable_cols = self._vm.editable_cols(view)
        self._col_names = ["id"] + list(view.columns)
        readonly = self._vm.is_readonly()
        # Layout: leading "Remove" checkbox column + "id" column +
        # one column per DataFrame column.
        n_cols = 2 + len(view.columns)
        # The "id" + extra columns; the leading Remove column is added
        # conditionally for non-read-only extensions.
        self._table.blockSignals(True)
        self._table.clear()
        if readonly:
            self._table.setColumnCount(1 + len(view.columns))
            self._table.setHorizontalHeaderLabels(["id"] + list(view.columns))
            offset = 1
        else:
            self._table.setColumnCount(n_cols)
            self._table.setHorizontalHeaderLabels(
                ["Remove", "id"] + list(view.columns),
            )
            offset = 2
        self._table.setRowCount(view.shape[0])
        for r, (idx, row) in enumerate(view.iterrows()):
            element_id = str(idx)
            # Remove checkbox column (skipped on read-only extensions).
            if not readonly:
                cb_item = QTableWidgetItem()
                cb_item.setFlags(
                    Qt.ItemIsEnabled | Qt.ItemIsUserCheckable,
                )
                cb_item.setCheckState(
                    Qt.Checked if self._vm.is_ticked(element_id)
                    else Qt.Unchecked,
                )
                self._table.setItem(r, 0, cb_item)
            # ID column (read-only).
            id_item = QTableWidgetItem(element_id)
            id_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self._table.setItem(r, offset - 1, id_item)
            # Data columns.
            for c, col in enumerate(view.columns):
                value = row[col]
                pending = self._vm.get_edit(element_id, col)
                cell_value = pending if pending is not None else value
                display = self._format_cell(cell_value)
                cell_item = QTableWidgetItem(display)
                if col in editable_cols and not readonly:
                    cell_item.setFlags(
                        Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable,
                    )
                else:
                    cell_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self._table.setItem(r, offset + c, cell_item)
        self._table.blockSignals(False)
        if len(view) == total:
            self._summary_lbl.setText(
                f"{total} {ext!r} extension{'s' if total != 1 else ''}",
            )
        else:
            self._summary_lbl.setText(
                f"{len(view)} of {total} {ext!r} extension(s)",
            )
        self._apply_btn.setEnabled(bool(editable_cols) and not readonly)
        self._remove_btn.setEnabled(not readonly)

    @staticmethod
    def _format_cell(value) -> str:
        if value is None:
            return ""
        try:
            import math
            if isinstance(value, float) and math.isnan(value):
                return ""
        except Exception:
            pass
        if isinstance(value, float):
            return format(value, ".4g")
        return str(value)

    # ------------------------------------------------------------------
    # Internals — edit + apply + remove
    # ------------------------------------------------------------------
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        ext = self._current_extension()
        if not ext:
            return
        readonly = self._vm.is_readonly()
        col = item.column()
        row = item.row()
        # Remove-checkbox column.
        if not readonly and col == 0:
            id_item = self._table.item(row, 1)
            if id_item is None:
                return
            self._vm.tick_remove(
                id_item.text(), item.checkState() == Qt.Checked,
            )
            return
        offset = 1 if readonly else 2
        if col < offset:
            return
        col_idx = col - offset
        if col_idx >= len(self._vm.current_df.columns):
            return
        col_name = str(self._vm.current_df.columns[col_idx])
        if col_name not in self._vm.editable_cols():
            return
        id_item = self._table.item(row, offset - 1)
        if id_item is None:
            return
        element_id = id_item.text()
        parsed = self._parse_for_column(col_name, item.text())
        self._vm.add_edit(element_id, col_name, parsed)

    def _parse_for_column(self, col_name: str, text: str):
        """Best-effort cast for the user's input — pypowsybl wants the
        column's native dtype."""
        df = self._vm.current_df
        if col_name not in df.columns:
            return text
        series = df[col_name]
        # Use the first non-null value's type as a hint.
        sample = None
        for v in series:
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                sample = v
                break
        if isinstance(sample, bool):
            return text.strip().lower() in ("true", "1", "yes", "on")
        if isinstance(sample, int):
            try:
                return int(text)
            except (TypeError, ValueError):
                return text
        if isinstance(sample, float):
            try:
                return float(text)
            except (TypeError, ValueError):
                return float("nan")
        return text

    def _on_apply_clicked(self) -> None:
        ext = self._current_extension()
        if not ext or self._network is None:
            return
        if not self._vm.has_edits():
            self._status_lbl.setText("No pending changes.")
            self._status_lbl.setStyleSheet("color: #555;")
            return
        df = self._vm.edits_changes_df()
        try:
            update_extension(self._network, ext, df)
        except Exception as exc:
            QMessageBox.critical(
                self, "Update failed", f"Failed to update {ext}: {exc}",
            )
            return
        n = len(df)
        self._vm.clear_edits()
        self._status_lbl.setText(f"Applied {n} change(s).")
        self._status_lbl.setStyleSheet("color: #0a7e2a;")
        self._refresh_table(preserve_pending=False)
        self.extensions_changed.emit()

    def _on_remove_clicked(self) -> None:
        ext = self._current_extension()
        if not ext or self._network is None:
            return
        ids = self._vm.removals_list()
        if not ids:
            self._status_lbl.setText("Tick at least one row to remove.")
            self._status_lbl.setStyleSheet("color: #555;")
            return
        try:
            remove_extension(self._network, ext, ids)
        except Exception as exc:
            QMessageBox.critical(
                self, "Remove failed", f"Failed to remove {ext}: {exc}",
            )
            return
        self._vm.clear_removals()
        # Drop any cached edits for the just-removed rows.
        self._vm.drop_edits_for(ids)
        self._status_lbl.setText(f"Removed {len(ids)} extension row(s).")
        self._status_lbl.setStyleSheet("color: #0a7e2a;")
        self._refresh_table(preserve_pending=False)
        self.extensions_changed.emit()
