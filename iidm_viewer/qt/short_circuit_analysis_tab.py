"""Short Circuit Analysis tab — PySide6 host.

Ports the Streamlit Short Circuit Analysis tab: the bus-fault list
builder (filtered by nominal voltage), the analysis parameters form,
the runner trigger, and the results overview (per-fault summary +
drill-down with feeder contributions and limit violations).

All pypowsybl work goes through the shared
:mod:`iidm_viewer.short_circuit_analysis` core so the Streamlit,
PySide6 and NiceGUI hosts stay in lockstep.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.qt.data_explorer_tab import PandasTableModel
from iidm_viewer.short_circuit_analysis import (
    FAULT_TYPES,
    STUDY_TYPES,
    build_bus_faults,
    build_summary_dataframe,
    count_failures,
    count_with_violations,
    default_hv_preselect,
    format_fault_type,
    get_nominal_voltages,
    make_sc_params,
    max_fault_power_mva,
    run_short_circuit_analysis,
)


_STATUS_FAIL_BG = QColor(255, 75, 75)
_VIOL_HIGH_BG = QColor(255, 75, 75)
_VIOL_MED_BG = QColor(255, 165, 0)


class _SummaryTableModel(PandasTableModel):
    """``PandasTableModel`` subclass that colours the ``Status`` and
    ``Violations`` columns the same way the Streamlit ``Styler`` does.
    """

    _status_col: Optional[int] = None
    _viol_col: Optional[int] = None

    def set_dataframe(self, df, editable_cols=None) -> None:  # type: ignore[override]
        super().set_dataframe(df, editable_cols=editable_cols)
        self._status_col = None
        self._viol_col = None
        if df is not None and not df.empty:
            cols = list(df.columns)
            if "Status" in cols:
                self._status_col = cols.index("Status")
            if "Violations" in cols:
                self._viol_col = cols.index("Violations")

    def data(self, index, role=Qt.DisplayRole):  # type: ignore[override]
        if role == Qt.BackgroundRole:
            if (
                self._status_col is not None
                and index.column() == self._status_col
            ):
                val = self._df.iat[index.row(), index.column()]
                if val != "CONVERGED":
                    return QBrush(_STATUS_FAIL_BG)
            elif (
                self._viol_col is not None
                and index.column() == self._viol_col
            ):
                try:
                    n = int(self._df.iat[index.row(), index.column()])
                except (TypeError, ValueError):
                    return None
                if n >= 3:
                    return QBrush(_VIOL_HIGH_BG)
                if n > 0:
                    return QBrush(_VIOL_MED_BG)
        return super().data(index, role)


def _multiselect_list(max_height: int = 84) -> QListWidget:
    lst = QListWidget()
    lst.setSelectionMode(QAbstractItemView.MultiSelection)
    lst.setMaximumHeight(max_height)
    return lst


def _selected_floats(lst: QListWidget) -> list[float]:
    out: list[float] = []
    for i in range(lst.count()):
        item = lst.item(i)
        if item.isSelected():
            try:
                out.append(float(item.text()))
            except ValueError:
                continue
    return out


def _fill_voltage_list(lst: QListWidget, voltages: list[float], preselect: set) -> None:
    lst.clear()
    for v in voltages:
        item = QListWidgetItem(f"{v}")
        lst.addItem(item)
        if v in preselect:
            item.setSelected(True)


class ShortCircuitAnalysisTab(QWidget):
    """Tab body. Owns the network handle, the built fault list, and
    the last results dict."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._faults: list[dict] = []
        self._results: Optional[dict] = None
        self._fault_options: list[str] = []
        self._summary_df: pd.DataFrame = pd.DataFrame()

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._placeholder = QLabel(
            "Load a network to run a short circuit analysis."
        )
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #666; padding: 8px;")
        root.addWidget(self._placeholder)

        root.addWidget(self._build_config_group())
        root.addWidget(self._build_params_group())

        run_row = QHBoxLayout()
        self._build_btn = QPushButton("Build fault list")
        self._build_btn.clicked.connect(self._on_build)
        run_row.addWidget(self._build_btn)
        self._run_btn = QPushButton("Run short circuit analysis")
        self._run_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._on_run)
        run_row.addWidget(self._run_btn)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #555;")
        self._status_lbl.setWordWrap(True)
        run_row.addWidget(self._status_lbl, 1)
        self._run_row_widget = QWidget()
        self._run_row_widget.setLayout(run_row)
        root.addWidget(self._run_row_widget)

        root.addWidget(self._build_results_group(), 1)

        self._set_network_loaded(False)

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------
    def _build_config_group(self) -> QGroupBox:
        group = QGroupBox("Fault configuration")
        layout = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(QLabel("Fault type:"))
        self._fault_type_combo = QComboBox()
        for ft in FAULT_TYPES:
            self._fault_type_combo.addItem(format_fault_type(ft), userData=ft)
        row.addWidget(self._fault_type_combo, 1)
        layout.addLayout(row)

        layout.addWidget(QLabel(
            "Filter by nominal voltage (kV) — leave empty to include all:"
        ))
        self._nominal_v_list = _multiselect_list()
        layout.addWidget(self._nominal_v_list)
        self._fault_count_lbl = QLabel("")
        self._fault_count_lbl.setStyleSheet("color: #555;")
        layout.addWidget(self._fault_count_lbl)

        self._config_group = group
        return group

    def _build_params_group(self) -> QGroupBox:
        group = QGroupBox("Analysis parameters")
        layout = QFormLayout(group)

        self._study_combo = QComboBox()
        self._study_combo.addItems(list(STUDY_TYPES))
        self._study_combo.setToolTip(
            "SUB_TRANSIENT uses subtransient reactances (default); "
            "TRANSIENT uses transient reactances."
        )
        layout.addRow("Study type:", self._study_combo)

        self._feeder_chk = QCheckBox("Compute feeder contributions")
        self._feeder_chk.setChecked(True)
        self._feeder_chk.setToolTip(
            "Break down fault current by contributing feeder."
        )
        layout.addRow("", self._feeder_chk)

        self._violations_chk = QCheckBox("Check limit violations")
        self._violations_chk.setChecked(True)
        self._violations_chk.setToolTip(
            "Detect currents exceeding operational limits."
        )
        layout.addRow("", self._violations_chk)

        self._min_drop_spin = QDoubleSpinBox()
        self._min_drop_spin.setRange(0.0, 100.0)
        self._min_drop_spin.setSingleStep(1.0)
        self._min_drop_spin.setSuffix(" %")
        self._min_drop_spin.setToolTip(
            "Only report buses with a voltage drop above this threshold."
        )
        layout.addRow("Min voltage drop:", self._min_drop_spin)

        self._params_group = group
        return group

    def _build_results_group(self) -> QGroupBox:
        group = QGroupBox("Results")
        layout = QVBoxLayout(group)

        metrics_row = QHBoxLayout()
        self._metric_simulated = QLabel("Faults simulated: 0")
        self._metric_failed = QLabel("Failed: 0")
        self._metric_violations = QLabel("With violations: 0")
        for lbl in (
            self._metric_simulated, self._metric_failed, self._metric_violations,
        ):
            lbl.setStyleSheet("padding: 2px 8px; font-weight: bold;")
            metrics_row.addWidget(lbl)
        metrics_row.addStretch(1)
        layout.addLayout(metrics_row)

        # Power-threshold slider mirrors the Streamlit slider.
        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Show faults with fault power ≥"))
        self._pwr_slider = QSlider(Qt.Horizontal)
        self._pwr_slider.setRange(0, 0)
        self._pwr_slider.valueChanged.connect(self._on_pwr_slider_changed)
        slider_row.addWidget(self._pwr_slider, 1)
        self._pwr_slider_value_lbl = QLabel("0 MVA")
        self._pwr_slider_value_lbl.setMinimumWidth(80)
        slider_row.addWidget(self._pwr_slider_value_lbl)
        self._slider_row_widget = QWidget()
        self._slider_row_widget.setLayout(slider_row)
        layout.addWidget(self._slider_row_widget)

        self._summary_view = QTableView()
        self._summary_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._summary_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._summary_view.setMaximumHeight(220)
        self._summary_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._summary_model = _SummaryTableModel()
        self._summary_view.setModel(self._summary_model)
        layout.addWidget(self._summary_view)

        # ----- Drill-down -----
        layout.addWidget(QLabel("Fault detail"))
        drill_row = QHBoxLayout()
        drill_row.addWidget(QLabel("Filter by fault id:"))
        self._fault_filter_edit = QLineEdit()
        self._fault_filter_edit.textChanged.connect(self._on_fault_filter_changed)
        drill_row.addWidget(self._fault_filter_edit, 1)
        drill_row.addWidget(QLabel("Select fault:"))
        self._fault_combo = QComboBox()
        self._fault_combo.currentTextChanged.connect(self._on_fault_selected)
        drill_row.addWidget(self._fault_combo, 1)
        layout.addLayout(drill_row)

        self._detail_status_lbl = QLabel("")
        self._detail_status_lbl.setStyleSheet("padding: 2px 4px; font-weight: bold;")
        layout.addWidget(self._detail_status_lbl)
        detail_metrics = QHBoxLayout()
        self._detail_power_lbl = QLabel("Fault power: —")
        self._detail_current_lbl = QLabel("Fault current: —")
        for lbl in (self._detail_power_lbl, self._detail_current_lbl):
            lbl.setStyleSheet("padding: 2px 8px;")
            detail_metrics.addWidget(lbl)
        detail_metrics.addStretch(1)
        layout.addLayout(detail_metrics)

        layout.addWidget(QLabel("Feeder contributions:"))
        self._feeder_view = QTableView()
        self._feeder_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._feeder_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._feeder_view.setMaximumHeight(140)
        self._feeder_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._feeder_model = PandasTableModel()
        self._feeder_view.setModel(self._feeder_model)
        layout.addWidget(self._feeder_view)

        layout.addWidget(QLabel("Limit violations:"))
        self._violations_view = QTableView()
        self._violations_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._violations_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._violations_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._violations_model = PandasTableModel()
        self._violations_view.setModel(self._violations_model)
        layout.addWidget(self._violations_view, 1)

        self._results_group = group
        return group

    # ------------------------------------------------------------------
    # Public API (mirrors the other Qt tabs).
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._results = None
        self._faults = []
        self._summary_df = pd.DataFrame()
        if network is None:
            self._set_network_loaded(False)
            return
        self._set_network_loaded(True)
        self._status_lbl.setText("")
        self._fault_count_lbl.setText("")
        self._run_btn.setEnabled(False)
        try:
            voltages = get_nominal_voltages(network)
        except Exception:
            voltages = []
        preselect = set(default_hv_preselect(voltages))
        _fill_voltage_list(self._nominal_v_list, voltages, preselect)
        self._render_results()

    # ------------------------------------------------------------------
    # Build + run
    # ------------------------------------------------------------------
    def _on_build(self) -> None:
        if self._network is None:
            return
        fault_type = self._fault_type_combo.currentData() or FAULT_TYPES[0]
        chosen = set(_selected_floats(self._nominal_v_list))
        nominal_v_set = chosen or None
        self._fault_count_lbl.setText("Building fault list…")
        try:
            self._faults = build_bus_faults(
                self._network, nominal_v_set, fault_type,
            )
        except Exception as exc:
            self._fault_count_lbl.setText(f"Build failed: {exc}")
            return
        n = len(self._faults)
        self._fault_count_lbl.setText(
            f"{n} bus fault{'' if n == 1 else 's'} ready.",
        )
        self._run_btn.setEnabled(n > 0)

    def _on_run(self) -> None:
        if self._network is None or not self._faults:
            self._status_lbl.setText("Build a fault list first.")
            return
        sc_params = make_sc_params(
            study_type=self._study_combo.currentText(),
            with_feeder_result=self._feeder_chk.isChecked(),
            with_limit_violations=self._violations_chk.isChecked(),
            min_voltage_drop_percent=self._min_drop_spin.value(),
        )
        n = len(self._faults)
        self._status_lbl.setText(
            f"Running short circuit analysis on {n} fault"
            f"{'' if n == 1 else 's'}…",
        )
        try:
            self._results = run_short_circuit_analysis(
                self._network, self._faults, sc_params,
            )
        except Exception as exc:
            self._status_lbl.setText(f"Short circuit analysis failed: {exc}")
            return
        self._status_lbl.setText(
            f"Done — {n} fault{'' if n == 1 else 's'} analysed.",
        )
        self._render_results()

    # ------------------------------------------------------------------
    # Results rendering
    # ------------------------------------------------------------------
    def _set_network_loaded(self, loaded: bool) -> None:
        self._placeholder.setVisible(not loaded)
        self._config_group.setVisible(loaded)
        self._params_group.setVisible(loaded)
        self._run_row_widget.setVisible(loaded)
        self._results_group.setVisible(loaded and self._results is not None)

    def _render_results(self) -> None:
        results = self._results
        if results is None:
            self._results_group.setVisible(False)
            self._summary_model.set_dataframe(pd.DataFrame())
            self._feeder_model.set_dataframe(pd.DataFrame())
            self._violations_model.set_dataframe(pd.DataFrame())
            self._fault_combo.clear()
            self._fault_options = []
            return
        self._results_group.setVisible(True)
        self._summary_df = build_summary_dataframe(results)
        faults: list[dict] = results.get("faults", [])
        self._metric_simulated.setText(f"Faults simulated: {len(faults)}")
        self._metric_failed.setText(
            f"Failed: {count_failures(self._summary_df)}",
        )
        self._metric_violations.setText(
            f"With violations: {count_with_violations(self._summary_df)}",
        )
        # Power slider range — int MVA, since QSlider only takes ints.
        max_pwr = int(round(max_fault_power_mva(self._summary_df)))
        self._pwr_slider.blockSignals(True)
        self._pwr_slider.setRange(0, max(max_pwr, 1))
        self._pwr_slider.setValue(0)
        self._pwr_slider.blockSignals(False)
        self._slider_row_widget.setVisible(max_pwr > 0)
        self._pwr_slider_value_lbl.setText("0 MVA")
        self._apply_pwr_filter()
        self._summary_view.resizeColumnsToContents()
        # Drill-down combo — populated from the canonical fault list so
        # the order matches the configuration tab.
        self._fault_options = [f["id"] for f in faults]
        self._refresh_fault_combo()

    def _on_pwr_slider_changed(self, value: int) -> None:
        self._pwr_slider_value_lbl.setText(f"{value} MVA")
        self._apply_pwr_filter()

    def _apply_pwr_filter(self) -> None:
        if self._summary_df.empty:
            self._summary_model.set_dataframe(pd.DataFrame())
            return
        threshold = float(self._pwr_slider.value())
        if threshold <= 0:
            self._summary_model.set_dataframe(self._summary_df)
            return
        df = self._summary_df
        mask = df["Fault power (MVA)"].isna() | (
            df["Fault power (MVA)"] >= threshold
        )
        self._summary_model.set_dataframe(df[mask].reset_index(drop=True))

    def _on_fault_filter_changed(self, _text: str) -> None:
        self._refresh_fault_combo()

    def _refresh_fault_combo(self) -> None:
        sub = self._fault_filter_edit.text().strip().lower()
        if sub:
            opts = [fid for fid in self._fault_options if sub in fid.lower()]
        else:
            opts = list(self._fault_options)
        self._fault_combo.blockSignals(True)
        self._fault_combo.clear()
        self._fault_combo.addItems(opts)
        self._fault_combo.blockSignals(False)
        if opts:
            self._on_fault_selected(opts[0])
        else:
            self._render_fault_detail(None)

    def _on_fault_selected(self, fid: str) -> None:
        if not fid:
            self._render_fault_detail(None)
            return
        self._render_fault_detail(fid)

    def _render_fault_detail(self, fid: Optional[str]) -> None:
        if not fid or self._results is None:
            self._detail_status_lbl.setText("")
            self._detail_power_lbl.setText("Fault power: —")
            self._detail_current_lbl.setText("Fault current: —")
            self._feeder_model.set_dataframe(pd.DataFrame())
            self._violations_model.set_dataframe(pd.DataFrame())
            return
        fr = self._results.get("fault_results", {}).get(fid, {})
        status = fr.get("status", "UNKNOWN")
        self._detail_status_lbl.setText(f"Status: {status}")
        self._detail_status_lbl.setStyleSheet(
            "padding: 2px 4px; font-weight: bold; color: "
            + ("green" if status == "CONVERGED" else "red")
            + ";"
        )
        pwr = fr.get("short_circuit_power_mva")
        cur = fr.get("current_kA")
        self._detail_power_lbl.setText(
            f"Fault power: {pwr:.1f} MVA" if pwr is not None
            else "Fault power: —",
        )
        self._detail_current_lbl.setText(
            f"Fault current: {cur:.3f} kA" if cur is not None
            else "Fault current: —",
        )
        feeder_df = fr.get("feeder_results", pd.DataFrame())
        viol_df = fr.get("limit_violations", pd.DataFrame())
        self._feeder_model.set_dataframe(feeder_df)
        self._violations_model.set_dataframe(viol_df)
        self._feeder_view.resizeColumnsToContents()
        self._violations_view.resizeColumnsToContents()
