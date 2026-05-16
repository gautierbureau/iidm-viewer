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
    EDITABLE_EXTENSIONS,
    READONLY_EXTENSIONS,
    filter_by_id_substring,
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
        self._current_df: pd.DataFrame = pd.DataFrame()
        self._editable_cols: list[str] = []
        # Map column index → column name for the current extension's table
        # (so cell edits can resolve back to a column name without index
        # arithmetic spread across the file).
        self._col_names: list[str] = []
        # Cached descriptions DataFrame from pypowsybl — looked up once
        # per network change so the picker's caption fires fast.
        self._info_df: pd.DataFrame = pd.DataFrame()

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

        # Tracks per-extension pending edits keyed by (ext_name, element_id).
        # Each value is ``{column: new_value}``. Edits land in here on
        # ``itemChanged`` and get flushed to pypowsybl on Apply.
        self._pending_edits: dict[tuple[str, str], dict[str, object]] = {}
        # IDs ticked for removal — flushed on the Remove click.
        self._pending_removals: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._pending_edits.clear()
        self._pending_removals.clear()
        if network is None:
            self._populate_ext_combo([])
            self._set_table_empty("No network loaded.")
            self._info_df = pd.DataFrame()
            return
        try:
            names = list_extension_names()
        except Exception:
            names = []
        self._populate_ext_combo(names)
        try:
            self._info_df = get_extensions_information()
        except Exception:
            self._info_df = pd.DataFrame()
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
        self._pending_edits.clear()
        self._pending_removals.clear()
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
        # Description caption.
        detail = ""
        if (
            self._info_df is not None
            and not self._info_df.empty
            and ext in self._info_df.index
        ):
            try:
                detail = str(self._info_df.loc[ext].get("detail") or "")
            except Exception:
                detail = ""
        self._detail_lbl.setText(detail)
        self._current_df = df if df is not None else pd.DataFrame()
        if self._current_df.empty:
            self._set_table_empty(f"No {ext!r} extensions found.")
            return
        total = len(self._current_df)
        view = filter_by_id_substring(
            self._current_df, self._filter_edit.text() or "",
        )
        if view.empty:
            self._set_table_empty(
                f"No {ext!r} extensions match the filter.",
            )
            return
        editable_cols = [
            c for c in EDITABLE_EXTENSIONS.get(ext, []) if c in view.columns
        ]
        self._editable_cols = editable_cols
        self._col_names = ["id"] + list(view.columns)
        readonly = ext in READONLY_EXTENSIONS
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
                    Qt.Checked if element_id in self._pending_removals
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
                pending = self._pending_edits.get((ext, element_id), {}).get(col)
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
        readonly = ext in READONLY_EXTENSIONS
        col = item.column()
        row = item.row()
        # Remove-checkbox column.
        if not readonly and col == 0:
            id_item = self._table.item(row, 1)
            if id_item is None:
                return
            element_id = id_item.text()
            if item.checkState() == Qt.Checked:
                self._pending_removals.add(element_id)
            else:
                self._pending_removals.discard(element_id)
            return
        offset = 1 if readonly else 2
        if col < offset:
            return
        col_idx = col - offset
        if col_idx >= len(self._current_df.columns):
            return
        col_name = str(self._current_df.columns[col_idx])
        if col_name not in self._editable_cols:
            return
        id_item = self._table.item(row, offset - 1)
        if id_item is None:
            return
        element_id = id_item.text()
        text = item.text()
        parsed = self._parse_for_column(col_name, text)
        key = (ext, element_id)
        self._pending_edits.setdefault(key, {})
        self._pending_edits[key][col_name] = parsed

    def _parse_for_column(self, col_name: str, text: str):
        """Best-effort cast for the user's input — pypowsybl wants the
        column's native dtype."""
        if col_name not in self._current_df.columns:
            return text
        series = self._current_df[col_name]
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
        pending = {
            element_id: cols
            for (e, element_id), cols in self._pending_edits.items()
            if e == ext
        }
        if not pending:
            self._status_lbl.setText("No pending changes.")
            self._status_lbl.setStyleSheet("color: #555;")
            return
        df = pd.DataFrame.from_dict(pending, orient="index")
        try:
            update_extension(self._network, ext, df)
        except Exception as exc:
            QMessageBox.critical(
                self, "Update failed", f"Failed to update {ext}: {exc}",
            )
            return
        self._pending_edits = {
            k: v for k, v in self._pending_edits.items() if k[0] != ext
        }
        self._status_lbl.setText(f"Applied {len(pending)} change(s).")
        self._status_lbl.setStyleSheet("color: #0a7e2a;")
        self._refresh_table(preserve_pending=False)
        self.extensions_changed.emit()

    def _on_remove_clicked(self) -> None:
        ext = self._current_extension()
        if not ext or self._network is None:
            return
        ids = sorted(self._pending_removals)
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
        self._pending_removals.clear()
        # Drop any cached edits for the just-removed rows.
        self._pending_edits = {
            k: v for k, v in self._pending_edits.items()
            if not (k[0] == ext and k[1] in ids)
        }
        self._status_lbl.setText(f"Removed {len(ids)} extension row(s).")
        self._status_lbl.setStyleSheet("color: #0a7e2a;")
        self._refresh_table(preserve_pending=False)
        self.extensions_changed.emit()
