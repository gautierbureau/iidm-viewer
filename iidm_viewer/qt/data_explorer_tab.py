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
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.change_log import ChangeLog
from iidm_viewer.component_creation import (
    CREATABLE_BRANCHES,
    CREATABLE_COMPONENTS,
    CREATABLE_CONTAINERS,
)
from iidm_viewer.qt.change_log_panel import ChangeLogPanel
from iidm_viewer.qt.create_panel import (
    CreateBranchPanel,
    CreateComponentPanel,
    CreateContainerPanel,
    CreateCouplingDevicePanel,
    CreateHvdcLinePanel,
    CreateOperationalLimitsPanel,
    CreateReactiveLimitsPanel,
    CreateExtensionPanel,
    CreateSecondaryVoltageControlPanel,
    CreateTapChangerPanel,
)
from iidm_viewer.component_registry import (
    COMPONENT_TYPES,
    DISCONNECTABLE_COMPONENTS,
    DISCONNECT_ATTRS,
    REMOVABLE_COMPONENTS,
    TOPOLOGY_AFFECTING_ATTRIBUTES,
    apply_cell_edit,
    editable_attributes,
    get_dataframe,
    is_editable,
)
from iidm_viewer.data_view import (
    FILTERS,
    VL_FILTERABLE,
    apply_and_log_bulk_disconnect,
    apply_and_log_bulk_edit,
    apply_filter_specs,
    build_data_explorer_view_model,
    compute_filter_widget_spec,
    dataframe_to_csv,
    delete_and_log_elements,
)
from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.variants import INITIAL_VARIANT_ID, NK_VARIANT_ID


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
    bulk_removed = Signal(str, list)  # component, removed_ids
    # Emitted after a successful "Apply & Run LF" bulk edit — the
    # MainWindow listens and calls state.run_loadflow().
    loadflow_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._change_log: Optional[ChangeLog] = None
        # The selected_vl coming from the MainWindow's AppState. Used
        # by the "filter by selected VL" checkbox below.
        self._selected_vl: Optional[str] = None
        # Active structured-filter specs (column -> value) — fed into
        # ``data_view.apply_filter_specs`` on every refresh.
        self._filter_specs: dict[str, object] = {}
        # Per-column filter widgets, rebuilt on every component change.
        self._filter_widgets: dict[str, QWidget] = {}
        # Currently displayed variant — the view-mode combo below flips
        # it; N-K mode is read-only by contract (no in-cell edits, no
        # bulk apply, no remove). ``_view_mode`` (N / N-K /
        # Side-by-side) tracks the combo selection separately from
        # the active variant — in Side-by-side, the primary table is
        # always InitialState and the right pane is N-K.
        self._variant_id = INITIAL_VARIANT_ID
        self._view_mode = "N"
        self._state = None  # set via set_state

        self._combo = QComboBox()
        for label in COMPONENT_TYPES:
            self._combo.addItem(label)
        generators_idx = list(COMPONENT_TYPES).index("Generators")
        self._combo.setCurrentIndex(generators_idx)
        self._combo.currentTextChanged.connect(self._on_component_changed)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter rows (matches across all columns)…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._on_filter_changed)

        # "Filter by VL" checkbox — only visible when the network has
        # been loaded and the current component is VL-filterable.
        self._vl_filter = QCheckBox()
        self._vl_filter.setText("Filter by VL")
        self._vl_filter.toggled.connect(lambda _v: self._refresh(self._combo.currentText()))
        self._vl_filter.setVisible(False)

        # CSV download — exports whatever the user is currently looking
        # at (post enrichment, reorder, filters).
        self._csv_btn = QPushButton("Download CSV")
        self._csv_btn.clicked.connect(self._on_csv_clicked)

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
        # Secondary table for the N-K pane in Side-by-side mode. Read
        # only by contract (no edit triggers) — N-K rows reflect the
        # post-LF state under the contingency.
        self._model_nk = PandasTableModel()
        self._table_nk = QTableView()
        self._table_nk.setModel(self._model_nk)
        self._table_nk.setAlternatingRowColors(True)
        self._table_nk.setSelectionBehavior(QTableView.SelectRows)
        self._table_nk.setEditTriggers(QTableView.NoEditTriggers)
        self._table_nk.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setDefaultSectionSize(22)
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        # View-mode combo — disabled until an N-K variant is built.
        # N-K mode is read-only by contract.
        self._view_mode_combo = QComboBox()
        # 'Side-by-side' shows the N rows on the left and the N-K rows
        # on the right via a QSplitter; the N-K pane is read-only by
        # contract (bulk-edit buttons stay disabled).
        self._view_mode_combo.addItems(["N", "N-K", "Side-by-side"])
        self._view_mode_combo.setEnabled(False)
        self._view_mode_combo.currentTextChanged.connect(
            self._on_view_mode_changed,
        )

        controls = QHBoxLayout()
        controls.setContentsMargins(10, 6, 10, 0)
        controls.addWidget(QLabel("View:"))
        controls.addWidget(self._view_mode_combo)
        controls.addSpacing(8)
        controls.addWidget(QLabel("Component:"))
        controls.addWidget(self._combo)
        controls.addSpacing(8)
        controls.addWidget(self._vl_filter)
        controls.addSpacing(12)
        controls.addWidget(self._filter, 1)
        controls.addSpacing(8)
        controls.addWidget(self._csv_btn)

        # Structured filters live in a separate horizontal row that
        # repopulates on every component change.
        self._filters_panel = QFrame()
        self._filters_panel.setFrameShape(QFrame.NoFrame)
        self._filters_layout = QHBoxLayout(self._filters_panel)
        self._filters_layout.setContentsMargins(10, 0, 10, 4)
        self._filters_layout.setSpacing(8)
        self._filters_panel.setVisible(False)

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
        # "Apply & Run LF" mirrors Streamlit's twin-button layout: the
        # edit goes through ``apply_bulk_edit`` as usual, then an AC
        # load flow runs on the network. The Run-LF call sits outside
        # this tab; we surface a callback the MainWindow plugs in.
        self._bulk_apply_lf = QPushButton("Apply && Run LF")
        self._bulk_apply_lf.clicked.connect(self._on_bulk_apply_lf)
        # Two more bulk actions next to "Apply", both vectorised
        # through the shared registry. "Disconnect" flips the right
        # ``connected*`` / ``open`` attribute(s) per component type;
        # "Delete" routes through pn.remove_feeder_bays /
        # remove_hvdc_lines / remove_voltage_levels as appropriate.
        self._bulk_disconnect = QPushButton("Disconnect")
        self._bulk_disconnect.clicked.connect(self._on_bulk_disconnect)
        self._bulk_disconnect.setToolTip(
            "Set connected=False (or open=True for switches) on the "
            "selected elements; records each change in the Change Log."
        )
        self._bulk_delete = QPushButton("Delete")
        self._bulk_delete.setStyleSheet("color: #b30000;")
        self._bulk_delete.clicked.connect(self._on_bulk_delete)
        self._bulk_delete.setToolTip(
            "Remove the selected elements from the network. "
            "Cascades to bay switches, HVDC triples, and VL "
            "containers as appropriate. Not reversible."
        )

        self._bulk_panel = QFrame()
        self._bulk_panel.setFrameShape(QFrame.NoFrame)
        bulk_layout = QHBoxLayout(self._bulk_panel)
        bulk_layout.setContentsMargins(10, 2, 10, 4)
        bulk_layout.addWidget(self._bulk_label)
        bulk_layout.addWidget(self._bulk_attr)
        bulk_layout.addWidget(QLabel("="))
        bulk_layout.addWidget(self._bulk_value, 1)
        bulk_layout.addWidget(self._bulk_apply)
        bulk_layout.addWidget(self._bulk_apply_lf)
        bulk_layout.addWidget(self._bulk_disconnect)
        bulk_layout.addWidget(self._bulk_delete)
        self._set_bulk_enabled(False)

        self._change_log_panel = ChangeLogPanel()
        self._change_log_panel.reverted.connect(self._on_log_reverted)

        # Create-new-component panels: visible only when the current
        # component is in CREATABLE_COMPONENTS / CREATABLE_BRANCHES and
        # the network has node-breaker voltage levels. The form schemas
        # come from the shared registry; the widget toolkit is Qt-specific.
        self._create_panel = CreateComponentPanel()
        self._create_panel.component_created.connect(self._on_component_created)
        self._create_branch_panel = CreateBranchPanel()
        self._create_branch_panel.component_created.connect(self._on_component_created)
        self._create_container_panel = CreateContainerPanel()
        self._create_container_panel.component_created.connect(self._on_component_created)
        self._create_hvdc_panel = CreateHvdcLinePanel()
        self._create_hvdc_panel.component_created.connect(self._on_component_created)
        self._create_tap_changer_panel = CreateTapChangerPanel()
        self._create_tap_changer_panel.component_created.connect(self._on_component_created)
        self._create_coupling_panel = CreateCouplingDevicePanel()
        self._create_coupling_panel.component_created.connect(self._on_component_created)
        self._create_reactive_limits_panel = CreateReactiveLimitsPanel()
        self._create_reactive_limits_panel.component_created.connect(self._on_component_created)
        self._create_operational_limits_panel = CreateOperationalLimitsPanel()
        self._create_operational_limits_panel.component_created.connect(self._on_component_created)
        self._create_svc_panel = CreateSecondaryVoltageControlPanel()
        self._create_svc_panel.component_created.connect(self._on_component_created)
        self._create_extension_panel = CreateExtensionPanel()
        self._create_extension_panel.component_created.connect(self._on_component_created)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(controls)
        layout.addWidget(self._create_panel)
        layout.addWidget(self._create_branch_panel)
        layout.addWidget(self._create_container_panel)
        layout.addWidget(self._create_hvdc_panel)
        layout.addWidget(self._create_tap_changer_panel)
        layout.addWidget(self._create_coupling_panel)
        layout.addWidget(self._create_reactive_limits_panel)
        layout.addWidget(self._create_operational_limits_panel)
        layout.addWidget(self._create_svc_panel)
        layout.addWidget(self._create_extension_panel)
        layout.addWidget(self._filters_panel)
        layout.addWidget(self._summary)
        # Wrap the primary + N-K tables in a QSplitter so Side-by-side
        # renders them next to each other. Single-pane modes hide the
        # N-K table.
        self._table_splitter = QSplitter(Qt.Horizontal)
        self._table_splitter.addWidget(self._table)
        self._table_splitter.addWidget(self._table_nk)
        self._table_nk.setVisible(False)
        layout.addWidget(self._table_splitter, 1)
        layout.addWidget(self._bulk_panel)
        layout.addWidget(self._change_log_panel)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def _create_panels(self):
        """Return the tuple of every create-sub-panel hosted by this tab.

        Used by :meth:`set_network` and :meth:`_on_component_created`
        to fan out network / component updates without listing each
        panel twice — a fresh in-tab create (Substation, VL, BBS, …)
        changes what every *other* panel can offer, so they all need
        the same refresh.
        """
        return (
            self._create_panel,
            self._create_branch_panel,
            self._create_container_panel,
            self._create_hvdc_panel,
            self._create_tap_changer_panel,
            self._create_coupling_panel,
            self._create_reactive_limits_panel,
            self._create_operational_limits_panel,
            self._create_svc_panel,
            self._create_extension_panel,
        )

    def _fanout_to_create_panels(
        self, network: Optional[NetworkProxy], component: str,
    ) -> None:
        """Push the current network + active component into every
        create panel. Each panel decides on its own whether to render
        for the given component."""
        for panel in self._create_panels():
            panel.set_network(network)
            panel.set_component(component)

    def set_state(self, state) -> None:
        """Wire the tab to the host's :class:`AppState` so the view-mode
        combo can enable / disable in response to N-K variant lifecycle
        events. The N-K pane is read-only by contract — the in-cell
        edit triggers stay disabled while ``self._variant_id`` is N-K
        (handled inside ``_refresh`` via the editable-cols whitelist
        and the bulk-edit button gates below)."""
        self._state = state
        state.nk_variant_changed.connect(self._on_nk_variant_changed)
        state.nk_loadflow_completed.connect(
            lambda _r: self._refresh(self._combo.currentText())
        )
        self._on_nk_variant_changed(state.nk_variant_id)

    def _on_nk_variant_changed(self, variant_id) -> None:
        active = variant_id == NK_VARIANT_ID
        self._view_mode_combo.setEnabled(active)
        if not active:
            self._view_mode_combo.blockSignals(True)
            self._view_mode_combo.setCurrentText("N")
            self._view_mode_combo.blockSignals(False)
            self._view_mode = "N"
            self._table_nk.setVisible(False)
            if self._variant_id != INITIAL_VARIANT_ID:
                self._variant_id = INITIAL_VARIANT_ID
                self._refresh(self._combo.currentText())
        self._apply_variant_readonly()

    def _on_view_mode_changed(self, txt: str) -> None:
        if txt == self._view_mode:
            return
        self._view_mode = txt
        if txt == "Side-by-side":
            # Primary table stays bound to InitialState so bulk-edit
            # affordances continue to work for N; the N-K table on the
            # right is populated from a parallel variant view-model.
            self._variant_id = INITIAL_VARIANT_ID
            self._table_nk.setVisible(True)
        elif txt == "N-K":
            self._variant_id = NK_VARIANT_ID
            self._table_nk.setVisible(False)
        else:
            self._variant_id = INITIAL_VARIANT_ID
            self._table_nk.setVisible(False)
        self._apply_variant_readonly()
        self._refresh(self._combo.currentText())

    def _apply_variant_readonly(self) -> None:
        """N-K mode is read-only — flush in-cell edits through the
        ``_set_bulk_enabled`` recompute so the bulk Apply / Apply&LF
        / Disconnect / Delete buttons grey out. The in-cell edit
        gate lives in :meth:`_on_edit_requested`."""
        # Trigger a recompute of the bulk-button enabled state. The
        # variant gate is inside ``_set_bulk_enabled`` (see below).
        self._on_selection_changed()

    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._change_log_panel.set_network(network)
        self._fanout_to_create_panels(network, self._combo.currentText())
        if network is None:
            self._model.set_dataframe(pd.DataFrame(), editable_cols=[])
            self._summary.setText("No network loaded.")
            return
        self._refresh(self._combo.currentText())

    def set_change_log(self, change_log: ChangeLog) -> None:
        """Bind the host's ChangeLog instance so cell + bulk edits get recorded."""
        self._change_log = change_log
        self._change_log_panel.set_change_log(change_log)

    def set_selected_vl(self, vl_id: Optional[str]) -> None:
        """Push the host's selected VL down so "Filter by VL" can use it."""
        self._selected_vl = vl_id or None
        self._update_vl_filter_visibility()
        # Only re-fetch when the filter is on; otherwise no work needed.
        if self._vl_filter.isChecked() and self._network is not None:
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
        # Drop the structured-filter specs — they're per-component
        # and the new component may not have the same columns.
        self._filter_specs.clear()
        self._update_vl_filter_visibility()
        # Update the create panels to render the new component's form;
        # each one auto-hides for the wrong category.
        self._create_panel.set_component(label)
        self._create_branch_panel.set_component(label)
        self._create_container_panel.set_component(label)
        self._create_hvdc_panel.set_component(label)
        self._create_tap_changer_panel.set_component(label)
        self._create_coupling_panel.set_component(label)
        self._create_reactive_limits_panel.set_component(label)
        self._create_operational_limits_panel.set_component(label)
        self._create_svc_panel.set_component(label)
        self._create_extension_panel.set_component(label)
        self._refresh(label)
        # Rebuild the structured-filter widgets *after* refresh so the
        # widget specs come from the freshly-loaded frame.
        self._rebuild_filter_widgets(label)

    def _on_component_created(self, component: str, element_id: str) -> None:
        """Refresh the data grid + diagram caches + sibling create
        panels after a new component is created.

        Creating a Substation / Voltage Level / Busbar Section changes
        what every *other* create panel can offer (e.g. the Generators
        form needs the new node-breaker VL in its dropdown). Fan the
        latest network + active component out to every create panel
        before refreshing the data grid so the user sees the new
        options without having to reload.
        """
        active = self._combo.currentText()
        # Re-feed every create panel so VL / busbar / target dropdowns
        # pick up the brand-new element. Each panel is idempotent on a
        # set_network/set_component pair.
        self._fanout_to_create_panels(self._network, active)
        # Re-fetch the live frame to surface the new row.
        if component == active:
            self._refresh(component)
        # Diagram caches store SVGs that don't include the new element;
        # surface this via the bulk_edit_applied path so MainWindow's
        # existing topology-handler flushes them.
        self.bulk_edit_applied.emit(component, [element_id], "connected", True, {})

    def _update_vl_filter_visibility(self) -> None:
        component = self._combo.currentText()
        applicable = (
            component in VL_FILTERABLE
            and self._network is not None
            and self._selected_vl is not None
        )
        self._vl_filter.setVisible(applicable)
        if applicable and self._selected_vl:
            self._vl_filter.setText(f"Filter by VL: {self._selected_vl}")
        if not applicable and self._vl_filter.isChecked():
            self._vl_filter.blockSignals(True)
            self._vl_filter.setChecked(False)
            self._vl_filter.blockSignals(False)

    def _rebuild_filter_widgets(self, label: str) -> None:
        """Reconstruct the structured-filter row for ``label``.

        Builds one widget per column in ``FILTERS[label]`` that's
        present in the source dataframe, with shape decided by
        ``compute_filter_widget_spec``. Updates ``self._filter_specs``
        on every change and re-fires ``_refresh``.
        """
        # Clear any existing widgets.
        while self._filters_layout.count():
            item = self._filters_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._filter_widgets.clear()

        df_source = self._model.dataframe()
        cols = [c for c in FILTERS.get(label, []) if c in df_source.columns]
        if not cols:
            self._filters_panel.setVisible(False)
            return

        self._filters_layout.addWidget(QLabel("Filters:"))
        for col in cols:
            self._add_filter_widget(col, df_source[col])
        self._filters_layout.addStretch(1)
        self._filters_panel.setVisible(True)

    def _add_filter_widget(self, col: str, series) -> None:
        shape = compute_filter_widget_spec(series)
        kind = shape.get("kind")
        if kind == "skip":
            return

        self._filters_layout.addWidget(QLabel(f"{col}:"))
        if kind == "bool":
            cb = QComboBox()
            cb.addItems(["Any", "True", "False"])
            def _on_change(v, col=col):
                if v == "Any":
                    self._filter_specs.pop(col, None)
                else:
                    self._filter_specs[col] = v
                self._refresh(self._combo.currentText())
            cb.currentTextChanged.connect(_on_change)
            self._filters_layout.addWidget(cb)
            self._filter_widgets[col] = cb
            return

        if kind == "range":
            state = shape.get("state")
            if state in ("empty", "constant"):
                lbl = QLabel(f"({state})")
                lbl.setStyleSheet("color: #888;")
                self._filters_layout.addWidget(lbl)
                return
            lo, hi = shape["min"], shape["max"]
            sp_lo = QDoubleSpinBox()
            sp_lo.setRange(lo, hi)
            sp_lo.setValue(lo)
            sp_lo.setDecimals(3)
            sp_hi = QDoubleSpinBox()
            sp_hi.setRange(lo, hi)
            sp_hi.setValue(hi)
            sp_hi.setDecimals(3)
            def _on_change(_v=None, col=col, sp_lo=sp_lo, sp_hi=sp_hi, lo=lo, hi=hi):
                sel = (sp_lo.value(), sp_hi.value())
                if sel == (lo, hi):
                    self._filter_specs.pop(col, None)
                else:
                    self._filter_specs[col] = sel
                self._refresh(self._combo.currentText())
            sp_lo.valueChanged.connect(_on_change)
            sp_hi.valueChanged.connect(_on_change)
            self._filters_layout.addWidget(sp_lo)
            self._filters_layout.addWidget(QLabel("–"))
            self._filters_layout.addWidget(sp_hi)
            self._filter_widgets[col] = (sp_lo, sp_hi)
            return

        if kind == "multiselect":
            # QToolButton with a popup menu of checkable actions —
            # equivalent UX to Streamlit's ``st.multiselect``.
            tb = QToolButton()
            tb.setText("any")
            tb.setPopupMode(QToolButton.InstantPopup)
            menu = QMenu(tb)
            options = list(shape["options"])
            def _update_label():
                vals = self._filter_specs.get(col)
                if not vals:
                    tb.setText("any")
                elif len(vals) <= 2:
                    tb.setText(", ".join(map(str, vals)))
                else:
                    tb.setText(f"{len(vals)} selected")
            for opt in options:
                act = menu.addAction(str(opt))
                act.setCheckable(True)
                def _toggle(checked, opt=opt, col=col):
                    vals = list(self._filter_specs.get(col, []))
                    if checked and opt not in vals:
                        vals.append(opt)
                    elif not checked and opt in vals:
                        vals.remove(opt)
                    if vals:
                        self._filter_specs[col] = vals
                    else:
                        self._filter_specs.pop(col, None)
                    _update_label()
                    self._refresh(self._combo.currentText())
                act.toggled.connect(_toggle)
            tb.setMenu(menu)
            self._filters_layout.addWidget(tb)
            self._filter_widgets[col] = tb
            _update_label()
            return

    def _on_csv_clicked(self) -> None:
        df = self._model.dataframe()
        if df.empty:
            return
        component = self._combo.currentText() or "data"
        default_name = f"{component.lower().replace(' ', '_')}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Download CSV", default_name, "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(dataframe_to_csv(df))
        except Exception as exc:
            QMessageBox.warning(self, "Save failed", str(exc))

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
        # The host-agnostic ``build_data_explorer_view_model`` runs the
        # whole "fetch enriched → reorder → optional VL filter →
        # structured per-column filters → editable-cols intersection"
        # pipeline in one call so the Streamlit, PySide6 and NiceGUI
        # tabs stay in lockstep. ``filter_specs`` carries this tab's
        # structured filter state; the VL toggle / selected_vl come
        # from the local widget state.
        try:
            vm = build_data_explorer_view_model(
                self._network,
                label,
                selected_vl=self._selected_vl,
                filter_by_vl=(
                    self._vl_filter.isChecked() and label in VL_FILTERABLE
                ),
                filter_specs=self._filter_specs,
                variant_id=self._variant_id,
            )
        except Exception as exc:
            self._model.set_dataframe(pd.DataFrame(), editable_cols=[])
            self._summary.setText(f"{label}: failed — {exc}")
            return

        df = vm.rows_df
        original_rows = vm.total_count
        cols = list(vm.editable_cols)
        self._model.set_dataframe(df, editable_cols=cols)
        if vm.is_empty and original_rows == 0:
            self._summary.setText(f"{label}: empty (no rows in this network)")
        else:
            editable_msg = " · editable: " + ", ".join(cols) if cols else ""
            if vm.filtered_count < original_rows:
                self._summary.setText(
                    f"{label}: {vm.filtered_count} / {original_rows} rows · "
                    f"{df.shape[1]} columns{editable_msg}"
                )
            else:
                self._summary.setText(
                    f"{label}: {vm.filtered_count} rows · "
                    f"{df.shape[1]} columns{editable_msg}"
                )
        if not df.empty:
            self._table.resizeColumnsToContents()
        # The set of editable attributes is component-specific; refresh
        # the bulk panel so its dropdown reflects the current frame.
        self._refresh_bulk_attrs()
        if self._view_mode == "Side-by-side":
            self._refresh_nk_table(label)

    def _refresh_nk_table(self, label: str) -> None:
        """Populate the right-pane N-K table for ``label``. Read-only —
        no editable columns surfaced. Per-(net_key, lf_gen[N-K],
        N-K) caching from data_view.get_enriched_dataframe still
        applies."""
        if self._network is None:
            self._model_nk.set_dataframe(pd.DataFrame(), editable_cols=[])
            return
        try:
            nk_vm = build_data_explorer_view_model(
                self._network,
                label,
                selected_vl=self._selected_vl,
                filter_by_vl=(
                    self._vl_filter.isChecked() and label in VL_FILTERABLE
                ),
                filter_specs=self._filter_specs,
                variant_id=NK_VARIANT_ID,
            )
        except Exception:
            self._model_nk.set_dataframe(pd.DataFrame(), editable_cols=[])
            return
        nk_df = nk_vm.rows_df if nk_vm is not None else pd.DataFrame()
        self._model_nk.set_dataframe(nk_df, editable_cols=[])
        if not nk_df.empty:
            self._table_nk.resizeColumnsToContents()

    def _on_log_reverted(self, touched) -> None:
        """When the change-log panel reverts entries, refresh the grid
        if the currently-displayed component was touched. Also signal
        the main window so it can clear NAD/SLD caches when needed.
        """
        if not touched or self._network is None:
            return
        current = self._combo.currentText()
        if any(component == current for component, _ in touched):
            self._refresh(current)
        # Surface each touched (component, attribute) up via the
        # bulk_edit_applied signal — MainWindow already invalidates
        # diagram caches for topology-affecting attributes on that path.
        for component, attribute in touched:
            self.bulk_edit_applied.emit(component, [], attribute, None, {})

    def _set_bulk_enabled(self, enabled: bool) -> None:
        # ``enabled`` here is the bulk-EDIT enable state. Disconnect /
        # Delete have their own enable rules (any selection, vs. the
        # component being disconnectable / removable). N-K mode is
        # read-only by contract so every bulk action stays disabled
        # while ``self._variant_id`` is not InitialState.
        editable_variant = self._variant_id == INITIAL_VARIANT_ID
        effective = enabled and editable_variant
        for w in (self._bulk_attr, self._bulk_value, self._bulk_apply, self._bulk_apply_lf):
            w.setEnabled(effective)
        n_selected = len(self._selected_element_ids())
        component = self._combo.currentText()
        self._bulk_disconnect.setEnabled(
            editable_variant
            and n_selected > 0
            and component in DISCONNECTABLE_COMPONENTS
        )
        self._bulk_delete.setEnabled(
            editable_variant
            and n_selected > 0
            and component in REMOVABLE_COMPONENTS
        )

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
        # ``_set_bulk_enabled`` updates the disconnect / delete enable
        # state too, based on the current component's membership in
        # DISCONNECTABLE_COMPONENTS / REMOVABLE_COMPONENTS.
        self._set_bulk_enabled(n > 0 and has_editable and is_editable(component))

    def _on_selection_changed(self, *_args) -> None:
        self._update_bulk_state()

    def _on_bulk_disconnect(self) -> None:
        component = self._combo.currentText()
        ids = self._selected_element_ids()
        if not ids or self._network is None:
            return
        if component not in DISCONNECTABLE_COMPONENTS:
            return
        try:
            outcome = apply_and_log_bulk_disconnect(
                self._network, component, ids, change_log=self._change_log,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Disconnect rejected",
                f"{component}/{len(ids)} rows\n\n{exc}",
            )
            return
        df = outcome["refreshed_df"]
        cols = [c for c in editable_attributes(component) if c in df.columns]
        self._model.set_dataframe(df, editable_cols=cols)
        # Re-emit on bulk_edit_applied so MainWindow flushes NAD/SLD caches
        # for each touched attribute (Lines / 2W flip two).
        for attribute, prev_map in outcome["per_attr_prev_map"].items():
            self.bulk_edit_applied.emit(
                component, ids, attribute,
                DISCONNECT_ATTRS[component][attribute], prev_map,
            )
        self._update_bulk_state()

    def _on_bulk_delete(self) -> None:
        component = self._combo.currentText()
        ids = self._selected_element_ids()
        if not ids or self._network is None:
            return
        if component not in REMOVABLE_COMPONENTS:
            return
        confirm = QMessageBox.question(
            self,
            "Delete elements?",
            f"Permanently remove {len(ids)} {component.lower()} from the "
            f"network?\n\n"
            f"This cascades (feeder-bay switches, HVDC triples, VL "
            f"contents) and is not reversible by the Change Log.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return
        # Snapshot the to-be-removed rows so the ChangeLog can stash
        # them — useful for a future "recreate" undo and for visual
        # display in the panel.
        df = self._model.dataframe()
        snapshot = (
            df.set_index(self._model._id_col, drop=False)
            if self._model._id_col and self._model._id_col in df.columns
            else None
        )
        try:
            removed = delete_and_log_elements(
                self._network, component, ids,
                change_log=self._change_log,
                snapshot_df=snapshot,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Delete failed",
                f"{component}/{len(ids)} rows\n\n{exc}",
            )
            return
        # Refetch the live frame.
        self._refresh(component)
        self.bulk_removed.emit(component, removed)
        self._update_bulk_state()

    def _on_bulk_apply_lf(self) -> None:
        """Apply the bulk edit and then immediately request a load flow.

        Same path as :meth:`_on_bulk_apply` but emits
        :pyattr:`loadflow_requested` on success so the MainWindow
        kicks off the LF.
        """
        # Tag the run so _on_bulk_apply knows to fire the request
        # only when its own pypowsybl call succeeded.
        self._pending_lf_after_apply = True
        try:
            self._on_bulk_apply()
        finally:
            self._pending_lf_after_apply = False

    def _on_bulk_apply(self) -> None:
        component = self._combo.currentText()
        attribute = self._bulk_attr.currentText()
        new_value = self._bulk_value.text()
        ids = self._selected_element_ids()
        if not ids or not attribute or self._network is None:
            return
        try:
            outcome = apply_and_log_bulk_edit(
                self._network, component, ids, attribute, new_value,
                change_log=self._change_log,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Bulk edit rejected",
                f"{component}/{len(ids)} rows/{attribute}\n\n{exc}",
            )
            return
        df = outcome["refreshed_df"]
        cols = [c for c in editable_attributes(component) if c in df.columns]
        self._model.set_dataframe(df, editable_cols=cols)
        self._bulk_value.clear()
        self.bulk_edit_applied.emit(
            component, ids, attribute,
            outcome["display_value"], outcome["prev_map"],
        )
        if getattr(self, "_pending_lf_after_apply", False):
            self.loadflow_requested.emit()
        self._update_bulk_state()

    def _on_edit_requested(self, element_id: str, attribute: str, new_value, previous) -> None:
        component = self._combo.currentText()
        if self._network is None or not is_editable(component, attribute):
            return
        # N-K mode is read-only — silently drop the edit so the
        # PandasTableModel can roll the visible cell back via its
        # rollback hook.
        if self._variant_id != INITIAL_VARIANT_ID:
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
        if self._change_log is not None:
            self._change_log.record(
                component, element_id, attribute, prev, display_value,
            )
        self.edit_applied.emit(component, element_id, attribute, display_value, prev)
