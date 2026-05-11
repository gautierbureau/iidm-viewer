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
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.component_creation import (
    CREATABLE_COMPONENTS,
    LOCATOR_FIELDS,
    coerce_field_values,
    create_component_bay,
    list_busbar_sections,
    list_node_breaker_voltage_levels,
)
from iidm_viewer.powsybl_worker import NetworkProxy


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

    def _read_widget(self, field: dict) -> Any:
        w = self._field_widgets.get(field["name"])
        if w is None:
            return None
        kind = field["kind"]
        if kind == "text":
            return w.text()
        if kind in ("float", "int"):
            return w.value()
        if kind == "bool":
            return w.isChecked()
        if kind == "select":
            return w.currentText()
        return None

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
