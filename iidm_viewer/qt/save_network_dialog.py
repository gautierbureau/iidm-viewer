"""PySide6 "Save network" format-picker dialog.

A thin modal — a label and a ``QComboBox`` listing pypowsybl's export
formats. The host pairs this with a follow-up ``QFileDialog`` to pick
the destination path; the actual export goes through the shared
:func:`iidm_viewer.network_loader.export_network`.

Kept lightweight on purpose: format-specific options (the
``get_format_parameters`` machinery Streamlit uses) are out of scope
for this minimal port. The pypowsybl defaults work for the common
formats; we can grow this dialog later if a user needs to tune one.
"""
from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


class SaveNetworkDialog(QDialog):
    """Format picker for the "Save network" sidebar action."""

    def __init__(self, formats: Iterable[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save network")
        self.resize(360, 140)
        self.selected_format: Optional[str] = None

        intro = QLabel("Pick an export format:")
        self._combo = QComboBox()
        for fmt in formats:
            self._combo.addItem(str(fmt))
        # Default to XIIDM when present — matches Streamlit's typical use.
        idx = self._combo.findText("XIIDM")
        if idx >= 0:
            self._combo.setCurrentIndex(idx)

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
        layout.addStretch(1)
        layout.addLayout(btn_row)

    def _on_save_clicked(self) -> None:
        self.selected_format = self._combo.currentText() or None
        self.accept()
