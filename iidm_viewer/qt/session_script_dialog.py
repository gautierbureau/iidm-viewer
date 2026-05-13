"""PySide6 "Session Script" dialog — preview + download the HMI-mirror script.

Mirrors Streamlit's :func:`iidm_viewer.session_script.show_session_script_dialog`:
the recorder collects ops as the user manipulates the network, this
dialog renders the resulting script in a read-only text view with a
Recording pause toggle, a "Include reverted edits" toggle, a download
button and a clear-log button.

All shared logic — reading the op log, generating the script —
delegates to :mod:`iidm_viewer.script_recorder` and
:mod:`iidm_viewer.script_generator` so the Streamlit dialog and this
one stay in lockstep.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer import script_recorder
from iidm_viewer.script_generator import generate_script


class SessionScriptDialog(QDialog):
    """Preview + download the auto-recorded session script."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Session Script")
        self.setMinimumSize(720, 520)

        caption = QLabel(
            "A runnable Python script that replays the operations you "
            "have performed in this session against any pypowsybl-"
            "loadable network."
        )
        caption.setWordWrap(True)
        caption.setStyleSheet("color: #555; padding: 4px 0;")

        # Toggle row: Recording + Include reverted.
        toggles_row = QHBoxLayout()
        self._recording_checkbox = QCheckBox("Recording")
        self._recording_checkbox.setChecked(not script_recorder.is_paused())
        self._recording_checkbox.setToolTip(
            "When off, new operations are not added to the log. The log "
            "itself is preserved. Loading a new network always re-enables "
            "recording."
        )
        self._recording_checkbox.stateChanged.connect(self._on_recording_changed)

        self._include_reverted_checkbox = QCheckBox("Include reverted edits")
        self._include_reverted_checkbox.setToolTip(
            "Off: the script reproduces the net state — reverted edits are "
            "dropped. On: every recorded operation is emitted in order, "
            "including reverts (full transcript)."
        )
        self._include_reverted_checkbox.stateChanged.connect(self._rerender)

        toggles_row.addWidget(self._recording_checkbox)
        toggles_row.addSpacing(20)
        toggles_row.addWidget(self._include_reverted_checkbox)
        toggles_row.addStretch(1)

        # Paused warning.
        self._paused_lbl = QLabel(
            "Recording is paused — new operations will not be captured."
        )
        self._paused_lbl.setStyleSheet(
            "padding: 4px 8px; color: #8a6d3b; background: #fcf8e3; "
            "border: 1px solid #faebcc; border-radius: 4px;"
        )
        self._paused_lbl.setVisible(script_recorder.is_paused())

        # Count + source caption.
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet("color: #555; padding: 2px 0;")

        # Script preview — monospace, read-only.
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.TypeWriter)
        mono.setPointSize(10)
        self._preview.setFont(mono)
        self._preview.setLineWrapMode(QPlainTextEdit.NoWrap)

        # Bottom action row: Download + Clear log + Close.
        action_row = QHBoxLayout()
        self._download_btn = QPushButton("Download script")
        self._download_btn.clicked.connect(self._on_download)
        self._clear_btn = QPushButton("Clear log")
        self._clear_btn.setToolTip(
            "Drop every recorded operation. Cannot be undone."
        )
        self._clear_btn.clicked.connect(self._on_clear)
        action_row.addWidget(self._download_btn)
        action_row.addWidget(self._clear_btn)
        action_row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        action_row.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(caption)
        layout.addLayout(toggles_row)
        layout.addWidget(self._paused_lbl)
        layout.addWidget(self._count_lbl)
        layout.addWidget(self._preview, 1)
        layout.addLayout(action_row)

        # Cached script body so the download button doesn't re-run the
        # generator just to grab the bytes.
        self._current_script: str = ""
        self._rerender()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _on_recording_changed(self, state: int) -> None:
        is_recording = bool(state == Qt.Checked.value)
        script_recorder.set_paused(not is_recording)
        self._paused_lbl.setVisible(not is_recording)
        # The script preview body doesn't change when toggling recording
        # — the log is preserved — but the count line is unaffected too.

    def _rerender(self) -> None:
        ops = script_recorder.get_log()
        include_reverted = self._include_reverted_checkbox.isChecked()
        source_filename = script_recorder.get_source_filename()
        script = generate_script(
            ops,
            include_reverted=include_reverted,
            source_filename=source_filename,
        )
        self._current_script = script
        self._preview.setPlainText(script)
        visible_count = sum(
            1 for o in ops if include_reverted or not o.get("reverted")
        )
        total = len(ops)
        reverted = total - sum(1 for o in ops if not o.get("reverted"))
        src_blurb = f" — source: {source_filename}" if source_filename else ""
        rev_blurb = f" ({reverted} reverted)" if reverted else ""
        self._count_lbl.setText(
            f"{visible_count} of {total} operation(s) emitted{rev_blurb}{src_blurb}"
        )

    def _on_download(self) -> None:
        ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"session_{ts_tag}.py"
        path, _ = QFileDialog.getSaveFileName(
            self, "Download script", default_name, "Python script (*.py)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._current_script)
        except OSError as exc:
            QMessageBox.warning(self, "Download failed", str(exc))

    def _on_clear(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Clear session script?",
            "Drop every recorded operation? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return
        script_recorder.clear_log()
        # Recording auto-resumes on a clear; reflect that in the toggle.
        self._recording_checkbox.blockSignals(True)
        try:
            self._recording_checkbox.setChecked(True)
        finally:
            self._recording_checkbox.blockSignals(False)
        self._paused_lbl.setVisible(False)
        self._rerender()
