"""Pmax Visualization tab — PySide6 host.

Composes the shared :mod:`iidm_viewer.pmax_visualization` core with
PySide6 widgets:

* a "Only lines connected to VL X" checkbox that drives the shared
  :func:`~iidm_viewer.pmax_visualization.filter_by_vl`,
* a summary ``QTableView`` of lines ranked by margin (red / orange
  / green cells map to the shared ``ratio_color`` / ``margin_color``
  classifiers),
* a line picker + four-metric header,
* the Plotly P-δ chart rendered into a ``QWebEngineView`` (same
  temp-file trick as the Reactive Capability Curves tab — see
  ``qt/reactive_curves_tab._load_plot_html``).

All pypowsybl calls hop through the worker thread (per AGENTS.md §1)
via :func:`iidm_viewer.pmax_visualization.compute_pmax_data`.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

import pandas as pd
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.pmax_visualization import (
    DISPLAY_COLUMNS,
    build_display_dataframe,
    build_pangle_chart,
    compute_pmax_data,
    filter_by_vl,
    margin_color,
    ratio_color,
)
from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.qt.data_explorer_tab import PandasTableModel


# Map shared classifier output → Qt colour. ``unknown`` falls back to
# the default cell brush.
_COLOR_RGB: dict = {
    "warning": QColor(255, 75, 75),
    "caution": QColor(255, 165, 0),
    "safe": None,
    "unknown": None,
}


class _PmaxSummaryModel(PandasTableModel):
    """Pandas-backed table model that colours the ``P/Pmax`` and
    ``Margin (%)`` cells via the shared classifiers."""

    _ratio_col: int = -1
    _margin_col: int = -1

    def set_dataframe(self, df, editable_cols=None) -> None:  # type: ignore[override]
        super().set_dataframe(df, editable_cols)
        self._ratio_col = (
            df.columns.get_loc("P/Pmax") if "P/Pmax" in df.columns else -1
        )
        self._margin_col = (
            df.columns.get_loc("Margin (%)")
            if "Margin (%)" in df.columns else -1
        )

    def data(self, index, role=Qt.DisplayRole):  # type: ignore[override]
        if not index.isValid():
            return None
        if role == Qt.BackgroundRole:
            col = index.column()
            value = self._df.iat[index.row(), col]
            if col == self._ratio_col:
                rgb = _COLOR_RGB.get(ratio_color(value))
                if rgb is not None:
                    return QBrush(rgb)
            elif col == self._margin_col:
                rgb = _COLOR_RGB.get(margin_color(value))
                if rgb is not None:
                    return QBrush(rgb)
            return None
        if role == Qt.ForegroundRole:
            col = index.column()
            value = self._df.iat[index.row(), col]
            kind = None
            if col == self._ratio_col:
                kind = ratio_color(value)
            elif col == self._margin_col:
                kind = margin_color(value)
            if kind == "warning":
                return QBrush(QColor("white"))
            return None
        return super().data(index, role)


class PmaxVisualizationTab(QWidget):
    """Tab body. Owns the per-network DataFrame + the chart temp file."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._selected_vl: Optional[str] = None
        self._df: pd.DataFrame = pd.DataFrame()
        self._unfiltered: pd.DataFrame = pd.DataFrame()
        self._plot_tmp: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        caption = QLabel(
            "For each line: Pmax = V₁ × V₂ / X (V in kV, X in Ω, "
            "result in MW). The ratio P/Pmax = sin(δ) shows proximity "
            "to the steady-state stability limit — the operating point "
            "reaches the limit when δ = 90°."
        )
        caption.setWordWrap(True)
        caption.setStyleSheet("color: #555; padding: 4px;")
        layout.addWidget(caption)

        # "Only lines connected to VL X" — same UX as Reactive Curves.
        self._only_vl_checkbox = QCheckBox(
            "Only lines connected to selected VL",
        )
        self._only_vl_checkbox.setVisible(False)
        self._only_vl_checkbox.stateChanged.connect(self._on_only_vl_toggled)
        layout.addWidget(self._only_vl_checkbox)

        self._placeholder = QLabel(
            "No data available. Make sure a load flow has been run "
            "and the network contains transmission lines."
        )
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #666; padding: 8px;")
        layout.addWidget(self._placeholder)

        # Summary group.
        self._summary_group = QGroupBox(
            "Lines sorted by proximity to stability limit",
        )
        sg_layout = QVBoxLayout(self._summary_group)
        self._summary_view = QTableView()
        self._summary_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._summary_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._summary_view.setMaximumHeight(220)
        self._summary_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._summary_model = _PmaxSummaryModel()
        self._summary_view.setModel(self._summary_model)
        sg_layout.addWidget(self._summary_view)
        layout.addWidget(self._summary_group)

        # Detail group.
        self._detail_group = QGroupBox("Power-angle characteristic")
        dg_layout = QVBoxLayout(self._detail_group)
        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Line:"))
        self._line_combo = QComboBox()
        self._line_combo.setMinimumWidth(220)
        self._line_combo.currentTextChanged.connect(self._on_line_changed)
        picker_row.addWidget(self._line_combo)
        picker_row.addStretch(1)
        dg_layout.addLayout(picker_row)

        metrics_row = QHBoxLayout()
        self._pmax_lbl = QLabel("Pmax: —")
        self._pactual_lbl = QLabel("P: —")
        self._ratio_lbl = QLabel("P/Pmax: —")
        self._delta_lbl = QLabel("δ: —")
        for lbl in (
            self._pmax_lbl, self._pactual_lbl,
            self._ratio_lbl, self._delta_lbl,
        ):
            lbl.setStyleSheet(
                "padding: 4px 8px; border: 1px solid #ddd; "
                "border-radius: 4px; background: #fafafa;",
            )
            metrics_row.addWidget(lbl, 1)
        dg_layout.addLayout(metrics_row)

        self._plot_view = QWebEngineView()
        self._plot_view.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding,
        )
        self._plot_view.setMinimumHeight(400)
        dg_layout.addWidget(self._plot_view, 1)
        layout.addWidget(self._detail_group, 1)

        self._set_data_visible(False)

    # ------------------------------------------------------------------
    # Public API (mirrors the other Qt tabs).
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self.refresh()

    def set_selected_vl(self, vl_id: Optional[str]) -> None:
        self._selected_vl = vl_id or None
        if vl_id:
            self._only_vl_checkbox.setText(
                f"Only lines connected to VL {vl_id}",
            )
        self._update_vl_checkbox_visibility()
        self._apply_vl_filter()

    def refresh(self) -> None:
        """Recompute the Pmax DataFrame + redraw everything."""
        if self._network is None:
            self._unfiltered = pd.DataFrame()
            self._df = pd.DataFrame()
            self._placeholder.setText(
                "Load a network to see Pmax visualization.",
            )
            self._set_data_visible(False)
            return
        try:
            self._unfiltered = compute_pmax_data(self._network)
        except Exception as exc:
            self._unfiltered = pd.DataFrame()
            self._placeholder.setText(f"Pmax visualization failed: {exc}")
            self._set_data_visible(False)
            return
        if self._unfiltered.empty:
            self._df = pd.DataFrame()
            self._placeholder.setText(
                "No data available. Make sure a load flow has been run "
                "and the network contains transmission lines.",
            )
            self._set_data_visible(False)
            return
        self._update_vl_checkbox_visibility()
        self._apply_vl_filter()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _on_only_vl_toggled(self, _state: int) -> None:
        self._apply_vl_filter()

    def _on_line_changed(self, line_id: str) -> None:
        if not line_id or self._df.empty or line_id not in self._df.index:
            self._reset_metrics()
            self._plot_view.setHtml("")
            return
        row = self._df.loc[line_id]
        self._update_metrics(row)
        self._load_plot_html(
            build_pangle_chart(line_id, row).to_html(
                include_plotlyjs="inline", full_html=True,
            ),
        )

    def _update_vl_checkbox_visibility(self) -> None:
        if (
            self._selected_vl
            and not self._unfiltered.empty
            and not filter_by_vl(
                self._unfiltered, self._selected_vl,
            ).empty
        ):
            self._only_vl_checkbox.setVisible(True)
        else:
            self._only_vl_checkbox.setVisible(False)
            self._only_vl_checkbox.setChecked(False)

    def _apply_vl_filter(self) -> None:
        if self._unfiltered.empty:
            self._df = pd.DataFrame()
            self._set_data_visible(False)
            return
        if self._only_vl_checkbox.isChecked() and self._selected_vl:
            self._df = filter_by_vl(self._unfiltered, self._selected_vl)
        else:
            self._df = self._unfiltered
        if self._df.empty:
            self._placeholder.setText(
                "No lines match the current filter.",
            )
            self._set_data_visible(False)
            return
        self._set_data_visible(True)
        self._summary_model.set_dataframe(build_display_dataframe(self._df))
        self._summary_view.resizeColumnsToContents()
        self._refresh_line_combo()

    def _refresh_line_combo(self) -> None:
        line_ids = [str(x) for x in self._df.index.tolist()]
        previous = self._line_combo.currentText()
        self._line_combo.blockSignals(True)
        self._line_combo.clear()
        self._line_combo.addItems(line_ids)
        if previous in line_ids:
            self._line_combo.setCurrentText(previous)
        elif line_ids:
            self._line_combo.setCurrentIndex(0)
        self._line_combo.blockSignals(False)
        # Trigger the renderer for whichever line is now selected.
        self._on_line_changed(self._line_combo.currentText())

    def _update_metrics(self, row: pd.Series) -> None:
        pmax = row["pmax_mw"]
        p_actual = row["p_actual_mw"]
        ratio_val = row["p_pmax_ratio"]
        margin_val = row["margin_pct"]
        delta = row["delta_deg"]
        self._pmax_lbl.setText(f"Pmax: {pmax:.1f} MW")
        self._pactual_lbl.setText(f"P: {p_actual:.1f} MW")
        if pd.notna(ratio_val):
            ratio_str = f"P/Pmax: {ratio_val:.1%}"
            if pd.notna(margin_val):
                ratio_str += f"  (margin {margin_val:.1f} %)"
            self._ratio_lbl.setText(ratio_str)
        else:
            self._ratio_lbl.setText("P/Pmax: N/A")
        self._delta_lbl.setText(
            f"δ: {delta:.1f}°" if pd.notna(delta) else "δ: N/A",
        )

    def _reset_metrics(self) -> None:
        self._pmax_lbl.setText("Pmax: —")
        self._pactual_lbl.setText("P: —")
        self._ratio_lbl.setText("P/Pmax: —")
        self._delta_lbl.setText("δ: —")

    def _set_data_visible(self, visible: bool) -> None:
        self._placeholder.setVisible(not visible)
        self._summary_group.setVisible(visible)
        self._detail_group.setVisible(visible)
        if not visible:
            self._summary_model.set_dataframe(
                pd.DataFrame(columns=DISPLAY_COLUMNS),
            )
            self._line_combo.blockSignals(True)
            self._line_combo.clear()
            self._line_combo.blockSignals(False)
            self._reset_metrics()
            self._plot_view.setHtml("")

    def _load_plot_html(self, html: str) -> None:
        """Write *html* to a temp file and load it via ``setUrl``.

        ``QWebEngineView.setHtml`` truncates content past ~2 MB after
        percent-encoding; the inline Plotly JS alone is ~4.5 MB. Same
        trick as ``qt/reactive_curves_tab._load_plot_html``.
        """
        self._cleanup_plot_tmp()
        fd, path = tempfile.mkstemp(suffix=".html", prefix="iidm_pmax_")
        try:
            os.write(fd, html.encode("utf-8"))
        finally:
            os.close(fd)
        self._plot_tmp = path
        self._plot_view.setUrl(QUrl.fromLocalFile(path))

    def _cleanup_plot_tmp(self) -> None:
        if self._plot_tmp and os.path.isfile(self._plot_tmp):
            try:
                os.unlink(self._plot_tmp)
            except OSError:
                pass
        self._plot_tmp = None
