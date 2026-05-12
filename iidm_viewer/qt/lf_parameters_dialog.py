"""PySide6 "Load Flow Parameters" dialog.

Two tabs in a single ``QDialog``: the **Generic** tab is driven by the
shared :data:`iidm_viewer.loadflow.GENERIC_PARAMETERS` schema; the
**OpenLoadFlow** tab is driven by pypowsybl's
``get_provider_parameters()`` descriptor. All non-UI logic
(option-string parsing, type coercion, "changed vs default" filter,
category grouping) lives in :mod:`iidm_viewer.lf_parameters_schema`
so the Streamlit + NiceGUI dialogs share it.

On Save, the dialog writes the trimmed dicts onto
``AppState.lf_generic_params`` / ``AppState.lf_provider_params`` so
the next ``run_loadflow`` picks them up.
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.lf_parameters_schema import (
    coerce_provider_value,
    filter_changed_generic_params,
    filter_changed_provider_params,
    group_provider_params_by_category,
    parse_provider_options,
)
from iidm_viewer.loadflow import GENERIC_PARAMETERS, get_provider_parameters_df


class LFParametersDialog(QDialog):
    """Modal editor for AC load-flow parameters.

    Parameters
    ----------
    generic_overrides, provider_overrides
        Current overrides from the AppState. Each is a (possibly empty)
        dict — entries that match pypowsybl's defaults are dropped on
        save, so storing empty dicts is the canonical "no overrides".
    """

    def __init__(
        self,
        generic_overrides: Optional[dict] = None,
        provider_overrides: Optional[dict] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Load Flow Parameters")
        self.resize(720, 600)
        self._generic_overrides = dict(generic_overrides or {})
        self._provider_overrides = dict(provider_overrides or {})

        # Result: the dialog populates these on Save so the host can
        # read them after ``exec()`` returns ``QDialog.Accepted``.
        self.generic_params: dict = {}
        self.provider_params: dict = {}

        # Generic tab widgets (per-param) keyed by param name.
        self._generic_widgets: dict[str, QWidget] = {}
        # Provider tab widgets keyed by param name; we also remember
        # the param row so coerce_provider_value picks the right type
        # on Save without re-reading the dataframe.
        self._provider_widgets: dict[str, tuple[str, QWidget]] = {}
        self._provider_df = None

        tabs = QTabWidget(self)
        tabs.addTab(self._build_generic_tab(), "Generic Parameters")
        tabs.addTab(self._build_provider_tab(), "OpenLoadFlow Parameters")

        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save_clicked)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs, 1)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Generic tab
    # ------------------------------------------------------------------
    def _build_generic_tab(self) -> QWidget:
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setVerticalSpacing(8)

        for param_def in GENERIC_PARAMETERS:
            name, ptype, default, desc = param_def[0], param_def[1], param_def[2], param_def[3]
            current = self._generic_overrides.get(name, default)
            widget: QWidget
            if ptype == "bool":
                box = QCheckBox()
                box.setChecked(bool(current))
                widget = box
            elif ptype == "enum":
                combo = QComboBox()
                for opt in param_def[4]:
                    combo.addItem(opt)
                idx = combo.findText(str(current))
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                widget = combo
            elif ptype == "float":
                spin = QDoubleSpinBox()
                spin.setDecimals(6)
                spin.setRange(-1e15, 1e15)
                try:
                    spin.setValue(float(current))
                except (TypeError, ValueError):
                    spin.setValue(float(default))
                widget = spin
            else:
                widget = QLineEdit(str(current))

            self._generic_widgets[name] = widget
            label = QLabel(desc)
            label.setToolTip(name)
            form.addRow(label, widget)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_widget)
        return scroll

    def _read_generic_values(self) -> dict:
        out: dict[str, Any] = {}
        for param_def in GENERIC_PARAMETERS:
            name, ptype = param_def[0], param_def[1]
            widget = self._generic_widgets.get(name)
            if widget is None:
                continue
            if ptype == "bool":
                out[name] = widget.isChecked()
            elif ptype == "enum":
                out[name] = widget.currentText()
            elif ptype == "float":
                out[name] = widget.value()
            else:
                out[name] = widget.text()
        return out

    # ------------------------------------------------------------------
    # Provider tab
    # ------------------------------------------------------------------
    def _build_provider_tab(self) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(8, 8, 8, 8)

        try:
            df = get_provider_parameters_df()
        except Exception as exc:
            outer.addWidget(QLabel(f"Provider parameters unavailable: {exc}"))
            outer.addStretch(1)
            self._provider_df = None
            return container

        self._provider_df = df
        groups = group_provider_params_by_category(df)
        if not groups:
            outer.addWidget(QLabel("No provider parameters reported."))
            outer.addStretch(1)
            return container

        for category, rows in groups:
            box = QGroupBox(category)
            form = QFormLayout(box)
            form.setVerticalSpacing(6)
            for name, row in rows.iterrows():
                ptype = row["type"]
                default = row["default"]
                desc = row.get("description", "")
                current = self._provider_overrides.get(name, default)
                widget = self._build_provider_widget(ptype, current, default, row)
                self._provider_widgets[name] = (ptype, widget)
                label = QLabel(name)
                if desc:
                    label.setToolTip(desc)
                    widget.setToolTip(desc)
                form.addRow(label, widget)
            outer.addWidget(box)

        outer.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        return scroll

    def _build_provider_widget(
        self, ptype: str, current: Any, default: Any, row: Any,
    ) -> QWidget:
        if ptype == "BOOLEAN":
            box = QCheckBox()
            box.setChecked(coerce_provider_value(ptype, current, default))
            return box
        if ptype == "INTEGER":
            spin = QSpinBox()
            spin.setRange(-2 ** 31, 2 ** 31 - 1)
            spin.setValue(coerce_provider_value(ptype, current, default))
            return spin
        if ptype == "DOUBLE":
            spin = QDoubleSpinBox()
            spin.setDecimals(6)
            spin.setRange(-1e15, 1e15)
            spin.setValue(coerce_provider_value(ptype, current, default))
            return spin
        # STRING (or anything else): try enum first, fall back to text.
        options = parse_provider_options(row.get("possible_values"))
        if options:
            combo = QComboBox()
            for opt in options:
                combo.addItem(opt)
            idx = combo.findText(str(current))
            if idx >= 0:
                combo.setCurrentIndex(idx)
            return combo
        return QLineEdit("" if current is None else str(current))

    def _read_provider_values(self) -> dict:
        out: dict[str, Any] = {}
        for name, (ptype, widget) in self._provider_widgets.items():
            if isinstance(widget, QCheckBox):
                raw: Any = widget.isChecked()
            elif isinstance(widget, QComboBox):
                raw = widget.currentText()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                raw = widget.value()
            elif isinstance(widget, QLineEdit):
                raw = widget.text()
            else:
                continue
            out[name] = coerce_provider_value(ptype, raw)
        return out

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    def _on_save_clicked(self) -> None:
        self.generic_params = filter_changed_generic_params(self._read_generic_values())
        self.provider_params = filter_changed_provider_params(
            self._read_provider_values(), self._provider_df,
        )
        self.accept()
