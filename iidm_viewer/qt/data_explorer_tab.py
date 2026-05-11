"""Data Explorer Components tab — filterable, sortable, editable.

Picks a component type, shows its DataFrame in a ``QTableView``, with:

* a top text box that filters rows across all columns (substring match);
* per-column sort via the table-header click (``QSortFilterProxyModel``);
* in-place editing of the columns listed in
  :data:`iidm_viewer.component_registry.EDITABLE_COMPONENTS` — the edit
  is applied on the pypowsybl worker thread and the row is updated in
  place. Cells in non-editable columns reject the edit silently.

The PySide6 reason-to-exist: Streamlit's ``st.data_editor`` reruns
``app.py`` on every cell touch. Qt's model/view only repaints the
two cells the edit affected. With a network of a few thousand
generators, the difference is "snappy" vs. "freezes for a second".
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.component_registry import (
    COMPONENT_TYPES,
    TOPOLOGY_AFFECTING_ATTRIBUTES,
    apply_bulk_edit,
    apply_cell_edit,
    editable_attributes,
    get_dataframe,
    is_editable,
)
from iidm_viewer.powsybl_worker import NetworkProxy


class PandasTableModel(QAbstractTableModel):
    """Pandas-backed Qt model with optional inline-edit support.

    The model knows nothing about pypowsybl — it stores a DataFrame
    and an "editable columns" allow-list. Edits inside that allow-list
    fire :pyattr:`edit_requested`; the owning tab routes them to
    :func:`apply_cell_edit` on the worker and calls
    :meth:`commit_edit` (or :meth:`reject_edit`) when the call
    returns.
    """

    edit_requested = Signal(str, str, object, object)  # element_id, attribute, new_value, previous_value

    def __init__(self, df: Optional[pd.DataFrame] = None, parent=None) -> None:
        super().__init__(parent)
        self._df: pd.DataFrame = df if df is not None else pd.DataFrame()
        self._editable_cols: set[str] = set()
        self._id_col: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_dataframe(
        self,
        df: pd.DataFrame,
        editable_cols: Optional[list[str]] = None,
    ) -> None:
        self.beginResetModel()
        self._df = df if df is not None else pd.DataFrame()
        self._editable_cols = set(editable_cols or [])
        # pypowsybl getters return the equipment id either as the
        # index (when not reset) or as a column named "id" after
        # ``reset_index()``. ``get_dataframe`` always resets, so "id"
        # is what we look for.
        self._id_col = "id" if "id" in self._df.columns else None
        self.endResetModel()

    def dataframe(self) -> pd.DataFrame:
        return self._df

    def commit_edit(self, row: int, col: int, value) -> None:
        """Mutate the in-memory DataFrame to reflect a successful edit."""
        try:
            self._df.iat[row, col] = value
        except Exception:
            return
        idx = self.index(row, col)
        self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.EditRole])

    # ------------------------------------------------------------------
    # QAbstractTableModel overrides
    # ------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else int(self._df.shape[0])

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else int(self._df.shape[1])

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        try:
            value = self._df.iat[row, col]
        except (IndexError, KeyError):
            return None
        if role == Qt.EditRole:
            # Hand the editor the raw value (so type round-trips work).
            if isinstance(value, float) and pd.isna(value):
                return ""
            return value if isinstance(value, (bool, int, float)) else str(value)
        if role == Qt.DisplayRole:
            if isinstance(value, float) and pd.isna(value):
                return "—"
            if isinstance(value, float):
                return format(value, ".4g")
            return str(value)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            try:
                return str(self._df.columns[section])
            except IndexError:
                return None
        return str(section + 1)

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if not index.isValid():
            return base
        col_name = str(self._df.columns[index.column()])
        if col_name in self._editable_cols:
            return base | Qt.ItemIsEditable
        return base

    def setData(self, index: QModelIndex, value, role: int = Qt.EditRole) -> bool:
        if role != Qt.EditRole or not index.isValid():
            return False
        col_name = str(self._df.columns[index.column()])
        if col_name not in self._editable_cols:
            return False
        if self._id_col is None:
            return False
        try:
            element_id = str(self._df.iat[index.row(), self._df.columns.get_loc(self._id_col)])
            previous = self._df.iat[index.row(), index.column()]
        except (IndexError, KeyError):
            return False
        # Fire-and-forget: the tab does the worker call and then either
        # commits the edit (success) or shows an error and reverts (failure).
        self.edit_requested.emit(element_id, col_name, value, previous)
        return True


class _MultiColumnFilterProxy(QSortFilterProxyModel):
    """Substring filter across every column.

    ``QSortFilterProxyModel`` with ``filterKeyColumn = -1`` only matches
    the rightmost column on some Qt builds; doing the column loop here
    is faster and behaves consistently.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._needle: str = ""

    def set_needle(self, text: str) -> None:
        self._needle = text.lower()
        # PySide6 6.11 flags both ``invalidateFilter`` and
        # ``invalidateRowsFilter`` as deprecated bindings; the
        # underlying Qt C++ API has them as the supported way to
        # ask the proxy to re-run :meth:`filterAcceptsRow`. Suppress
        # the warning rather than chasing a moving target.
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._needle:
            return True
        model = self.sourceModel()
        if model is None:
            return True
        cols = model.columnCount()
        for c in range(cols):
            idx = model.index(source_row, c, source_parent)
            cell = model.data(idx, Qt.DisplayRole)
            if cell is not None and self._needle in str(cell).lower():
                return True
        return False


class DataExplorerTab(QWidget):
    """Pick a component, filter, sort, edit (single cell or in bulk)."""

    edit_applied = Signal(str, str, str, object, object)  # component, element_id, attribute, new, prev
    bulk_edit_applied = Signal(str, list, str, object, dict)  # component, ids, attribute, new, prev_map

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None

        self._combo = QComboBox()
        for label in COMPONENT_TYPES:
            self._combo.addItem(label)
        self._combo.currentTextChanged.connect(self._on_component_changed)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter rows (matches across all columns)…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._on_filter_changed)

        self._summary = QLabel("Load a network to inspect its components.")
        self._summary.setStyleSheet("padding: 6px 10px; color: #444;")

        self._model = PandasTableModel()
        self._model.edit_requested.connect(self._on_edit_requested)
        self._proxy = _MultiColumnFilterProxy(self)
        self._proxy.setSourceModel(self._model)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)  # uses _proxy's sort
        # ``ExtendedSelection`` adds Ctrl/Shift multi-row picking on top of
        # the row-level selection behaviour. Required for bulk edit.
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setSelectionMode(QTableView.ExtendedSelection)
        self._table.setEditTriggers(
            QTableView.DoubleClicked | QTableView.EditKeyPressed | QTableView.AnyKeyPressed
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setDefaultSectionSize(22)
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        controls = QHBoxLayout()
        controls.setContentsMargins(10, 6, 10, 0)
        controls.addWidget(QLabel("Component:"))
        controls.addWidget(self._combo)
        controls.addSpacing(12)
        controls.addWidget(self._filter, 1)

        # --- Bulk-edit panel ---------------------------------------------------
        # Disabled until N>=1 rows are selected and the current component
        # has at least one editable attribute.
        self._bulk_label = QLabel("Apply to 0 selected:")
        self._bulk_attr = QComboBox()
        self._bulk_attr.setMinimumWidth(140)
        self._bulk_value = QLineEdit()
        self._bulk_value.setPlaceholderText("New value")
        self._bulk_value.setMinimumWidth(120)
        self._bulk_apply = QPushButton("Apply")
        self._bulk_apply.clicked.connect(self._on_bulk_apply)
        self._bulk_panel = QFrame()
        self._bulk_panel.setFrameShape(QFrame.NoFrame)
        bulk_layout = QHBoxLayout(self._bulk_panel)
        bulk_layout.setContentsMargins(10, 2, 10, 4)
        bulk_layout.addWidget(self._bulk_label)
        bulk_layout.addWidget(self._bulk_attr)
        bulk_layout.addWidget(QLabel("="))
        bulk_layout.addWidget(self._bulk_value, 1)
        bulk_layout.addWidget(self._bulk_apply)
        self._set_bulk_enabled(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(controls)
        layout.addWidget(self._summary)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._bulk_panel)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        if network is None:
            self._model.set_dataframe(pd.DataFrame(), editable_cols=[])
            self._summary.setText("No network loaded.")
            return
        self._refresh(self._combo.currentText())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _on_component_changed(self, label: str) -> None:
        if self._network is None or not label:
            return
        # Clearing the filter on component switch avoids "empty grid"
        # confusion when the new component doesn't match the old needle.
        self._filter.blockSignals(True)
        self._filter.clear()
        self._filter.blockSignals(False)
        self._proxy.set_needle("")
        self._refresh(label)

    def _on_filter_changed(self, text: str) -> None:
        self._proxy.set_needle(text)
        # Update the row count in the summary so the user sees the filter take.
        df = self._model.dataframe()
        visible = self._proxy.rowCount()
        editable_msg = (
            " · editable: " + ", ".join(editable_attributes(self._combo.currentText()))
            if is_editable(self._combo.currentText())
            else ""
        )
        if not df.empty:
            self._summary.setText(
                f"{self._combo.currentText()}: {visible} / {df.shape[0]} rows "
                f"· {df.shape[1]} columns{editable_msg}"
            )

    def _refresh(self, label: str) -> None:
        if self._network is None:
            return
        try:
            df = get_dataframe(self._network, label)
        except Exception as exc:
            self._model.set_dataframe(pd.DataFrame(), editable_cols=[])
            self._summary.setText(f"{label}: failed — {exc}")
            return
        cols = editable_attributes(label)
        # Keep only the columns the DataFrame actually has — some
        # editable attributes may be absent on networks that don't
        # carry their extensions (e.g. regulated_element_id).
        cols = [c for c in cols if c in df.columns]
        self._model.set_dataframe(df, editable_cols=cols)
        if df.empty:
            self._summary.setText(f"{label}: empty (no rows in this network)")
        else:
            editable_msg = " · editable: " + ", ".join(cols) if cols else ""
            self._summary.setText(
                f"{label}: {df.shape[0]} rows · {df.shape[1]} columns{editable_msg}"
            )
        if not df.empty:
            self._table.resizeColumnsToContents()
        # The set of editable attributes is component-specific; refresh
        # the bulk panel so its dropdown reflects the current frame.
        self._refresh_bulk_attrs()

    def _set_bulk_enabled(self, enabled: bool) -> None:
        for w in (self._bulk_attr, self._bulk_value, self._bulk_apply):
            w.setEnabled(enabled)

    def _refresh_bulk_attrs(self) -> None:
        """Re-fill the bulk-attr combo with the current component's
        editable columns, keeping the previous selection when possible.
        """
        component = self._combo.currentText()
        cols = [c for c in editable_attributes(component) if c in self._model.dataframe().columns]
        previous = self._bulk_attr.currentText()
        self._bulk_attr.blockSignals(True)
        self._bulk_attr.clear()
        for c in cols:
            self._bulk_attr.addItem(c)
        if previous in cols:
            self._bulk_attr.setCurrentText(previous)
        self._bulk_attr.blockSignals(False)
        self._update_bulk_state()

    def _selected_element_ids(self) -> list[str]:
        """Map the table's selected proxy rows back to element ids."""
        sel = self._table.selectionModel()
        if sel is None or self._model._id_col is None:
            return []
        rows = sel.selectedRows()  # one QModelIndex per selected row, in the first column
        ids: list[str] = []
        id_col_pos = self._model.dataframe().columns.get_loc(self._model._id_col)
        for proxy_idx in rows:
            source_idx = self._proxy.mapToSource(proxy_idx)
            try:
                ids.append(str(self._model.dataframe().iat[source_idx.row(), id_col_pos]))
            except (IndexError, KeyError):
                continue
        return ids

    def _update_bulk_state(self) -> None:
        n = len(self._selected_element_ids())
        component = self._combo.currentText()
        has_editable = self._bulk_attr.count() > 0
        self._bulk_label.setText(f"Apply to {n} selected:")
        self._set_bulk_enabled(n > 0 and has_editable and is_editable(component))

    def _on_selection_changed(self, *_args) -> None:
        self._update_bulk_state()

    def _on_bulk_apply(self) -> None:
        component = self._combo.currentText()
        attribute = self._bulk_attr.currentText()
        new_value = self._bulk_value.text()
        ids = self._selected_element_ids()
        if not ids or not attribute or self._network is None:
            return
        try:
            prev_map = apply_bulk_edit(
                self._network, component, ids, attribute, new_value,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Bulk edit rejected",
                f"{component}/{len(ids)} rows/{attribute}\n\n{exc}",
            )
            return
        # Re-fetch the live frame once so the model reflects the
        # coerced/normalised values pypowsybl just accepted. Cheaper
        # than N individual cell writes and keeps the row index aligned
        # with the network after potential dtype normalisation.
        try:
            df = get_dataframe(self._network, component)
        except Exception:
            df = self._model.dataframe()
        cols = [c for c in editable_attributes(component) if c in df.columns]
        self._model.set_dataframe(df, editable_cols=cols)
        # apply_bulk_edit already coerced new_value once against the
        # column dtype; report that to listeners.
        try:
            from iidm_viewer.component_registry import _coerce
            display_value = _coerce(new_value, df[attribute].dtype) if attribute in df.columns else new_value
        except Exception:
            display_value = new_value
        self._bulk_value.clear()
        self.bulk_edit_applied.emit(component, ids, attribute, display_value, prev_map)
        self._update_bulk_state()

    def _on_edit_requested(self, element_id: str, attribute: str, new_value, previous) -> None:
        component = self._combo.currentText()
        if self._network is None or not is_editable(component, attribute):
            return
        try:
            prev = apply_cell_edit(self._network, component, element_id, attribute, new_value)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Edit rejected",
                f"{component}/{element_id}/{attribute}\n\n{exc}",
            )
            return
        # Mutate the in-memory frame so the cell repaints with the
        # accepted value (which may have been coerced — e.g. "true" -> True).
        df = self._model.dataframe()
        try:
            row = df.index[df[self._model._id_col].astype(str) == element_id][0]
            row_pos = df.index.get_loc(row)
            col_pos = df.columns.get_loc(attribute)
        except (KeyError, IndexError):
            return
        # Use the input value rather than re-reading from pypowsybl — one
        # less worker hop. apply_cell_edit already coerced and applied;
        # the only mismatch case is pypowsybl rejecting silently, which
        # it doesn't (validation failures raise).
        coerced = self._model.dataframe()[attribute].dtype
        try:
            from iidm_viewer.component_registry import _coerce
            display_value = _coerce(new_value, coerced)
        except Exception:
            display_value = new_value
        self._model.commit_edit(row_pos, col_pos, display_value)
        self.edit_applied.emit(component, element_id, attribute, display_value, prev)
