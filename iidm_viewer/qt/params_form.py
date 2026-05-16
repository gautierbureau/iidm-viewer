"""Reusable Qt widget for a pypowsybl import/export parameters table.

Given a DataFrame from
:func:`iidm_viewer.io_options_schema.get_format_parameters`, builds
one widget per row (BOOLEAN → ``QCheckBox``; INTEGER / DOUBLE → spin
boxes; STRING with enumerated ``possible_values`` → ``QComboBox``;
STRING_LIST with options → ``QListWidget`` in extended-selection;
everything else → ``QLineEdit``).

The host calls :meth:`ParametersForm.read_values` after the user is
done editing; the result is fed through
:func:`io_options_schema.filter_changed_params` so only overrides hit
pypowsybl's import/export parameters dict.

Used by the PySide6 ``LoadOptionsDialog`` and ``SaveNetworkDialog`` —
keeps the per-type widget construction in one place.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.io_options_schema import (
    coerce_param_value,
    csv_split,
    parse_possible_values,
)


class ParametersForm(QWidget):
    """Stack of typed widgets bound to a parameters DataFrame.

    Pass ``initial`` to pre-populate the widgets from a prior save —
    keys not in ``params_df.index`` are ignored. The widget tracks
    the parameter type alongside each Qt control so
    :meth:`read_values` can produce the right wire format without
    re-reading the dataframe.
    """

    def __init__(
        self,
        params_df: pd.DataFrame,
        initial: dict[str, str] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._params_df = params_df if params_df is not None else pd.DataFrame()
        self._initial = initial or {}
        self._widgets: dict[str, tuple[str, QWidget]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if self._params_df is None or self._params_df.empty:
            layout.addWidget(QLabel("No configurable options for this format."))
            layout.addStretch(1)
            return

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setVerticalSpacing(6)
        for name, row in self._params_df.iterrows():
            ptype = str(row.get("type") or "STRING").upper()
            default = row.get("default") if "default" in self._params_df.columns else ""
            desc = str(row.get("description") or name)
            current = self._initial.get(str(name), default)
            options = parse_possible_values(row.get("possible_values"))

            widget = self._build_widget(ptype, current, default, options)
            self._widgets[str(name)] = (ptype, widget)
            label = QLabel(desc)
            label.setToolTip(str(name))
            form.addRow(label, widget)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_widget)
        layout.addWidget(scroll)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def read_values(self) -> dict[str, str]:
        """Return ``{param_name: wire-format-string}`` for every widget.

        Pair with
        :func:`io_options_schema.filter_changed_params` to drop entries
        that match the pypowsybl default.
        """
        out: dict[str, str] = {}
        for name, (ptype, widget) in self._widgets.items():
            out[name] = self._read_widget(ptype, widget)
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _build_widget(
        self, ptype: str, current: Any, default: Any, options: list[str],
    ) -> QWidget:
        # STRING_LIST with explicit options → multi-select list widget.
        if ptype == "STRING_LIST" and options:
            lst = QListWidget()
            lst.setSelectionMode(QAbstractItemView.MultiSelection)
            lst.setMaximumHeight(120)
            selected = set(csv_split(current))
            for opt in options:
                item = QListWidgetItem(opt)
                item.setSelected(opt in selected)
                lst.addItem(item)
            return lst

        # Plain enum (any non-list type with a fixed option set).
        if options:
            combo = QComboBox()
            for opt in options:
                combo.addItem(opt)
            idx = combo.findText(str(current))
            if idx >= 0:
                combo.setCurrentIndex(idx)
            return combo

        if ptype == "BOOLEAN":
            box = QCheckBox()
            box.setChecked(
                coerce_param_value("BOOLEAN", current, default) == "true"
            )
            return box
        if ptype == "INTEGER":
            spin = QSpinBox()
            spin.setRange(-2 ** 31, 2 ** 31 - 1)
            try:
                spin.setValue(int(float(current)))
            except (TypeError, ValueError):
                try:
                    spin.setValue(int(float(default)))
                except (TypeError, ValueError):
                    spin.setValue(0)
            return spin
        if ptype in ("DOUBLE", "FLOAT"):
            spin = QDoubleSpinBox()
            spin.setDecimals(6)
            spin.setRange(-1e15, 1e15)
            try:
                spin.setValue(float(current))
            except (TypeError, ValueError):
                try:
                    spin.setValue(float(default))
                except (TypeError, ValueError):
                    spin.setValue(0.0)
            return spin
        # Fallback: free text.
        return QLineEdit("" if current is None else str(current))

    def _read_widget(self, ptype: str, widget: QWidget) -> str:
        if isinstance(widget, QListWidget):
            items = [
                widget.item(i).text()
                for i in range(widget.count())
                if widget.item(i).isSelected()
            ]
            return ",".join(items)
        if isinstance(widget, QComboBox):
            return widget.currentText()
        if isinstance(widget, QCheckBox):
            return "true" if widget.isChecked() else "false"
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            return coerce_param_value(ptype, widget.value())
        if isinstance(widget, QLineEdit):
            return widget.text()
        return ""
