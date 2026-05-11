"""Data Explorer Components tab — read-only DataFrame viewer.

Minimal first cut: a combo box to pick the component type, and a
``QTableView`` showing the pandas DataFrame ``pypowsybl`` returns for
that getter. No filtering, no editing — those are next-iteration
features.

The PySide6 reason-to-exist: this tab in Streamlit (``data_explorer.py``,
1384 LOC with ``st.data_editor``) is one of the slowest interactions
because every cell change reruns the whole script. With a
``QAbstractTableModel`` backed by pandas there is no rerun — only the
two cells the user touched get repainted.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.powsybl_worker import NetworkProxy, run


# Subset of ``network_info.COMPONENT_TYPES`` (kept local to avoid pulling
# the Streamlit-importing module into the Qt path). Matches the Streamlit
# tab's options 1:1.
COMPONENT_GETTERS: dict[str, str] = {
    "Substations": "get_substations",
    "Voltage Levels": "get_voltage_levels",
    "Buses": "get_buses",
    "Busbar Sections": "get_busbar_sections",
    "Generators": "get_generators",
    "Loads": "get_loads",
    "Lines": "get_lines",
    "2-Winding Transformers": "get_2_windings_transformers",
    "3-Winding Transformers": "get_3_windings_transformers",
    "Switches": "get_switches",
    "Shunt Compensators": "get_shunt_compensators",
    "Static VAR Compensators": "get_static_var_compensators",
    "HVDC Lines": "get_hvdc_lines",
    "VSC Converter Stations": "get_vsc_converter_stations",
    "LCC Converter Stations": "get_lcc_converter_stations",
    "Batteries": "get_batteries",
    "Dangling Lines": "get_dangling_lines",
    "Tie Lines": "get_tie_lines",
}


def _fetch_dataframe(network: NetworkProxy, getter: str) -> pd.DataFrame:
    """Call ``network.<getter>()`` on the pypowsybl worker thread.

    Bypasses ``NetworkProxy.__getattr__`` so the whole call (resolve
    method, invoke it, materialise the DataFrame) runs in a single
    worker hop instead of two.
    """
    raw = object.__getattribute__(network, "_obj")

    def _do() -> pd.DataFrame:
        method = getattr(raw, getter, None)
        if method is None:
            return pd.DataFrame()
        df = method()
        # pypowsybl returns the equipment id as the index; surfacing it
        # as a regular column makes the table read like the Streamlit one.
        if df is not None and df.index.name:
            df = df.reset_index()
        return df if df is not None else pd.DataFrame()

    return run(_do)


class PandasTableModel(QAbstractTableModel):
    """Read-only Qt model over a pandas DataFrame.

    Standard pattern (no third-party deps). Replacing the whole frame
    via :meth:`set_dataframe` issues a single ``beginResetModel`` /
    ``endResetModel`` cycle, which is cheap regardless of row count.
    """

    def __init__(self, df: Optional[pd.DataFrame] = None, parent=None) -> None:
        super().__init__(parent)
        self._df: pd.DataFrame = df if df is not None else pd.DataFrame()

    def set_dataframe(self, df: pd.DataFrame) -> None:
        self.beginResetModel()
        self._df = df if df is not None else pd.DataFrame()
        self.endResetModel()

    def dataframe(self) -> pd.DataFrame:
        return self._df

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return int(self._df.shape[0])

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return int(self._df.shape[1])

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        row, col = index.row(), index.column()
        try:
            value = self._df.iat[row, col]
        except (IndexError, KeyError):
            return None
        # NaN -> em-dash for parity with the Streamlit tables.
        if isinstance(value, float) and pd.isna(value):
            return "—"
        if isinstance(value, float):
            return format(value, ".4g")
        return str(value)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            try:
                return str(self._df.columns[section])
            except IndexError:
                return None
        # Vertical header: row number, 1-based for readability.
        return str(section + 1)


class DataExplorerTab(QWidget):
    """Pick a component type, view its DataFrame."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None

        self._combo = QComboBox()
        for label in COMPONENT_GETTERS:
            self._combo.addItem(label)
        self._combo.currentTextChanged.connect(self._on_component_changed)

        self._summary = QLabel("Load a network to inspect its components.")
        self._summary.setStyleSheet("padding: 6px 10px; color: #444;")

        self._model = PandasTableModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)  # sorting would need a proxy model — skip for the MVP
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setEditTriggers(QTableView.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setDefaultSectionSize(22)

        controls = QHBoxLayout()
        controls.setContentsMargins(10, 6, 10, 0)
        controls.addWidget(QLabel("Component:"))
        controls.addWidget(self._combo, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(controls)
        layout.addWidget(self._summary)
        layout.addWidget(self._table, 1)

    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        if network is None:
            self._model.set_dataframe(pd.DataFrame())
            self._summary.setText("No network loaded.")
            return
        self._refresh(self._combo.currentText())

    def _on_component_changed(self, label: str) -> None:
        if self._network is None or not label:
            return
        self._refresh(label)

    def _refresh(self, label: str) -> None:
        getter = COMPONENT_GETTERS.get(label)
        if getter is None or self._network is None:
            return
        try:
            df = _fetch_dataframe(self._network, getter)
        except Exception as exc:  # pypowsybl getters can raise on absent extensions
            self._model.set_dataframe(pd.DataFrame())
            self._summary.setText(f"{label}: failed — {exc}")
            return
        self._model.set_dataframe(df)
        if df.empty:
            self._summary.setText(f"{label}: empty (no rows in this network)")
        else:
            self._summary.setText(f"{label}: {df.shape[0]} rows · {df.shape[1]} columns")
        # First-time column sizing — only on populated frames, otherwise
        # ``resizeColumnsToContents`` walks zero cells.
        if not df.empty:
            self._table.resizeColumnsToContents()
