"""PySide6 "Import options" dialog.

Mirrors Streamlit's ``_show_load_options_dialog`` in shape:

* A format picker (``Auto-detect`` + the pypowsybl import-format list).
* Format-specific parameters via :class:`ParametersForm`, refreshed
  every time the format changes.
* A post-processors checklist (multi-select).

On Save the dialog populates ``.format`` (``None`` for auto-detect),
``.params`` (only entries that differ from pypowsybl's default), and
``.post_processors`` (the checked names). The host writes them onto
``AppState.import_*`` so the next ``load_network_from_path`` picks
them up.
"""
from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.io_options_schema import (
    filter_changed_params,
    get_format_parameters,
)
from iidm_viewer.qt.params_form import ParametersForm

AUTO_DETECT = "Auto-detect"


class LoadOptionsDialog(QDialog):
    """Modal editor for the next file-load's import options."""

    def __init__(
        self,
        formats: Iterable[str],
        post_processors: Iterable[str],
        current_format: Optional[str] = None,
        current_params: Optional[dict] = None,
        current_post_processors: Optional[Iterable[str]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import options")
        self.resize(640, 560)

        # Save result.
        self.format: Optional[str] = None
        self.params: dict = {}
        self.post_processors: list[str] = []

        self._current_params_initial = dict(current_params or {})

        intro = QLabel(
            "Configure how the next file is parsed. ``Auto-detect`` lets "
            "pypowsybl pick the format from the file extension."
        )
        intro.setWordWrap(True)

        # Format selector.
        self._fmt_combo = QComboBox()
        self._fmt_combo.addItem(AUTO_DETECT)
        for f in formats:
            self._fmt_combo.addItem(str(f))
        if current_format:
            idx = self._fmt_combo.findText(current_format)
            if idx >= 0:
                self._fmt_combo.setCurrentIndex(idx)
        self._fmt_combo.currentTextChanged.connect(self._on_format_changed)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Import format"))
        fmt_row.addWidget(self._fmt_combo, 1)

        # Format-specific parameters — rebuilt on every format change.
        self._params_box = QGroupBox("Format parameters")
        self._params_box_layout = QVBoxLayout(self._params_box)
        self._params_box_layout.setContentsMargins(8, 8, 8, 8)
        self._params_form: Optional[ParametersForm] = None
        self._build_params_form()

        # Post-processors checklist.
        self._post_processor_boxes: dict[str, QCheckBox] = {}
        pp_box = QGroupBox("Post-processors")
        pp_inner = QWidget()
        pp_layout = QVBoxLayout(pp_inner)
        pp_layout.setContentsMargins(8, 4, 8, 8)
        checked = set(current_post_processors or [])
        for pp in post_processors:
            box = QCheckBox(str(pp))
            box.setChecked(pp in checked)
            self._post_processor_boxes[str(pp)] = box
            pp_layout.addWidget(box)
        if not self._post_processor_boxes:
            pp_layout.addWidget(QLabel("No post-processors reported."))
        pp_layout.addStretch(1)
        pp_scroll = QScrollArea()
        pp_scroll.setWidgetResizable(True)
        pp_scroll.setWidget(pp_inner)
        pp_outer = QVBoxLayout(pp_box)
        pp_outer.setContentsMargins(8, 8, 8, 8)
        pp_outer.addWidget(pp_scroll)

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
        layout.addWidget(intro)
        layout.addLayout(fmt_row)
        layout.addWidget(self._params_box)
        layout.addWidget(pp_box)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _selected_format(self) -> Optional[str]:
        fmt = self._fmt_combo.currentText()
        return None if fmt == AUTO_DETECT else fmt

    def _on_format_changed(self, _txt: str) -> None:
        # Reset the initial values when the user switches to a different
        # format — pypowsybl reports a different parameter set so the
        # old map doesn't fit any more.
        self._current_params_initial = {}
        self._build_params_form()

    def _build_params_form(self) -> None:
        # Drop the previous form (if any) and rebuild for the current format.
        while self._params_box_layout.count():
            item = self._params_box_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._params_form = None
        fmt = self._selected_format()
        if fmt is None:
            self._params_box.setVisible(False)
            return
        try:
            df = get_format_parameters("import", fmt)
        except Exception:
            df = None
        self._params_form = ParametersForm(df, self._current_params_initial)
        self._params_box_layout.addWidget(self._params_form)
        self._params_box.setVisible(True)

    def _on_save_clicked(self) -> None:
        self.format = self._selected_format()
        if self._params_form is not None and self.format is not None:
            raw = self._params_form.read_values()
            try:
                df = get_format_parameters("import", self.format)
            except Exception:
                df = None
            self.params = filter_changed_params(raw, df) if df is not None else {}
        else:
            self.params = {}
        self.post_processors = [
            name for name, box in self._post_processor_boxes.items()
            if box.isChecked()
        ]
        self.accept()
