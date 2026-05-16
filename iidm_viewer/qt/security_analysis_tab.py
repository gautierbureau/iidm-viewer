"""Security Analysis tab — PySide6 host.

A focused port of the Streamlit Security Analysis tab: the automatic
N-1 / N-2 contingency builder, an AC run, and a results overview. The
advanced configuration (monitored elements, limit reductions, remedial
actions, operator strategies, JSON import) stays Streamlit-only for
now — those are heavily form-driven.

All pypowsybl work goes through the shared
:mod:`iidm_viewer.security_analysis` core so this tab, the Streamlit
tab and the NiceGUI tab stay in lockstep.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.qt.data_explorer_tab import PandasTableModel
from iidm_viewer.security_analysis import (
    AUTO_MODES,
    ELEMENT_TYPES,
    build_n1_contingencies,
    build_n2_contingencies,
    get_nominal_voltages,
    run_security_analysis,
    summarize_security_results,
)


class SecurityAnalysisTab(QWidget):
    """Tab body. Owns the network handle + the last results dict."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._results: Optional[dict] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._placeholder = QLabel("Load a network to run a security analysis.")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #666; padding: 8px;")
        root.addWidget(self._placeholder)

        # --- Configuration ------------------------------------------------
        self._config_group = QGroupBox("Contingency configuration")
        cfg = QVBoxLayout(self._config_group)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(list(AUTO_MODES))
        mode_row.addWidget(self._mode_combo)
        mode_row.addSpacing(16)
        mode_row.addWidget(QLabel("Element type:"))
        self._element_combo = QComboBox()
        self._element_combo.addItems(list(ELEMENT_TYPES))
        mode_row.addWidget(self._element_combo, 1)
        cfg.addLayout(mode_row)

        cfg.addWidget(QLabel("Nominal voltage filter (optional, none = all):"))
        self._nominal_v_list = QListWidget()
        self._nominal_v_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self._nominal_v_list.setMaximumHeight(96)
        cfg.addWidget(self._nominal_v_list)

        run_row = QHBoxLayout()
        self._run_btn = QPushButton("Run security analysis")
        self._run_btn.clicked.connect(self._on_run)
        run_row.addWidget(self._run_btn)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #555;")
        self._status_lbl.setWordWrap(True)
        run_row.addWidget(self._status_lbl, 1)
        cfg.addLayout(run_row)
        root.addWidget(self._config_group)

        # --- Results ------------------------------------------------------
        self._results_group = QGroupBox("Results")
        res = QVBoxLayout(self._results_group)
        self._pre_status_lbl = QLabel("")
        self._pre_status_lbl.setStyleSheet("padding: 2px 4px; font-weight: bold;")
        res.addWidget(self._pre_status_lbl)
        res.addWidget(QLabel("Per-contingency summary:"))
        self._summary_view = QTableView()
        self._summary_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._summary_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._summary_view.setMaximumHeight(200)
        self._summary_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._summary_model = PandasTableModel()
        self._summary_view.setModel(self._summary_model)
        res.addWidget(self._summary_view)
        res.addWidget(QLabel("Limit violations:"))
        self._violations_view = QTableView()
        self._violations_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._violations_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._violations_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._violations_model = PandasTableModel()
        self._violations_view.setModel(self._violations_model)
        res.addWidget(self._violations_view, 1)
        root.addWidget(self._results_group, 1)

        self._set_network_loaded(False)

    # ------------------------------------------------------------------
    # Public API (mirrors the other Qt tabs).
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._results = None
        if network is None:
            self._set_network_loaded(False)
            return
        self._set_network_loaded(True)
        self._status_lbl.setText("")
        # Repopulate the nominal-voltage filter.
        self._nominal_v_list.clear()
        try:
            voltages = get_nominal_voltages(network)
        except Exception:
            voltages = []
        for v in voltages:
            self._nominal_v_list.addItem(QListWidgetItem(str(v)))
        self._render_results()

    def refresh(self) -> None:
        """Re-render the results view (e.g. after a load flow)."""
        self._render_results()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _set_network_loaded(self, loaded: bool) -> None:
        self._placeholder.setVisible(not loaded)
        self._config_group.setVisible(loaded)
        self._results_group.setVisible(loaded and self._results is not None)

    def _selected_nominal_v(self) -> Optional[set]:
        chosen = {
            float(self._nominal_v_list.item(i).text())
            for i in range(self._nominal_v_list.count())
            if self._nominal_v_list.item(i).isSelected()
        }
        return chosen or None

    def _on_run(self) -> None:
        if self._network is None:
            return
        mode = self._mode_combo.currentText()
        element_type = self._element_combo.currentText()
        nominal_v_set = self._selected_nominal_v()
        builder = (
            build_n1_contingencies if mode == "N-1" else build_n2_contingencies
        )
        self._status_lbl.setText("Building contingencies…")
        try:
            contingencies = builder(self._network, element_type, nominal_v_set)
        except Exception as exc:
            self._status_lbl.setText(f"Contingency build failed: {exc}")
            return
        if not contingencies:
            self._status_lbl.setText(
                "No contingencies for this element type / voltage filter.",
            )
            self._results = None
            self._render_results()
            return
        n = len(contingencies)
        self._status_lbl.setText(
            f"Running AC security analysis on {n} "
            f"contingenc{'y' if n == 1 else 'ies'}…",
        )
        try:
            self._results = run_security_analysis(self._network, contingencies)
        except Exception as exc:
            self._status_lbl.setText(f"Security analysis failed: {exc}")
            return
        self._status_lbl.setText(
            f"Done — {n} contingenc{'y' if n == 1 else 'ies'} analysed.",
        )
        self._render_results()

    def _render_results(self) -> None:
        results = self._results
        if results is None:
            self._results_group.setVisible(False)
            self._summary_model.set_dataframe(pd.DataFrame())
            self._violations_model.set_dataframe(pd.DataFrame())
            return
        self._results_group.setVisible(True)
        self._pre_status_lbl.setText(
            f"Pre-contingency load flow: {results.get('pre_status', '?')}",
        )
        summary = summarize_security_results(results)
        self._summary_model.set_dataframe(summary)
        self._summary_view.resizeColumnsToContents()
        # Concatenate every post-contingency violation frame, tagged
        # with its contingency id.
        frames = []
        for cid, cr in (results.get("post") or {}).items():
            viol = cr.get("limit_violations")
            if viol is not None and not viol.empty:
                tagged = viol.copy()
                tagged.insert(0, "contingency_id", cid)
                frames.append(tagged)
        if frames:
            all_viol = pd.concat(frames, ignore_index=True)
        else:
            all_viol = pd.DataFrame()
        self._violations_model.set_dataframe(all_viol)
        self._violations_view.resizeColumnsToContents()
