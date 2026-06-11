"""N-K Variant dock — PySide6 host.

Composes the host-agnostic :func:`security_analysis.normalize_manual_contingency`
picker with PySide6 widgets and the variant-manager primitives in
:mod:`iidm_viewer.variants`. Sits in a :class:`QDockWidget` registered
on the right side of the main window and lets the user:

* pick a set of elements to outage (element-type combo + ID-substring
  filter + multiselect list + grouping combo + group-id text);
* click **Build N-K** to clone the working variant and disconnect the
  chosen elements;
* click **Run N-K Load Flow** to AC-solve on the N-K variant;
* click **Clear N-K** to drop the variant and reset the dock.

The dock keeps no direct pypowsybl state — every mutator funnels
through :class:`iidm_viewer.qt.state.AppState`. The state's
``nk_variant_changed`` and ``nk_loadflow_completed`` signals drive the
status pill + button enabled states so the dock stays in sync with
any other UI (per-tab combos, the menu bar, etc.) that touches the
N-K lifecycle.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.security_analysis import (
    MANUAL_GROUPINGS,
    MANUAL_GROUPING_TOKENS,
    MANUAL_TYPES,
    MANUAL_TYPE_IDS_KEY,
    get_element_ids,
    normalize_manual_contingency,
)
from iidm_viewer.variants import NK_VARIANT_ID


class NkVariantDock(QWidget):
    """Picker + Build / Run / Clear buttons + status pill for the
    N-K variant lifecycle."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._state = None  # set via set_state
        self._all_ids: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        title = QLabel("<b>N-K Variant</b>")
        title.setStyleSheet("padding-bottom: 2px;")
        root.addWidget(title)

        self._status_lbl = QLabel(
            "Pick the elements to outage, then click Build N-K. "
            "Affected tabs surface an N / N-K / Side-by-side toggle "
            "once the variant exists."
        )
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet("color: #555; padding: 2px 0 4px 0;")
        root.addWidget(self._status_lbl)

        # ---- Picker ----
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)

        self._type_combo = QComboBox()
        self._type_combo.addItems(list(MANUAL_TYPES))
        self._type_combo.currentTextChanged.connect(self._on_type_changed)
        form.addRow("Element type:", self._type_combo)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter by ID (substring)")
        self._filter_edit.textChanged.connect(self._refresh_id_list)
        form.addRow("Filter:", self._filter_edit)

        self._id_list = QListWidget()
        self._id_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self._id_list.setMinimumHeight(120)
        form.addRow("Pick elements:", self._id_list)

        self._grouping_combo = QComboBox()
        self._grouping_combo.addItems(list(MANUAL_GROUPINGS))
        form.addRow("Grouping:", self._grouping_combo)

        self._group_id_edit = QLineEdit()
        self._group_id_edit.setPlaceholderText(
            "Contingency id (for single grouped mode)"
        )
        form.addRow("Group id:", self._group_id_edit)

        root.addLayout(form)

        # ---- Build button ----
        self._build_btn = QPushButton("Build N-K")
        self._build_btn.setEnabled(False)
        self._build_btn.clicked.connect(self._on_build_clicked)
        root.addWidget(self._build_btn)

        # ---- Run LF + Clear buttons ----
        action_row = QHBoxLayout()
        self._run_lf_btn = QPushButton("Run N-K Load Flow")
        self._run_lf_btn.setEnabled(False)
        self._run_lf_btn.clicked.connect(self._on_run_lf_clicked)
        action_row.addWidget(self._run_lf_btn)

        self._clear_btn = QPushButton("Clear N-K")
        self._clear_btn.setEnabled(False)
        self._clear_btn.clicked.connect(self._on_clear_clicked)
        action_row.addWidget(self._clear_btn)
        root.addLayout(action_row)

        # ---- LF status pill ----
        self._lf_status_lbl = QLabel("")
        self._lf_status_lbl.setWordWrap(True)
        self._lf_status_lbl.setStyleSheet("padding: 4px 0;")
        root.addWidget(self._lf_status_lbl)

        root.addStretch(1)

    # ------------------------------------------------------------------
    # Public wiring
    # ------------------------------------------------------------------
    def set_state(self, state) -> None:
        """Wire the dock to a :class:`AppState`. Subscribes the
        dock's refresh hooks to ``nk_variant_changed`` /
        ``nk_loadflow_completed`` so external dock-state changes (a
        clear from a menu, a programmatic build) keep the UI in sync."""
        self._state = state
        state.nk_variant_changed.connect(self._on_nk_variant_changed)
        state.nk_loadflow_completed.connect(self._on_nk_loadflow_completed)
        state.network_changed.connect(self.set_network)

    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        if network is None:
            self._all_ids = []
            self._id_list.clear()
            self._build_btn.setEnabled(False)
            self._refresh_buttons()
            return
        self._reload_ids_for_current_type()
        self._refresh_buttons()

    # ------------------------------------------------------------------
    # Picker — type / filter / list management
    # ------------------------------------------------------------------
    def _on_type_changed(self, _txt: str) -> None:
        self._reload_ids_for_current_type()

    def _reload_ids_for_current_type(self) -> None:
        if self._network is None:
            self._all_ids = []
            self._id_list.clear()
            return
        try:
            ids_map = get_element_ids(self._network)
        except Exception:
            ids_map = {}
        manual_type = self._type_combo.currentText()
        key = MANUAL_TYPE_IDS_KEY.get(manual_type, "")
        self._all_ids = list(ids_map.get(key, []))
        self._refresh_id_list()

    def _refresh_id_list(self) -> None:
        text = (self._filter_edit.text() or "").strip().lower()
        self._id_list.clear()
        for eid in self._all_ids:
            if text and text not in str(eid).lower():
                continue
            self._id_list.addItem(QListWidgetItem(str(eid)))

    def _selected_ids(self) -> list[str]:
        return [
            self._id_list.item(i).text()
            for i in range(self._id_list.count())
            if self._id_list.item(i).isSelected()
        ]

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def _on_build_clicked(self) -> None:
        if self._network is None or self._state is None:
            return
        manual_type = self._type_combo.currentText()
        selected = self._selected_ids()
        grouping_label = self._grouping_combo.currentText()
        grouping_token = MANUAL_GROUPING_TOKENS.get(
            grouping_label, grouping_label,
        )
        group_id = self._group_id_edit.text() or ""
        try:
            contingencies = normalize_manual_contingency(
                manual_type, selected, grouping_token, group_id,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Picker error", str(exc))
            return

        if len(contingencies) > 1:
            # Per-element mode produced N-1 entries; collapse to a
            # single grouped contingency carrying every selected id.
            grouped_ids: list[str] = []
            for c in contingencies:
                grouped_ids.extend(c.get("element_ids") or [])
            first_id = contingencies[0]["id"]
            if first_id.startswith("N1_"):
                contingency = {"id": "N-K_grouped", "element_ids": grouped_ids}
            else:
                contingency = {"id": f"{first_id}_grouped", "element_ids": grouped_ids}
        else:
            contingency = contingencies[0]

        try:
            self._state.build_nk_variant(contingency)
        except Exception as exc:
            QMessageBox.critical(self, "Build N-K failed", str(exc))
            return

    def _on_run_lf_clicked(self) -> None:
        if self._state is None:
            return
        try:
            result = self._state.run_nk_loadflow(
                self._state.lf_generic_params,
                self._state.lf_provider_params,
            )
        except Exception as exc:
            QMessageBox.critical(self, "N-K LF failed", str(exc))
            return
        if result is None:
            QMessageBox.warning(self, "N-K LF", "Build the N-K variant first.")

    def _on_clear_clicked(self) -> None:
        if self._state is None:
            return
        self._state.clear_nk_variant()

    # ------------------------------------------------------------------
    # Reaction to AppState signals
    # ------------------------------------------------------------------
    def _on_nk_variant_changed(self, variant_id) -> None:
        self._refresh_buttons()
        self._refresh_status_label()
        # Reset the LF status pill when the variant changes.
        if variant_id is None:
            self._lf_status_lbl.setText("")

    def _on_nk_loadflow_completed(self, result) -> None:
        if result is None:
            return
        status = getattr(result, "status", "UNKNOWN")
        color = "#0a7e2a" if status == "CONVERGED" else "#b94a48"
        self._lf_status_lbl.setText(
            f'<span style="color:{color};">N-K LF: {status}</span>'
        )

    def _refresh_buttons(self) -> None:
        nk_active = (
            self._state is not None
            and self._state.nk_variant_id == NK_VARIANT_ID
        )
        net_loaded = self._network is not None
        self._build_btn.setEnabled(net_loaded)
        self._run_lf_btn.setEnabled(nk_active)
        self._clear_btn.setEnabled(nk_active)

    def _refresh_status_label(self) -> None:
        if self._state is None:
            return
        contingency = self._state.nk_contingency
        if contingency is None:
            self._status_lbl.setText(
                "Pick the elements to outage, then click Build N-K. "
                "Affected tabs surface an N / N-K / Side-by-side toggle "
                "once the variant exists."
            )
            return
        eids = contingency.get("element_ids") or []
        eid_text = ", ".join(eids)
        self._status_lbl.setText(
            f"<b>Active N-K:</b> {contingency.get('id', '')} — {eid_text}"
        )
