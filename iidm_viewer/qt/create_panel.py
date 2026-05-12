"""PySide6 "Create a new <component>" panel.

A single :class:`CreateComponentPanel` widget renders any component
declared in
:data:`iidm_viewer.component_creation.CREATABLE_COMPONENTS`. The
field specs come from the shared registry; the widget toolkit is
Qt-specific.

Layout:

* VL picker (only node-breaker VLs — bay creation needs busbar
  sections to attach the feeder to).
* Busbar section picker (refreshes when the VL changes).
* Per-field widget grid (3 columns).
* Locator fields (position_order + direction) — appended to every form.
* Create button + status label.
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.component_creation import (
    CREATABLE_BRANCHES,
    CREATABLE_COMPONENTS,
    CREATABLE_CONTAINERS,
    CREATABLE_HVDC_LINES,
    CREATABLE_TAP_CHANGERS,
    LOCATOR_FIELDS,
    branch_side_locator_fields,
    coerce_field_values,
    create_branch_bay,
    create_component_bay,
    create_container,
    create_coupling_device,
    create_hvdc_line,
    create_tap_changer,
    list_busbar_sections,
    list_converter_stations,
    list_node_breaker_voltage_levels,
    list_node_breaker_vls_with_multi_bbs,
    list_substations_df,
    list_transformers_without_tap_changer,
    next_free_node,
)
from iidm_viewer.powsybl_worker import NetworkProxy


# ---------------------------------------------------------------------------
# Shared widget builders — used by both ``CreateComponentPanel`` and
# ``CreateBranchPanel``.
# ---------------------------------------------------------------------------
def make_field_widget(field: dict) -> QWidget:
    """Build the right Qt widget for a field spec from the shared registry.

    Knows the five widget kinds the registry uses: text / float / int /
    bool / select. Raises ``ValueError`` on an unknown kind.
    """
    kind = field["kind"]
    if kind == "text":
        w = QLineEdit()
        w.setText(str(field.get("default") or ""))
        return w
    if kind == "float":
        w = QDoubleSpinBox()
        w.setDecimals(6)
        w.setRange(field.get("min_value", -1e15), 1e15)
        w.setValue(float(field.get("default", 0.0)))
        return w
    if kind == "int":
        w = QSpinBox()
        w.setRange(field.get("min_value", -2 ** 31), 2 ** 31 - 1)
        w.setSingleStep(int(field.get("step", 1)))
        w.setValue(int(field.get("default", 0)))
        return w
    if kind == "bool":
        w = QCheckBox()
        w.setChecked(bool(field.get("default", False)))
        return w
    if kind == "select":
        w = QComboBox()
        options = list(field.get("options", []))
        for opt in options:
            w.addItem(str(opt))
        default = field.get("default")
        if default in options:
            w.setCurrentIndex(options.index(default))
        return w
    raise ValueError(f"Unknown field kind {kind!r}")


def read_field_widget(field: dict, widget: QWidget) -> Any:
    """Read a widget's value typed per the field spec."""
    kind = field["kind"]
    if kind == "text":
        return widget.text()
    if kind in ("float", "int"):
        return widget.value()
    if kind == "bool":
        return widget.isChecked()
    if kind == "select":
        return widget.currentText()
    return None


class CreateComponentPanel(QWidget):
    """Generic creation form for any component in CREATABLE_COMPONENTS."""

    # Emitted after a successful create. Payload: (component, element_id).
    component_created = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._component: Optional[str] = None
        self._field_widgets: dict[str, QWidget] = {}

        # Header — collapsible via QGroupBox's checkable style. Note:
        # ``QGroupBox.setCheckable(True)`` toggles the *enabled* state
        # of child widgets too, which would make our tests + initial
        # render brittle. We default to checked, then ``_on_toggled``
        # only flips visibility of the inner widget.
        self._group = QGroupBox("Create a new component")
        self._group.setCheckable(True)
        self._group.setChecked(True)
        self._group.toggled.connect(self._on_toggled)

        # VL + busbar picker row.
        self._vl_combo = QComboBox()
        self._vl_combo.setMinimumWidth(220)
        self._vl_combo.currentTextChanged.connect(self._on_vl_changed)
        self._bbs_combo = QComboBox()
        self._bbs_combo.setMinimumWidth(180)

        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Voltage level:"))
        picker_row.addWidget(self._vl_combo)
        picker_row.addSpacing(12)
        picker_row.addWidget(QLabel("Busbar section:"))
        picker_row.addWidget(self._bbs_combo)
        picker_row.addStretch(1)

        # Field grid — repopulated on every set_component.
        self._fields_grid = QGridLayout()
        self._fields_grid.setSpacing(6)
        self._fields_widget = QWidget()
        self._fields_widget.setLayout(self._fields_grid)

        # Action row.
        self._create_btn = QPushButton("Create")
        self._create_btn.clicked.connect(self._on_create_clicked)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        action_row = QHBoxLayout()
        action_row.addWidget(self._create_btn)
        action_row.addWidget(self._status, 1)

        inner = QVBoxLayout()
        inner.setContentsMargins(6, 2, 6, 6)
        inner.setSpacing(6)
        inner.addLayout(picker_row)
        inner.addWidget(self._fields_widget)
        inner.addLayout(action_row)
        self._group.setLayout(inner)
        # Inner widget visible because the group starts checked.
        self._fields_widget.setVisible(True)
        # Hidden until a network with node-breaker VLs is loaded.
        self.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._group)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._refresh_vl_combo()
        self._refresh_visibility()

    def set_component(self, component: Optional[str]) -> None:
        """Switch the form to a different component (or hide entirely)."""
        self._component = component
        self._group.setTitle(
            f"Create a new {component.lower().rstrip('s')}"
            if component else "Create a new component"
        )
        self._rebuild_field_widgets()
        self._refresh_visibility()
        self._status.setText("")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _refresh_visibility(self) -> None:
        applicable = (
            self._network is not None
            and self._component in CREATABLE_COMPONENTS
        )
        self.setVisible(applicable)

    def _on_toggled(self, checked: bool) -> None:
        self._fields_widget.setVisible(checked)

    def _refresh_vl_combo(self) -> None:
        self._vl_combo.blockSignals(True)
        self._vl_combo.clear()
        self._bbs_combo.clear()
        if self._network is None:
            self._vl_combo.blockSignals(False)
            return
        try:
            vls = list_node_breaker_voltage_levels(self._network)
        except Exception:
            vls = None
        if vls is None or vls.empty:
            self._vl_combo.addItem("(no node-breaker VLs in this network)")
            self._vl_combo.setEnabled(False)
            self._bbs_combo.setEnabled(False)
            self._create_btn.setEnabled(False)
        else:
            self._vl_combo.setEnabled(True)
            self._bbs_combo.setEnabled(True)
            self._create_btn.setEnabled(True)
            for _, row in vls.iterrows():
                self._vl_combo.addItem(str(row["display"]), userData=str(row["id"]))
            self._on_vl_changed(self._vl_combo.currentText())
        self._vl_combo.blockSignals(False)

    def _on_vl_changed(self, _label: str) -> None:
        self._bbs_combo.clear()
        if self._network is None or self._vl_combo.currentData() is None:
            return
        vl_id = str(self._vl_combo.currentData())
        try:
            ids = list_busbar_sections(self._network, vl_id)
        except Exception:
            ids = []
        if not ids:
            self._bbs_combo.addItem("(no busbar sections)")
            self._bbs_combo.setEnabled(False)
            self._create_btn.setEnabled(False)
            return
        for bid in ids:
            self._bbs_combo.addItem(str(bid))
        self._bbs_combo.setEnabled(True)
        self._create_btn.setEnabled(True)

    def _rebuild_field_widgets(self) -> None:
        # Tear down existing widgets.
        for w in self._field_widgets.values():
            w.setParent(None)
        self._field_widgets.clear()
        while self._fields_grid.count():
            item = self._fields_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        if self._component not in CREATABLE_COMPONENTS:
            return

        spec = CREATABLE_COMPONENTS[self._component]
        fields = list(spec["fields"]) + list(LOCATOR_FIELDS)

        # Render in a 3-column grid.
        for i, f in enumerate(fields):
            row, col = divmod(i, 3)
            widget_label = f["label"] + (" *" if f.get("required") else "")
            label = QLabel(widget_label)
            if f.get("help"):
                label.setToolTip(f["help"])
            widget = self._make_widget(f)
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)
            cell_layout.addWidget(label)
            cell_layout.addWidget(widget)
            self._fields_grid.addWidget(cell, row, col)
            self._field_widgets[f["name"]] = widget

    def _make_widget(self, field: dict) -> QWidget:
        return make_field_widget(field)

    def _read_widget(self, field: dict) -> Any:
        w = self._field_widgets.get(field["name"])
        if w is None:
            return None
        return read_field_widget(field, w)

    def _on_create_clicked(self) -> None:
        if self._network is None or self._component not in CREATABLE_COMPONENTS:
            return
        bbs_id = self._bbs_combo.currentText() if self._bbs_combo.isEnabled() else ""
        if not bbs_id or bbs_id.startswith("("):
            self._status.setText("Select a busbar section first.")
            self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
            return

        spec = CREATABLE_COMPONENTS[self._component]
        all_fields = list(spec["fields"]) + list(LOCATOR_FIELDS)
        raw = {f["name"]: self._read_widget(f) for f in all_fields}
        values = coerce_field_values(all_fields, raw)
        values["bus_or_busbar_section_id"] = bbs_id

        try:
            create_component_bay(self._network, self._component, values)
        except Exception as exc:
            self._status.setText(f"Create failed — {exc}")
            self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
            return

        created_id = str(values.get("id", ""))
        self._status.setText(f"Created {self._component.rstrip('s')} {created_id!r}.")
        self._status.setStyleSheet("color: #0a7e2a; padding: 0 6px;")
        self.component_created.emit(self._component, created_id)


# ---------------------------------------------------------------------------
# Branch creation panel (Lines + 2-Winding Transformers)
# ---------------------------------------------------------------------------
class CreateBranchPanel(QWidget):
    """Generic creation form for any component in CREATABLE_BRANCHES.

    Layout:

    * Side-1 picker:   [VL 1 ▾]  [Busbar section 1 ▾]
    * Side-2 picker:   [VL 2 ▾]  [Busbar section 2 ▾]
    * Electrical fields (id, r, x, …)  — registry-driven, 3-column grid.
    * Side-1 locator:  [position_order 1] [direction 1]
    * Side-2 locator:  [position_order 2] [direction 2]
    * Create button + status.

    Same auto-hide rules as :class:`CreateComponentPanel`: hidden when
    the component isn't in :data:`CREATABLE_BRANCHES` or the network
    has no node-breaker voltage levels (bay creation needs busbar
    sections).
    """

    component_created = Signal(str, str)  # (component, element_id)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._component: Optional[str] = None
        self._field_widgets: dict[str, QWidget] = {}

        self._group = QGroupBox("Create a new branch")
        self._group.setCheckable(True)
        self._group.setChecked(True)
        self._group.toggled.connect(self._on_toggled)

        # Side pickers.
        self._vl1 = QComboBox(); self._vl1.setMinimumWidth(200)
        self._vl1.currentTextChanged.connect(lambda _t: self._on_vl_changed(1))
        self._bbs1 = QComboBox(); self._bbs1.setMinimumWidth(160)
        self._vl2 = QComboBox(); self._vl2.setMinimumWidth(200)
        self._vl2.currentTextChanged.connect(lambda _t: self._on_vl_changed(2))
        self._bbs2 = QComboBox(); self._bbs2.setMinimumWidth(160)

        side1_row = QHBoxLayout()
        side1_row.addWidget(QLabel("Side 1 — VL:"))
        side1_row.addWidget(self._vl1)
        side1_row.addSpacing(8)
        side1_row.addWidget(QLabel("Busbar:"))
        side1_row.addWidget(self._bbs1)
        side1_row.addStretch(1)

        side2_row = QHBoxLayout()
        side2_row.addWidget(QLabel("Side 2 — VL:"))
        side2_row.addWidget(self._vl2)
        side2_row.addSpacing(8)
        side2_row.addWidget(QLabel("Busbar:"))
        side2_row.addWidget(self._bbs2)
        side2_row.addStretch(1)

        # Electrical fields + per-side locators land in this grid.
        self._fields_grid = QGridLayout()
        self._fields_grid.setSpacing(6)
        self._fields_widget = QWidget()
        self._fields_widget.setLayout(self._fields_grid)

        # Action row.
        self._create_btn = QPushButton("Create")
        self._create_btn.clicked.connect(self._on_create_clicked)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        action_row = QHBoxLayout()
        action_row.addWidget(self._create_btn)
        action_row.addWidget(self._status, 1)

        inner = QVBoxLayout()
        inner.setContentsMargins(6, 2, 6, 6)
        inner.setSpacing(6)
        inner.addLayout(side1_row)
        inner.addLayout(side2_row)
        inner.addWidget(self._fields_widget)
        inner.addLayout(action_row)
        self._group.setLayout(inner)
        self._fields_widget.setVisible(True)
        self.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._group)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._refresh_vl_combos()
        self._refresh_visibility()

    def set_component(self, component: Optional[str]) -> None:
        self._component = component
        self._group.setTitle(
            f"Create a new {component.lower().rstrip('s')}"
            if component else "Create a new branch"
        )
        self._rebuild_field_widgets()
        self._refresh_visibility()
        self._status.setText("")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _refresh_visibility(self) -> None:
        applicable = (
            self._network is not None
            and self._component in CREATABLE_BRANCHES
        )
        self.setVisible(applicable)

    def _on_toggled(self, checked: bool) -> None:
        self._fields_widget.setVisible(checked)

    def _refresh_vl_combos(self) -> None:
        for combo in (self._vl1, self._vl2):
            combo.blockSignals(True)
            combo.clear()
        for combo in (self._bbs1, self._bbs2):
            combo.clear()

        if self._network is None:
            for combo in (self._vl1, self._vl2):
                combo.blockSignals(False)
            return
        try:
            vls = list_node_breaker_voltage_levels(self._network)
        except Exception:
            vls = None
        if vls is None or vls.empty:
            for combo in (self._vl1, self._vl2):
                combo.addItem("(no node-breaker VLs in this network)")
                combo.setEnabled(False)
                combo.blockSignals(False)
            for combo in (self._bbs1, self._bbs2):
                combo.setEnabled(False)
            self._create_btn.setEnabled(False)
            return
        for combo in (self._vl1, self._vl2):
            combo.setEnabled(True)
        for combo in (self._bbs1, self._bbs2):
            combo.setEnabled(True)
        self._create_btn.setEnabled(True)
        for combo in (self._vl1, self._vl2):
            for _, row in vls.iterrows():
                combo.addItem(str(row["display"]), userData=str(row["id"]))
        # Second VL pre-selects the next one (avoids accidental self-loop).
        if self._vl2.count() > 1:
            self._vl2.setCurrentIndex(1)
        for combo in (self._vl1, self._vl2):
            combo.blockSignals(False)
        self._on_vl_changed(1)
        self._on_vl_changed(2)

    def _on_vl_changed(self, side: int) -> None:
        vl_combo = self._vl1 if side == 1 else self._vl2
        bbs_combo = self._bbs1 if side == 1 else self._bbs2
        bbs_combo.clear()
        if self._network is None or vl_combo.currentData() is None:
            return
        try:
            ids = list_busbar_sections(self._network, str(vl_combo.currentData()))
        except Exception:
            ids = []
        if not ids:
            bbs_combo.addItem("(no busbar sections)")
            bbs_combo.setEnabled(False)
            self._create_btn.setEnabled(False)
            return
        for bid in ids:
            bbs_combo.addItem(str(bid))
        bbs_combo.setEnabled(True)
        self._create_btn.setEnabled(True)

    def _rebuild_field_widgets(self) -> None:
        for w in self._field_widgets.values():
            w.setParent(None)
        self._field_widgets.clear()
        while self._fields_grid.count():
            item = self._fields_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        if self._component not in CREATABLE_BRANCHES:
            return

        spec = CREATABLE_BRANCHES[self._component]
        # Electrical fields first; then per-side locator fields (which
        # carry _1 / _2 name suffixes).
        fields = (
            list(spec["fields"])
            + list(branch_side_locator_fields(1))
            + list(branch_side_locator_fields(2))
        )
        for i, f in enumerate(fields):
            row, col = divmod(i, 3)
            widget_label = f["label"] + (" *" if f.get("required") else "")
            label = QLabel(widget_label)
            if f.get("help"):
                label.setToolTip(f["help"])
            widget = make_field_widget(f)
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)
            cell_layout.addWidget(label)
            cell_layout.addWidget(widget)
            self._fields_grid.addWidget(cell, row, col)
            self._field_widgets[f["name"]] = widget

    def _on_create_clicked(self) -> None:
        if self._network is None or self._component not in CREATABLE_BRANCHES:
            return
        bbs1 = self._bbs1.currentText() if self._bbs1.isEnabled() else ""
        bbs2 = self._bbs2.currentText() if self._bbs2.isEnabled() else ""
        if not bbs1 or bbs1.startswith("(") or not bbs2 or bbs2.startswith("("):
            self._status.setText("Pick a busbar section on both sides first.")
            self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
            return
        spec = CREATABLE_BRANCHES[self._component]
        all_fields = (
            list(spec["fields"])
            + list(branch_side_locator_fields(1))
            + list(branch_side_locator_fields(2))
        )
        raw = {f["name"]: read_field_widget(f, self._field_widgets[f["name"]])
               for f in all_fields if f["name"] in self._field_widgets}
        values = coerce_field_values(all_fields, raw)
        values["bus_or_busbar_section_id_1"] = bbs1
        values["bus_or_busbar_section_id_2"] = bbs2
        try:
            create_branch_bay(self._network, self._component, values)
        except Exception as exc:
            self._status.setText(f"Create failed — {exc}")
            self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
            return
        created_id = str(values.get("id", ""))
        self._status.setText(f"Created {self._component.rstrip('s')} {created_id!r}.")
        self._status.setStyleSheet("color: #0a7e2a; padding: 0 6px;")
        self.component_created.emit(self._component, created_id)


# ---------------------------------------------------------------------------
# Container creation panel (Substations / Voltage Levels / Busbar Sections)
# ---------------------------------------------------------------------------
class CreateContainerPanel(QWidget):
    """Generic creation form for any component in :data:`CREATABLE_CONTAINERS`.

    Each container type has a different "context" picker on top:

    * **Substations**         — no picker (top-level).
    * **Voltage Levels**      — optional substation picker.
    * **Busbar Sections**     — required node-breaker VL picker; the
      ``node`` field default updates to ``next_free_node(network, vl)``.

    Below the picker comes the registry-driven field grid + Create
    button + status label.
    """

    component_created = Signal(str, str)  # (component, element_id)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._component: Optional[str] = None
        self._field_widgets: dict[str, QWidget] = {}

        self._group = QGroupBox("Create a new container")
        self._group.setCheckable(True)
        self._group.setChecked(True)
        self._group.toggled.connect(self._on_toggled)

        # Context picker — visible only when the component needs one.
        self._context_lbl = QLabel("")
        self._context_combo = QComboBox()
        self._context_combo.setMinimumWidth(240)
        self._context_combo.currentIndexChanged.connect(self._on_context_changed)
        picker_row = QHBoxLayout()
        picker_row.addWidget(self._context_lbl)
        picker_row.addWidget(self._context_combo)
        picker_row.addStretch(1)
        self._picker_widget = QWidget()
        self._picker_widget.setLayout(picker_row)
        self._picker_widget.setVisible(False)

        # Field grid — repopulated on every set_component.
        self._fields_grid = QGridLayout()
        self._fields_grid.setSpacing(6)
        self._fields_widget = QWidget()
        self._fields_widget.setLayout(self._fields_grid)

        self._create_btn = QPushButton("Create")
        self._create_btn.clicked.connect(self._on_create_clicked)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        action_row = QHBoxLayout()
        action_row.addWidget(self._create_btn)
        action_row.addWidget(self._status, 1)

        inner = QVBoxLayout()
        inner.setContentsMargins(6, 2, 6, 6)
        inner.setSpacing(6)
        inner.addWidget(self._picker_widget)
        inner.addWidget(self._fields_widget)
        inner.addLayout(action_row)
        self._group.setLayout(inner)
        self._fields_widget.setVisible(True)
        self.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._group)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._rebuild_for_current_component()

    def set_component(self, component: Optional[str]) -> None:
        self._component = component
        self._group.setTitle(
            f"Create a new {component.lower().rstrip('s')}"
            if component else "Create a new container"
        )
        self._rebuild_for_current_component()
        self._status.setText("")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _on_toggled(self, checked: bool) -> None:
        self._fields_widget.setVisible(checked)
        self._picker_widget.setVisible(checked and self._picker_lbl_text() is not None)

    def _picker_lbl_text(self) -> Optional[str]:
        if self._component == "Voltage Levels":
            return "Substation (optional):"
        if self._component == "Busbar Sections":
            return "Voltage level:"
        return None

    def _rebuild_for_current_component(self) -> None:
        applicable = (
            self._network is not None
            and self._component in CREATABLE_CONTAINERS
        )
        self.setVisible(applicable)
        if not applicable:
            return
        self._populate_context_combo()
        self._rebuild_field_widgets()

    def _populate_context_combo(self) -> None:
        self._context_combo.blockSignals(True)
        self._context_combo.clear()
        text = self._picker_lbl_text()
        if text is None:
            self._context_lbl.setText("")
            self._picker_widget.setVisible(False)
            self._context_combo.blockSignals(False)
            return
        self._context_lbl.setText(text)
        self._picker_widget.setVisible(True)
        if self._component == "Voltage Levels":
            # Optional — first entry is "(no substation)".
            self._context_combo.addItem("(none — no substation)", userData=None)
            try:
                subs = list_substations_df(self._network)
            except Exception:
                subs = None
            if subs is not None and not subs.empty:
                for _, row in subs.iterrows():
                    self._context_combo.addItem(
                        str(row["display"]), userData=str(row["id"]),
                    )
            self._create_btn.setEnabled(True)
        elif self._component == "Busbar Sections":
            try:
                vls = list_node_breaker_voltage_levels(self._network)
            except Exception:
                vls = None
            if vls is None or vls.empty:
                self._context_combo.addItem("(no node-breaker VLs)", userData=None)
                self._context_combo.setEnabled(False)
                self._create_btn.setEnabled(False)
            else:
                self._context_combo.setEnabled(True)
                self._create_btn.setEnabled(True)
                for _, row in vls.iterrows():
                    label = f"{row['display']} ({row['nominal_v']:.0f} kV)"
                    self._context_combo.addItem(label, userData=str(row["id"]))
        self._context_combo.blockSignals(False)

    def _on_context_changed(self, _idx: int) -> None:
        # For Busbar Sections, update the ``node`` default to the next
        # free node in the chosen VL.
        if self._component != "Busbar Sections":
            return
        vl_id = self._context_combo.currentData()
        if not vl_id or self._network is None:
            return
        try:
            suggested = next_free_node(self._network, str(vl_id))
        except Exception:
            return
        node_w = self._field_widgets.get("node")
        if isinstance(node_w, QSpinBox):
            node_w.setValue(int(suggested))

    def _rebuild_field_widgets(self) -> None:
        for w in self._field_widgets.values():
            w.setParent(None)
        self._field_widgets.clear()
        while self._fields_grid.count():
            item = self._fields_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        if self._component not in CREATABLE_CONTAINERS:
            return
        spec = CREATABLE_CONTAINERS[self._component]
        fields = list(spec["fields"])
        # For Busbar Sections, prefill the ``node`` widget with the
        # next-free-node for the currently-picked VL.
        suggested_node: Optional[int] = None
        if self._component == "Busbar Sections":
            vl_id = self._context_combo.currentData()
            if vl_id and self._network is not None:
                try:
                    suggested_node = next_free_node(self._network, str(vl_id))
                except Exception:
                    suggested_node = None

        for i, f in enumerate(fields):
            row, col = divmod(i, 3)
            widget_label = f["label"] + (" *" if f.get("required") else "")
            label = QLabel(widget_label)
            if f.get("help"):
                label.setToolTip(f["help"])
            field_spec = f
            if f["name"] == "node" and suggested_node is not None:
                field_spec = {**f, "default": suggested_node}
            widget = make_field_widget(field_spec)
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)
            cell_layout.addWidget(label)
            cell_layout.addWidget(widget)
            self._fields_grid.addWidget(cell, row, col)
            self._field_widgets[f["name"]] = widget

    def _on_create_clicked(self) -> None:
        if self._network is None or self._component not in CREATABLE_CONTAINERS:
            return
        spec = CREATABLE_CONTAINERS[self._component]
        raw = {f["name"]: read_field_widget(f, self._field_widgets[f["name"]])
               for f in spec["fields"] if f["name"] in self._field_widgets}
        values = coerce_field_values(spec["fields"], raw)
        # Inject the context from the picker.
        if self._component == "Voltage Levels":
            sub_id = self._context_combo.currentData()
            if sub_id:
                values["substation_id"] = str(sub_id)
        elif self._component == "Busbar Sections":
            vl_id = self._context_combo.currentData()
            if not vl_id:
                self._status.setText("Pick a voltage level first.")
                self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
                return
            values["voltage_level_id"] = str(vl_id)

        try:
            create_container(self._network, self._component, values)
        except Exception as exc:
            self._status.setText(f"Create failed — {exc}")
            self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
            return
        created_id = str(values.get("id", ""))
        self._status.setText(f"Created {self._component.rstrip('s')} {created_id!r}.")
        self._status.setStyleSheet("color: #0a7e2a; padding: 0 6px;")
        self.component_created.emit(self._component, created_id)


# ---------------------------------------------------------------------------
# HVDC line creation panel
# ---------------------------------------------------------------------------
class CreateHvdcLinePanel(QWidget):
    """Form to create an HVDC line between two existing converter stations.

    Layout:

    * Two station pickers (Converter station 1, Converter station 2),
      defaulting to the first and second stations so the user can't
      accidentally pick the same one on both sides.
    * Electrical fields (id, name, r, nominal_v, max_p, target_p,
      converters_mode) from :data:`CREATABLE_HVDC_LINES`.
    * Create button + status label.

    Auto-hides when the active component isn't "HVDC Lines" or the
    network has fewer than two converter stations (pypowsybl requires
    both endpoints to already exist).
    """

    component_created = Signal(str, str)  # ("HVDC Lines", element_id)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._component: Optional[str] = None
        self._field_widgets: dict[str, QWidget] = {}

        self._group = QGroupBox("Create a new HVDC line")
        self._group.setCheckable(True)
        self._group.setChecked(True)
        self._group.toggled.connect(self._on_toggled)

        # Station pickers — labelled with both the id and the kind so
        # the user can spot VSC vs. LCC at a glance.
        self._cs1 = QComboBox(); self._cs1.setMinimumWidth(240)
        self._cs2 = QComboBox(); self._cs2.setMinimumWidth(240)
        pickers = QHBoxLayout()
        pickers.addWidget(QLabel("Converter station 1:"))
        pickers.addWidget(self._cs1)
        pickers.addSpacing(12)
        pickers.addWidget(QLabel("Converter station 2:"))
        pickers.addWidget(self._cs2)
        pickers.addStretch(1)

        self._fields_grid = QGridLayout()
        self._fields_grid.setSpacing(6)
        self._fields_widget = QWidget()
        self._fields_widget.setLayout(self._fields_grid)

        self._create_btn = QPushButton("Create")
        self._create_btn.clicked.connect(self._on_create_clicked)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        action_row = QHBoxLayout()
        action_row.addWidget(self._create_btn)
        action_row.addWidget(self._status, 1)

        inner = QVBoxLayout()
        inner.setContentsMargins(6, 2, 6, 6)
        inner.setSpacing(6)
        inner.addLayout(pickers)
        inner.addWidget(self._fields_widget)
        inner.addLayout(action_row)
        self._group.setLayout(inner)
        self._fields_widget.setVisible(True)
        self.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._group)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._refresh_for_current()

    def set_component(self, component: Optional[str]) -> None:
        self._component = component
        self._refresh_for_current()
        self._status.setText("")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _on_toggled(self, checked: bool) -> None:
        self._fields_widget.setVisible(checked)

    def _refresh_for_current(self) -> None:
        applicable = (
            self._network is not None
            and self._component == "HVDC Lines"
        )
        if not applicable:
            self.setVisible(False)
            return

        try:
            stations = list_converter_stations(self._network)
        except Exception:
            stations = []
        if len(stations) < 2:
            # pypowsybl needs at least two existing stations.
            self.setVisible(False)
            return

        self.setVisible(True)
        self._cs1.blockSignals(True)
        self._cs2.blockSignals(True)
        self._cs1.clear()
        self._cs2.clear()
        for sid, kind in stations:
            label = f"{sid} ({kind})"
            self._cs1.addItem(label, userData=sid)
            self._cs2.addItem(label, userData=sid)
        # Pre-select the second station on side 2.
        if self._cs2.count() > 1:
            self._cs2.setCurrentIndex(1)
        self._cs1.blockSignals(False)
        self._cs2.blockSignals(False)

        self._rebuild_field_widgets()

    def _rebuild_field_widgets(self) -> None:
        for w in self._field_widgets.values():
            w.setParent(None)
        self._field_widgets.clear()
        while self._fields_grid.count():
            item = self._fields_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        for i, f in enumerate(CREATABLE_HVDC_LINES["fields"]):
            row, col = divmod(i, 3)
            widget_label = f["label"] + (" *" if f.get("required") else "")
            label = QLabel(widget_label)
            if f.get("help"):
                label.setToolTip(f["help"])
            widget = make_field_widget(f)
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)
            cell_layout.addWidget(label)
            cell_layout.addWidget(widget)
            self._fields_grid.addWidget(cell, row, col)
            self._field_widgets[f["name"]] = widget

    def _on_create_clicked(self) -> None:
        if self._network is None or self._component != "HVDC Lines":
            return
        cs1 = self._cs1.currentData()
        cs2 = self._cs2.currentData()
        if not cs1 or not cs2:
            self._status.setText("Pick both converter stations first.")
            self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
            return
        raw = {
            f["name"]: read_field_widget(f, self._field_widgets[f["name"]])
            for f in CREATABLE_HVDC_LINES["fields"]
            if f["name"] in self._field_widgets
        }
        values = coerce_field_values(CREATABLE_HVDC_LINES["fields"], raw)
        values["converter_station1_id"] = str(cs1)
        values["converter_station2_id"] = str(cs2)
        try:
            create_hvdc_line(self._network, values)
        except Exception as exc:
            self._status.setText(f"Create failed — {exc}")
            self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
            return
        created_id = str(values.get("id", ""))
        self._status.setText(f"Created HVDC line {created_id!r}.")
        self._status.setStyleSheet("color: #0a7e2a; padding: 0 6px;")
        self.component_created.emit("HVDC Lines", created_id)


# ---------------------------------------------------------------------------
# Tap changer creation panel (ratio + phase, sub-form on a 2WT)
# ---------------------------------------------------------------------------
class CreateTapChangerPanel(QWidget):
    """Sub-form to add a ratio or phase tap changer to an existing 2WT.

    Auto-hides unless the active data-explorer component is
    "2-Winding Transformers" and the network carries at least one
    transformer that doesn't yet have a tap changer of the chosen kind.

    Layout:
      * Kind picker (Ratio / Phase)
      * Target transformer picker (filtered to ones without that kind)
      * Main-fields grid built from the shared registry
      * Editable steps table with a "Number of steps" spinner
      * Create button + status label
    """

    component_created = Signal(str, str)  # ("Tap Changers", transformer_id)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._component: Optional[str] = None
        self._kind: str = "Ratio"
        self._main_widgets: dict[str, QWidget] = {}

        self._group = QGroupBox("Create a tap changer on a 2-winding transformer")
        self._group.setCheckable(True)
        self._group.setChecked(True)
        self._group.toggled.connect(self._on_toggled)

        # Pickers row
        self._kind_combo = QComboBox()
        for k in CREATABLE_TAP_CHANGERS:
            self._kind_combo.addItem(k)
        self._kind_combo.currentTextChanged.connect(self._on_kind_changed)
        self._twt_combo = QComboBox()
        self._twt_combo.setMinimumWidth(220)
        pickers = QHBoxLayout()
        pickers.addWidget(QLabel("Kind"))
        pickers.addWidget(self._kind_combo)
        pickers.addSpacing(12)
        pickers.addWidget(QLabel("Target 2WT"))
        pickers.addWidget(self._twt_combo, 1)

        # Main-fields grid
        self._main_grid = QGridLayout()
        self._main_grid.setSpacing(6)
        self._main_widget = QWidget()
        self._main_widget.setLayout(self._main_grid)

        # Steps table + count spinner
        self._steps_count = QSpinBox()
        self._steps_count.setRange(1, 50)
        self._steps_count.setValue(3)
        self._steps_count.valueChanged.connect(self._resize_steps_table)
        self._steps_table = QTableWidget(0, 0)
        self._steps_table.setMinimumHeight(120)
        self._steps_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        steps_row = QHBoxLayout()
        steps_row.addWidget(QLabel("Number of steps"))
        steps_row.addWidget(self._steps_count)
        steps_row.addStretch(1)

        # Action row
        self._create_btn = QPushButton("Create tap changer")
        self._create_btn.clicked.connect(self._on_create_clicked)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        action_row = QHBoxLayout()
        action_row.addWidget(self._create_btn)
        action_row.addWidget(self._status, 1)

        inner = QVBoxLayout()
        inner.addLayout(pickers)
        inner.addWidget(self._main_widget)
        inner.addLayout(steps_row)
        inner.addWidget(self._steps_table)
        inner.addLayout(action_row)
        self._group.setLayout(inner)

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._group)
        self.setLayout(outer)

        self.setVisible(False)

    # -- Public API ------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._refresh_for_current()

    def set_component(self, component: Optional[str]) -> None:
        self._component = component
        self._refresh_for_current()
        self._status.setText("")

    # -- Internals -------------------------------------------------------
    def _on_toggled(self, checked: bool) -> None:
        self._main_widget.setVisible(checked)
        self._steps_table.setVisible(checked)

    def _on_kind_changed(self, _kind: str) -> None:
        self._kind = self._kind_combo.currentText()
        self._refresh_for_current()

    def _refresh_for_current(self) -> None:
        show = (
            self._network is not None
            and self._component == "2-Winding Transformers"
        )
        if show:
            available = list_transformers_without_tap_changer(
                self._network, self._kind,
            )
        else:
            available = []
        self.setVisible(bool(show and available))
        if not (show and available):
            return

        self._twt_combo.blockSignals(True)
        self._twt_combo.clear()
        for tid in available:
            self._twt_combo.addItem(tid)
        self._twt_combo.blockSignals(False)

        self._rebuild_main_fields()
        self._rebuild_steps_table()

    def _rebuild_main_fields(self) -> None:
        for w in self._main_widgets.values():
            w.deleteLater()
        self._main_widgets.clear()
        while self._main_grid.count():
            item = self._main_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        spec = CREATABLE_TAP_CHANGERS[self._kind]
        for idx, f in enumerate(spec["main_fields"]):
            widget = make_field_widget(f)
            row, col = divmod(idx, 3)
            cell = QWidget()
            box = QVBoxLayout()
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(2)
            box.addWidget(QLabel(f["label"]))
            box.addWidget(widget)
            cell.setLayout(box)
            self._main_grid.addWidget(cell, row, col)
            self._main_widgets[f["name"]] = widget

    def _rebuild_steps_table(self) -> None:
        spec = CREATABLE_TAP_CHANGERS[self._kind]
        cols = spec["step_columns"]
        n_rows = self._steps_count.value()
        self._steps_table.blockSignals(True)
        self._steps_table.setColumnCount(len(cols))
        self._steps_table.setHorizontalHeaderLabels(cols)
        self._steps_table.setRowCount(n_rows)
        defaults = spec["step_defaults"]
        for r in range(n_rows):
            for c, col in enumerate(cols):
                item = self._steps_table.item(r, c)
                if item is None:
                    self._steps_table.setItem(
                        r, c, QTableWidgetItem(str(defaults[col])),
                    )
        self._steps_table.blockSignals(False)

    def _resize_steps_table(self, _n: int) -> None:
        spec = CREATABLE_TAP_CHANGERS[self._kind]
        cols = spec["step_columns"]
        defaults = spec["step_defaults"]
        n_rows = self._steps_count.value()
        prev = self._steps_table.rowCount()
        self._steps_table.setRowCount(n_rows)
        if n_rows > prev:
            for r in range(prev, n_rows):
                for c, col in enumerate(cols):
                    self._steps_table.setItem(
                        r, c, QTableWidgetItem(str(defaults[col])),
                    )

    def _collect_steps(self) -> list[dict]:
        spec = CREATABLE_TAP_CHANGERS[self._kind]
        cols = spec["step_columns"]
        rows: list[dict] = []
        for r in range(self._steps_table.rowCount()):
            row: dict = {}
            for c, col in enumerate(cols):
                item = self._steps_table.item(r, c)
                text = (item.text() if item else "").strip()
                try:
                    row[col] = float(text) if text != "" else spec["step_defaults"][col]
                except ValueError:
                    row[col] = spec["step_defaults"][col]
            rows.append(row)
        return rows

    def _on_create_clicked(self) -> None:
        if self._network is None or self._component != "2-Winding Transformers":
            return
        transformer_id = self._twt_combo.currentText()
        if not transformer_id:
            self._status.setText("Pick a target transformer first.")
            self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
            return
        spec = CREATABLE_TAP_CHANGERS[self._kind]
        raw = {
            f["name"]: read_field_widget(f, self._main_widgets[f["name"]])
            for f in spec["main_fields"]
            if f["name"] in self._main_widgets
        }
        main_fields = coerce_field_values(spec["main_fields"], raw)
        steps = self._collect_steps()
        try:
            create_tap_changer(
                self._network, self._kind, transformer_id, main_fields, steps,
            )
        except Exception as exc:
            self._status.setText(f"Create failed — {exc}")
            self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
            return
        self._status.setText(
            f"Created {self._kind.lower()} tap changer on {transformer_id!r} "
            f"({len(steps)} steps)."
        )
        self._status.setStyleSheet("color: #0a7e2a; padding: 0 6px;")
        self.component_created.emit("Tap Changers", transformer_id)
        # Refresh the target picker — the transformer just got its tap changer.
        self._refresh_for_current()


# ---------------------------------------------------------------------------
# Coupling device creation panel (switches tying two busbar sections)
# ---------------------------------------------------------------------------
class CreateCouplingDevicePanel(QWidget):
    """Sub-form to create a coupling device inside a node-breaker VL.

    Auto-hides unless the active data-explorer component is "Switches"
    and the network has at least one node-breaker voltage level carrying
    two or more busbar sections.

    Layout:
      * VL picker (only node-breaker VLs with ≥2 BBS).
      * Two BBS pickers (refresh when the VL changes; default to distinct rows).
      * Optional switch-prefix text field.
      * Create button + status label.
    """

    component_created = Signal(str, str)  # ("Coupling Devices", vl_id)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._component: Optional[str] = None

        self._group = QGroupBox("Create a coupling device")
        self._group.setCheckable(True)
        self._group.setChecked(True)
        self._group.toggled.connect(self._on_toggled)

        self._vl_combo = QComboBox()
        self._vl_combo.setMinimumWidth(220)
        self._vl_combo.currentIndexChanged.connect(self._on_vl_changed)
        self._bbs1_combo = QComboBox()
        self._bbs1_combo.setMinimumWidth(200)
        self._bbs2_combo = QComboBox()
        self._bbs2_combo.setMinimumWidth(200)
        self._prefix_edit = QLineEdit()
        self._prefix_edit.setPlaceholderText("optional switch prefix")
        self._prefix_edit.setMaximumWidth(220)

        row_vl = QHBoxLayout()
        row_vl.addWidget(QLabel("Voltage level"))
        row_vl.addWidget(self._vl_combo, 1)

        row_bbs = QHBoxLayout()
        row_bbs.addWidget(QLabel("BBS 1"))
        row_bbs.addWidget(self._bbs1_combo, 1)
        row_bbs.addSpacing(10)
        row_bbs.addWidget(QLabel("BBS 2"))
        row_bbs.addWidget(self._bbs2_combo, 1)

        row_prefix = QHBoxLayout()
        row_prefix.addWidget(QLabel("Switch prefix"))
        row_prefix.addWidget(self._prefix_edit, 1)

        self._create_btn = QPushButton("Create coupling device")
        self._create_btn.clicked.connect(self._on_create_clicked)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        row_action = QHBoxLayout()
        row_action.addWidget(self._create_btn)
        row_action.addWidget(self._status, 1)

        inner = QVBoxLayout()
        inner.addLayout(row_vl)
        inner.addLayout(row_bbs)
        inner.addLayout(row_prefix)
        inner.addLayout(row_action)
        self._group.setLayout(inner)

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._group)
        self.setLayout(outer)
        self.setVisible(False)

    # -- Public API ------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._refresh_for_current()

    def set_component(self, component: Optional[str]) -> None:
        self._component = component
        self._refresh_for_current()
        self._status.setText("")

    # -- Internals -------------------------------------------------------
    def _on_toggled(self, checked: bool) -> None:
        for w in (
            self._vl_combo, self._bbs1_combo, self._bbs2_combo, self._prefix_edit,
        ):
            w.setVisible(checked)

    def _refresh_for_current(self) -> None:
        show = (
            self._network is not None
            and self._component == "Switches"
        )
        vls: list[tuple[str, str, float]] = []
        if show:
            vls = list_node_breaker_vls_with_multi_bbs(self._network)
        self.setVisible(bool(show and vls))
        if not (show and vls):
            return

        self._vl_combo.blockSignals(True)
        self._vl_combo.clear()
        for vl_id, display, kv in vls:
            label = f"{display} ({kv:.0f} kV)"
            self._vl_combo.addItem(label, userData=vl_id)
        self._vl_combo.blockSignals(False)
        self._refresh_bbs()

    def _on_vl_changed(self, _index: int) -> None:
        self._refresh_bbs()

    def _refresh_bbs(self) -> None:
        if self._network is None:
            return
        vl_id = self._vl_combo.currentData()
        if not vl_id:
            return
        ids = list_busbar_sections(self._network, vl_id)
        for combo in (self._bbs1_combo, self._bbs2_combo):
            combo.blockSignals(True)
            combo.clear()
            for bid in ids:
                combo.addItem(bid)
            combo.blockSignals(False)
        if self._bbs2_combo.count() > 1:
            self._bbs2_combo.setCurrentIndex(1)

    def _on_create_clicked(self) -> None:
        if self._network is None or self._component != "Switches":
            return
        bbs1 = self._bbs1_combo.currentText()
        bbs2 = self._bbs2_combo.currentText()
        prefix = self._prefix_edit.text().strip() or None
        try:
            create_coupling_device(self._network, bbs1, bbs2, prefix)
        except Exception as exc:
            self._status.setText(f"Create failed — {exc}")
            self._status.setStyleSheet("color: #b30000; padding: 0 6px;")
            return
        self._status.setText(f"Created coupling device between {bbs1} and {bbs2}.")
        self._status.setStyleSheet("color: #0a7e2a; padding: 0 6px;")
        vl_id = str(self._vl_combo.currentData() or "")
        self.component_created.emit("Coupling Devices", vl_id)
