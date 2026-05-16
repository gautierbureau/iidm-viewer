"""Security Analysis tab — PySide6 host.

Ports the Streamlit Security Analysis tab: the automatic N-1 / N-2
contingency builder, the advanced configuration (monitored elements,
limit reductions, remedial actions, operator strategies), an AC run
and a results overview. JSON import stays Streamlit-only — file
upload is host-specific.

All pypowsybl work + the config builders / validators go through the
shared :mod:`iidm_viewer.security_analysis` core so this tab, the
Streamlit tab and the NiceGUI tab stay in lockstep.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from PySide6.QtCore import Qt
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
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.qt.data_explorer_tab import PandasTableModel
from iidm_viewer.security_analysis import (
    ACTION_FIELDS,
    AUTO_MODES,
    CONDITION_TYPES,
    CTX_TYPES,
    ELEMENT_TYPES,
    VIOLATION_TYPES,
    action_summary,
    build_n1_contingencies,
    build_n2_contingencies,
    get_element_ids,
    get_nominal_voltages,
    limit_reduction_summary,
    make_action,
    make_limit_reduction,
    make_monitored_element,
    make_operator_strategy,
    monitored_element_summary,
    operator_strategy_summary,
    run_security_analysis,
    summarize_security_results,
    validate_action,
    validate_limit_reduction,
    validate_monitored_element,
    validate_operator_strategy,
)


def _multiselect_list(max_height: int = 84) -> QListWidget:
    lst = QListWidget()
    lst.setSelectionMode(QAbstractItemView.MultiSelection)
    lst.setMaximumHeight(max_height)
    return lst


def _selected_texts(lst: QListWidget) -> list[str]:
    return [
        lst.item(i).text()
        for i in range(lst.count())
        if lst.item(i).isSelected()
    ]


def _fill_list(lst: QListWidget, items) -> None:
    lst.clear()
    for it in items:
        lst.addItem(QListWidgetItem(str(it)))


class SecurityAnalysisTab(QWidget):
    """Tab body. Owns the network handle, the built contingency list,
    the advanced-config entry lists, and the last results dict."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._results: Optional[dict] = None
        self._contingencies: list[dict] = []
        self._element_ids: dict = {}
        self._monitored: list[dict] = []
        self._reductions: list[dict] = []
        self._actions: list[dict] = []
        self._strategies: list[dict] = []
        # Registry-driven action widgets: {field_name: (fdef, widget)}.
        self._action_field_widgets: dict = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._placeholder = QLabel("Load a network to run a security analysis.")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #666; padding: 8px;")
        root.addWidget(self._placeholder)

        root.addWidget(self._build_config_group())
        root.addWidget(self._build_advanced_group())

        run_row = QHBoxLayout()
        self._run_btn = QPushButton("Run security analysis")
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
        group = QGroupBox("Contingency configuration")
        layout = QVBoxLayout(group)
        row = QHBoxLayout()
        row.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(list(AUTO_MODES))
        row.addWidget(self._mode_combo)
        row.addSpacing(12)
        row.addWidget(QLabel("Element type:"))
        self._element_combo = QComboBox()
        self._element_combo.addItems(list(ELEMENT_TYPES))
        row.addWidget(self._element_combo, 1)
        layout.addLayout(row)

        layout.addWidget(QLabel("Nominal voltage filter (optional, none = all):"))
        self._nominal_v_list = _multiselect_list()
        layout.addWidget(self._nominal_v_list)

        build_row = QHBoxLayout()
        self._build_btn = QPushButton("Build contingency list")
        self._build_btn.clicked.connect(self._on_build)
        build_row.addWidget(self._build_btn)
        self._contingency_count_lbl = QLabel("")
        self._contingency_count_lbl.setStyleSheet("color: #555;")
        build_row.addWidget(self._contingency_count_lbl, 1)
        layout.addLayout(build_row)
        self._config_group = group
        return group

    def _build_advanced_group(self) -> QGroupBox:
        group = QGroupBox("Advanced configuration")
        group.setCheckable(True)
        group.setChecked(False)
        layout = QVBoxLayout(group)
        layout.addWidget(self._build_monitored_group())
        layout.addWidget(self._build_reductions_group())
        layout.addWidget(self._build_actions_group())
        layout.addWidget(self._build_strategies_group())
        self._advanced_group = group
        return group

    def _build_monitored_group(self) -> QGroupBox:
        group = QGroupBox("Monitored elements")
        layout = QVBoxLayout(group)
        ctx_row = QHBoxLayout()
        ctx_row.addWidget(QLabel("Context:"))
        self._mon_ctx_combo = QComboBox()
        self._mon_ctx_combo.addItems(list(CTX_TYPES))
        ctx_row.addWidget(self._mon_ctx_combo)
        ctx_row.addStretch(1)
        layout.addLayout(ctx_row)
        cols = QHBoxLayout()
        for label, attr in (
            ("Contingencies (SPECIFIC)", "_mon_cids_list"),
            ("Branches", "_mon_branches_list"),
            ("Voltage levels", "_mon_vls_list"),
            ("3WTs", "_mon_3wt_list"),
        ):
            col = QVBoxLayout()
            col.addWidget(QLabel(label))
            lst = _multiselect_list()
            setattr(self, attr, lst)
            col.addWidget(lst)
            cols.addLayout(col)
        layout.addLayout(cols)
        add_btn = QPushButton("Add monitored rule")
        add_btn.clicked.connect(self._on_add_monitored)
        layout.addWidget(add_btn)
        self._mon_entries_list = QListWidget()
        self._mon_entries_list.setMaximumHeight(96)
        layout.addWidget(self._mon_entries_list)
        rm_btn = QPushButton("Remove selected rule")
        rm_btn.clicked.connect(self._on_remove_monitored)
        layout.addWidget(rm_btn)
        return group

    def _build_reductions_group(self) -> QGroupBox:
        group = QGroupBox("Limit reductions")
        layout = QVBoxLayout(group)
        row = QHBoxLayout()
        row.addWidget(QLabel("Value (0–1):"))
        self._lr_value = QDoubleSpinBox()
        self._lr_value.setRange(0.0, 1.0)
        self._lr_value.setSingleStep(0.05)
        self._lr_value.setValue(0.9)
        row.addWidget(self._lr_value)
        self._lr_perm = QCheckBox("Permanent")
        self._lr_perm.setChecked(True)
        self._lr_temp = QCheckBox("Temporary")
        self._lr_temp.setChecked(True)
        row.addWidget(self._lr_perm)
        row.addWidget(self._lr_temp)
        row.addStretch(1)
        layout.addLayout(row)
        add_btn = QPushButton("Add limit reduction")
        add_btn.clicked.connect(self._on_add_reduction)
        layout.addWidget(add_btn)
        self._lr_entries_list = QListWidget()
        self._lr_entries_list.setMaximumHeight(80)
        layout.addWidget(self._lr_entries_list)
        rm_btn = QPushButton("Remove selected reduction")
        rm_btn.clicked.connect(self._on_remove_reduction)
        layout.addWidget(rm_btn)
        return group

    def _build_actions_group(self) -> QGroupBox:
        group = QGroupBox("Remedial actions")
        layout = QVBoxLayout(group)
        row = QHBoxLayout()
        row.addWidget(QLabel("Type:"))
        self._act_type_combo = QComboBox()
        self._act_type_combo.addItems(list(ACTION_FIELDS))
        self._act_type_combo.currentTextChanged.connect(
            self._rebuild_action_fields,
        )
        row.addWidget(self._act_type_combo)
        row.addWidget(QLabel("Action ID:"))
        self._act_id_edit = QLineEdit()
        row.addWidget(self._act_id_edit, 1)
        layout.addLayout(row)
        # Registry-driven, type-specific field form.
        self._act_fields_box = QWidget()
        self._act_fields_layout = QFormLayout(self._act_fields_box)
        self._act_fields_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._act_fields_box)
        add_btn = QPushButton("Add action")
        add_btn.clicked.connect(self._on_add_action)
        layout.addWidget(add_btn)
        self._act_entries_list = QListWidget()
        self._act_entries_list.setMaximumHeight(96)
        layout.addWidget(self._act_entries_list)
        rm_btn = QPushButton("Remove selected action")
        rm_btn.clicked.connect(self._on_remove_action)
        layout.addWidget(rm_btn)
        return group

    def _build_strategies_group(self) -> QGroupBox:
        group = QGroupBox("Operator strategies")
        layout = QVBoxLayout(group)
        row = QHBoxLayout()
        row.addWidget(QLabel("Strategy ID:"))
        self._strat_id_edit = QLineEdit()
        row.addWidget(self._strat_id_edit)
        row.addWidget(QLabel("Triggered by:"))
        self._strat_cid_combo = QComboBox()
        row.addWidget(self._strat_cid_combo, 1)
        layout.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Condition:"))
        self._strat_condition_combo = QComboBox()
        self._strat_condition_combo.addItems(list(CONDITION_TYPES))
        row2.addWidget(self._strat_condition_combo)
        row2.addStretch(1)
        layout.addLayout(row2)
        cols = QHBoxLayout()
        for label, attr in (
            ("Actions to apply", "_strat_actions_list"),
            ("Violation types (optional)", "_strat_vtypes_list"),
        ):
            col = QVBoxLayout()
            col.addWidget(QLabel(label))
            lst = _multiselect_list()
            setattr(self, attr, lst)
            col.addWidget(lst)
            cols.addLayout(col)
        layout.addLayout(cols)
        _fill_list(self._strat_vtypes_list, VIOLATION_TYPES)
        add_btn = QPushButton("Add strategy")
        add_btn.clicked.connect(self._on_add_strategy)
        layout.addWidget(add_btn)
        self._strat_entries_list = QListWidget()
        self._strat_entries_list.setMaximumHeight(96)
        layout.addWidget(self._strat_entries_list)
        rm_btn = QPushButton("Remove selected strategy")
        rm_btn.clicked.connect(self._on_remove_strategy)
        layout.addWidget(rm_btn)
        return group

    def _build_results_group(self) -> QGroupBox:
        group = QGroupBox("Results")
        layout = QVBoxLayout(group)
        self._pre_status_lbl = QLabel("")
        self._pre_status_lbl.setStyleSheet("padding: 2px 4px; font-weight: bold;")
        layout.addWidget(self._pre_status_lbl)
        layout.addWidget(QLabel("Per-contingency summary:"))
        self._summary_view = QTableView()
        self._summary_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._summary_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._summary_view.setMaximumHeight(180)
        self._summary_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._summary_model = PandasTableModel()
        self._summary_view.setModel(self._summary_model)
        layout.addWidget(self._summary_view)
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
        self._contingencies = []
        self._monitored = []
        self._reductions = []
        self._actions = []
        self._strategies = []
        if network is None:
            self._set_network_loaded(False)
            return
        self._set_network_loaded(True)
        self._status_lbl.setText("")
        self._contingency_count_lbl.setText("")
        self._run_btn.setEnabled(False)
        # Nominal-voltage filter.
        try:
            voltages = get_nominal_voltages(network)
        except Exception:
            voltages = []
        _fill_list(self._nominal_v_list, voltages)
        # Element-id buckets for the advanced-config selectors.
        try:
            self._element_ids = get_element_ids(network)
        except Exception:
            self._element_ids = {}
        _fill_list(self._mon_branches_list,
                   self._element_ids.get("branches", []))
        _fill_list(self._mon_vls_list,
                   self._element_ids.get("voltage_levels", []))
        _fill_list(self._mon_3wt_list,
                   self._element_ids.get("three_windings_transformers", []))
        self._rebuild_action_fields()
        self._sync_contingency_dependents()
        self._refresh_all_entry_lists()
        self._render_results()

    def refresh(self) -> None:
        """Re-render the results view (e.g. after a load flow)."""
        self._render_results()

    # ------------------------------------------------------------------
    # Contingency build + run
    # ------------------------------------------------------------------
    def _on_build(self) -> None:
        if self._network is None:
            return
        mode = self._mode_combo.currentText()
        element_type = self._element_combo.currentText()
        chosen = {
            float(t) for t in _selected_texts(self._nominal_v_list)
        }
        nominal_v_set = chosen or None
        builder = (
            build_n1_contingencies if mode == "N-1" else build_n2_contingencies
        )
        self._contingency_count_lbl.setText("Building contingencies…")
        try:
            self._contingencies = builder(
                self._network, element_type, nominal_v_set,
            )
        except Exception as exc:
            self._contingency_count_lbl.setText(f"Build failed: {exc}")
            return
        n = len(self._contingencies)
        self._contingency_count_lbl.setText(
            f"{n} contingenc{'y' if n == 1 else 'ies'} ready.",
        )
        self._run_btn.setEnabled(n > 0)
        self._sync_contingency_dependents()

    def _on_run(self) -> None:
        if self._network is None or not self._contingencies:
            self._status_lbl.setText("Build a contingency list first.")
            return
        n = len(self._contingencies)
        self._status_lbl.setText(
            f"Running AC security analysis on {n} "
            f"contingenc{'y' if n == 1 else 'ies'}…",
        )
        try:
            self._results = run_security_analysis(
                self._network,
                self._contingencies,
                monitored_elements=self._monitored,
                limit_reductions=self._reductions,
                actions=self._actions,
                operator_strategies=self._strategies,
            )
        except Exception as exc:
            self._status_lbl.setText(f"Security analysis failed: {exc}")
            return
        self._status_lbl.setText(
            f"Done — {n} contingenc{'y' if n == 1 else 'ies'} analysed.",
        )
        self._render_results()

    # ------------------------------------------------------------------
    # Advanced-config handlers
    # ------------------------------------------------------------------
    def _sync_contingency_dependents(self) -> None:
        """Feed the built contingency ids into the monitored + strategy
        pickers."""
        cids = [c["id"] for c in self._contingencies]
        _fill_list(self._mon_cids_list, cids)
        self._strat_cid_combo.clear()
        self._strat_cid_combo.addItems(cids)

    def _rebuild_action_fields(self) -> None:
        """Rebuild the type-specific action field form from ACTION_FIELDS."""
        while self._act_fields_layout.count():
            item = self._act_fields_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._action_field_widgets = {}
        spec = ACTION_FIELDS.get(self._act_type_combo.currentText())
        if spec is None:
            return
        ids = self._element_ids.get(spec["id_key"]) or []
        for fdef in spec["fields"]:
            kind = fdef["kind"]
            if kind == "id":
                w = QComboBox()
                w.addItems([str(x) for x in ids])
            elif kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(fdef.get("default", False)))
            elif kind == "choice":
                w = QComboBox()
                w.addItems(list(fdef["options"]))
                if fdef.get("default") in fdef["options"]:
                    w.setCurrentText(fdef["default"])
            elif kind == "int":
                w = QSpinBox()
                w.setRange(-1_000_000, 1_000_000)
                w.setValue(int(fdef.get("default", 0)))
            else:  # float
                w = QDoubleSpinBox()
                w.setRange(-1e9, 1e9)
                w.setDecimals(2)
                w.setValue(float(fdef.get("default", 0.0)))
            self._action_field_widgets[fdef["name"]] = (fdef, w)
            self._act_fields_layout.addRow(fdef["label"], w)

    def _read_action_fields(self) -> dict:
        out: dict = {}
        for name, (fdef, w) in self._action_field_widgets.items():
            kind = fdef["kind"]
            if kind == "id" or kind == "choice":
                out[name] = w.currentText()
            elif kind == "bool":
                out[name] = w.isChecked()
            elif kind == "int":
                out[name] = int(w.value())
            else:  # float
                out[name] = float(w.value())
        return out

    def _on_add_monitored(self) -> None:
        ctx = self._mon_ctx_combo.currentText()
        cids = _selected_texts(self._mon_cids_list)
        branches = _selected_texts(self._mon_branches_list)
        vls = _selected_texts(self._mon_vls_list)
        t3w = _selected_texts(self._mon_3wt_list)
        errors = validate_monitored_element(ctx, cids, branches, vls, t3w)
        if errors:
            self._status_lbl.setText("; ".join(errors))
            return
        self._monitored.append(
            make_monitored_element(ctx, cids, branches, vls, t3w),
        )
        self._refresh_all_entry_lists()

    def _on_remove_monitored(self) -> None:
        self._remove_selected(self._mon_entries_list, self._monitored)

    def _on_add_reduction(self) -> None:
        errors = validate_limit_reduction(
            self._lr_value.value(),
            self._lr_perm.isChecked(),
            self._lr_temp.isChecked(),
        )
        if errors:
            self._status_lbl.setText("; ".join(errors))
            return
        self._reductions.append(make_limit_reduction(
            self._lr_value.value(),
            self._lr_perm.isChecked(),
            self._lr_temp.isChecked(),
        ))
        self._refresh_all_entry_lists()

    def _on_remove_reduction(self) -> None:
        self._remove_selected(self._lr_entries_list, self._reductions)

    def _on_add_action(self) -> None:
        atype = self._act_type_combo.currentText()
        aid = self._act_id_edit.text()
        fields = self._read_action_fields()
        existing = [a["action_id"] for a in self._actions]
        errors = validate_action(atype, aid, fields, existing)
        if errors:
            self._status_lbl.setText("; ".join(errors))
            return
        self._actions.append(make_action(atype, aid, fields))
        self._act_id_edit.clear()
        self._refresh_all_entry_lists()

    def _on_remove_action(self) -> None:
        rows = sorted(
            (self._act_entries_list.row(i)
             for i in self._act_entries_list.selectedItems()),
            reverse=True,
        )
        for r in rows:
            removed = self._actions.pop(r)
            for s in self._strategies:
                s["action_ids"] = [
                    a for a in s["action_ids"]
                    if a != removed["action_id"]
                ]
        self._refresh_all_entry_lists()

    def _on_add_strategy(self) -> None:
        sid = self._strat_id_edit.text()
        actions = _selected_texts(self._strat_actions_list)
        existing = [s["operator_strategy_id"] for s in self._strategies]
        errors = validate_operator_strategy(sid, actions, existing)
        cid = self._strat_cid_combo.currentText()
        if not cid:
            errors = errors + ["Pick a triggering contingency."]
        if errors:
            self._status_lbl.setText("; ".join(errors))
            return
        self._strategies.append(make_operator_strategy(
            sid, cid, actions,
            self._strat_condition_combo.currentText(),
            [], _selected_texts(self._strat_vtypes_list),
        ))
        self._strat_id_edit.clear()
        self._refresh_all_entry_lists()

    def _on_remove_strategy(self) -> None:
        self._remove_selected(self._strat_entries_list, self._strategies)

    def _remove_selected(self, list_widget: QListWidget, entries: list) -> None:
        rows = sorted(
            (list_widget.row(i) for i in list_widget.selectedItems()),
            reverse=True,
        )
        for r in rows:
            entries.pop(r)
        self._refresh_all_entry_lists()

    def _refresh_all_entry_lists(self) -> None:
        """Re-render every advanced-config entry list + the strategy
        action picker (which depends on the action list)."""
        _fill_list(
            self._mon_entries_list,
            [monitored_element_summary(e) for e in self._monitored],
        )
        _fill_list(
            self._lr_entries_list,
            [limit_reduction_summary(e) for e in self._reductions],
        )
        _fill_list(
            self._act_entries_list,
            [action_summary(e) for e in self._actions],
        )
        _fill_list(
            self._strat_entries_list,
            [operator_strategy_summary(e) for e in self._strategies],
        )
        # The strategy action picker mirrors the current action list.
        action_ids = [a["action_id"] for a in self._actions]
        previously = set(_selected_texts(self._strat_actions_list))
        _fill_list(self._strat_actions_list, action_ids)
        for i in range(self._strat_actions_list.count()):
            item = self._strat_actions_list.item(i)
            if item.text() in previously:
                item.setSelected(True)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    def _set_network_loaded(self, loaded: bool) -> None:
        self._placeholder.setVisible(not loaded)
        self._config_group.setVisible(loaded)
        self._advanced_group.setVisible(loaded)
        self._run_row_widget.setVisible(loaded)
        self._results_group.setVisible(loaded and self._results is not None)

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
        self._summary_model.set_dataframe(summarize_security_results(results))
        self._summary_view.resizeColumnsToContents()
        frames = []
        for cid, cr in (results.get("post") or {}).items():
            viol = cr.get("limit_violations")
            if viol is not None and not viol.empty:
                tagged = viol.copy()
                tagged.insert(0, "contingency_id", cid)
                frames.append(tagged)
        all_viol = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        self._violations_model.set_dataframe(all_viol)
        self._violations_view.resizeColumnsToContents()
