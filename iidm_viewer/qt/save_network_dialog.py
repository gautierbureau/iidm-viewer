"""PySide6 "Save network" dialog.

A format picker on top, a :class:`ParametersForm` below that refreshes
every time the user picks a different export format. The host pairs
this dialog with a follow-up ``QFileDialog`` to pick the destination
path; the actual export goes through the shared
:func:`iidm_viewer.network_loader.export_network`.
"""
from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from iidm_viewer.io_options_schema import (
    filter_changed_params,
    get_format_parameters,
)
from iidm_viewer.qt.params_form import ParametersForm


class SaveNetworkDialog(QDialog):
    """Format picker + format-specific parameters for the Save flow."""

    def __init__(self, formats: Iterable[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save network")
        self.resize(560, 480)
        self.selected_format: Optional[str] = None
        self.parameters: dict[str, str] = {}

        intro = QLabel("Pick an export format and (optionally) tweak its parameters:")

        self._combo = QComboBox()
        for fmt in formats:
            self._combo.addItem(str(fmt))
        # Default to XIIDM when present — matches Streamlit's typical use.
        idx = self._combo.findText("XIIDM")
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
        self._combo.currentTextChanged.connect(self._on_format_changed)

        self._params_box = QGroupBox("Export parameters")
        self._params_layout = QVBoxLayout(self._params_box)
        self._params_layout.setContentsMargins(8, 8, 8, 8)
        self._params_form: Optional[ParametersForm] = None
        self._build_params_form()

        save_btn = QPushButton("Save…")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save_clicked)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self._combo)
        layout.addWidget(self._params_box, 1)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _on_format_changed(self, _text: str) -> None:
        self._build_params_form()

    def _build_params_form(self) -> None:
        while self._params_layout.count():
            item = self._params_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        fmt = self._combo.currentText()
        if not fmt:
            self._params_form = None
            return
        try:
            df = get_format_parameters("export", fmt)
        except Exception:
            df = None
        self._params_form = ParametersForm(df)
        self._params_layout.addWidget(self._params_form)

    def _on_save_clicked(self) -> None:
        self.selected_format = self._combo.currentText() or None
        if self._params_form is not None and self.selected_format:
            raw = self._params_form.read_values()
            try:
                df = get_format_parameters("export", self.selected_format)
            except Exception:
                df = None
            self.parameters = filter_changed_params(raw, df) if df is not None else {}
        else:
            self.parameters = {}
        self.accept()
