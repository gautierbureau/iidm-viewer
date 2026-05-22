"""Operational Limits tab — PySide6 host.

Composes the shared
:class:`~iidm_viewer.operational_limits.OperationalLimitsViewModel`
+ :func:`build_element_chart` with PySide6 widgets:

* a "Most loaded" section with a percentage slider and a
  ``QTableView`` colored by loading band,
* an element ID filter + ``QComboBox`` for the per-element view,
* a losses ``QLabel`` and a Plotly chart rendered in a
  ``QWebEngineView`` (same widget that hosts the SLD / NAD bundles),
* the raw limits ``QTableView`` for the selected element.

All pypowsybl calls hop through the worker thread via
:func:`iidm_viewer.operational_limits.build_operational_limits_view_model`.
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
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSlider,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.operational_limits import (
    OperationalLimitsViewModel,
    build_element_chart,
    build_operational_limits_view_model,
)
from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.qt.data_explorer_tab import PandasTableModel


_LOADING_RED = QColor(255, 75, 75)
_LOADING_ORANGE = QColor(255, 165, 0)


class _LoadingTableModel(PandasTableModel):
    """``PandasTableModel`` subclass that color-codes the "Loading (%)"
    column based on the value (≥100 red, ≥80 orange).

    Done as a thin subclass so the rest of the Data Explorer model
    (sort, filter, edit handling) stays single-purpose.
    """

    _loading_col: Optional[int] = None

    def set_dataframe(self, df, editable_cols=None) -> None:  # type: ignore[override]
        super().set_dataframe(df, editable_cols=editable_cols)
        self._loading_col = None
        if df is not None and "Loading (%)" in df.columns:
            self._loading_col = list(df.columns).index("Loading (%)")

    def data(self, index, role=Qt.DisplayRole):  # type: ignore[override]
        if role == Qt.BackgroundRole and self._loading_col is not None:
            if index.column() == self._loading_col:
                try:
                    val = float(self._df.iat[index.row(), index.column()])
                except (TypeError, ValueError):
                    return None
                if val >= 100:
                    return QBrush(_LOADING_RED)
                if val >= 80:
                    return QBrush(_LOADING_ORANGE)
                return None
        return super().data(index, role)


class OperationalLimitsTab(QWidget):
    """Tab body. Owns the view model + selection state."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._view_model: Optional[OperationalLimitsViewModel] = None
        self._element_id: Optional[str] = None
        self._threshold: int = 50
        self._plot_tmp: Optional[str] = None  # temp file for Plotly HTML

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Placeholder shown when there's no data.
        self._placeholder = QLabel("Load a network to see operational limits.")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #666; padding: 8px;")
        root.addWidget(self._placeholder)

        # --- Most loaded section --------------------------------------
        self._loading_group = QGroupBox("Most loaded elements")
        loading_layout = QVBoxLayout(self._loading_group)
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(QLabel("Show elements loaded above"))
        self._threshold_slider = QSlider(Qt.Horizontal)
        self._threshold_slider.setRange(0, 100)
        self._threshold_slider.setValue(self._threshold)
        self._threshold_slider.valueChanged.connect(self._on_threshold_changed)
        threshold_row.addWidget(self._threshold_slider, 1)
        self._threshold_value_lbl = QLabel(f"{self._threshold}%")
        self._threshold_value_lbl.setMinimumWidth(40)
        threshold_row.addWidget(self._threshold_value_lbl)
        loading_layout.addLayout(threshold_row)
        self._loading_caption = QLabel("")
        self._loading_caption.setStyleSheet("color: #555;")
        loading_layout.addWidget(self._loading_caption)
        self._loading_view = QTableView()
        self._loading_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._loading_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._loading_view.setMaximumHeight(220)
        self._loading_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._loading_model = _LoadingTableModel()
        self._loading_view.setModel(self._loading_model)
        loading_layout.addWidget(self._loading_view)
        root.addWidget(self._loading_group)

        # --- Element detail section -----------------------------------
        self._detail_group = QGroupBox("Element detail")
        detail_layout = QVBoxLayout(self._detail_group)
        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Filter:"))
        self._id_filter = QLineEdit()
        self._id_filter.setPlaceholderText(
            "Element ID substring (case-insensitive)",
        )
        self._id_filter.textChanged.connect(self._on_id_filter_changed)
        picker_row.addWidget(self._id_filter, 1)
        picker_row.addSpacing(10)
        picker_row.addWidget(QLabel("Element:"))
        self._element_combo = QComboBox()
        self._element_combo.setMinimumWidth(220)
        self._element_combo.currentTextChanged.connect(self._on_element_changed)
        picker_row.addWidget(self._element_combo)
        self._element_count_lbl = QLabel("")
        self._element_count_lbl.setStyleSheet("color: #666;")
        picker_row.addWidget(self._element_count_lbl)
        picker_row.addStretch(1)
        detail_layout.addLayout(picker_row)
        self._losses_lbl = QLabel("")
        self._losses_lbl.setStyleSheet("padding: 4px 8px; color: #444;")
        detail_layout.addWidget(self._losses_lbl)
        self._plot_view = QWebEngineView()
        self._plot_view.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding,
        )
        self._plot_view.setMinimumHeight(420)
        detail_layout.addWidget(self._plot_view, 1)
        self._limits_view = QTableView()
        self._limits_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._limits_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._limits_view.setMaximumHeight(200)
        self._limits_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive,
        )
        self._limits_model = PandasTableModel()
        self._limits_view.setModel(self._limits_model)
        detail_layout.addWidget(self._limits_view)
        root.addWidget(self._detail_group, 1)

    # ------------------------------------------------------------------
    # Public API (mirrors the other Qt tabs).
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._view_model = None
        self._element_id = None
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the view model + redraw everything."""
        if self._network is None:
            self._view_model = None
            self._placeholder.setText("Load a network to see operational limits.")
            self._set_data_visible(False)
            return
        try:
            vm = build_operational_limits_view_model(self._network)
        except Exception as exc:
            self._view_model = None
            self._placeholder.setText(f"Operational limits failed: {exc}")
            self._set_data_visible(False)
            return
        if vm is None:
            self._view_model = None
            self._placeholder.setText(
                "No operational limits found in this network.",
            )
            self._set_data_visible(False)
            return
        self._view_model = vm
        self._set_data_visible(True)
        self._render_loading_table()
        self._refresh_element_choices()
        self._render_selected_element()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _set_data_visible(self, visible: bool) -> None:
        self._loading_group.setVisible(visible)
        self._detail_group.setVisible(visible)
        self._placeholder.setVisible(not visible)

    def _on_threshold_changed(self, value: int) -> None:
        self._threshold = int(value)
        self._threshold_value_lbl.setText(f"{self._threshold}%")
        self._render_loading_table()

    def _on_id_filter_changed(self, _text: str) -> None:
        self._refresh_element_choices()
        self._render_selected_element()

    def _on_element_changed(self, element_id: str) -> None:
        if not element_id:
            return
        self._element_id = element_id
        self._render_selected_element()

    def _render_loading_table(self) -> None:
        vm = self._view_model
        if vm is None:
            self._loading_model.set_dataframe(pd.DataFrame())
            self._loading_caption.setText("")
            return
        loading = vm.loading_df
        if loading is None or loading.empty:
            self._loading_model.set_dataframe(pd.DataFrame())
            self._loading_caption.setText(
                "No loading data available (run a load flow first).",
            )
            return
        above = loading[loading["loading_pct"] >= self._threshold].copy()
        if above.empty:
            self._loading_model.set_dataframe(pd.DataFrame())
            self._loading_caption.setText(
                f"No elements loaded above {self._threshold}%.",
            )
            return
        show = above[["element_id", "element_name", "element_type", "side",
                      "current", "permanent_limit", "loading_pct",
                      "losses"]].copy()
        show.columns = ["Element", "Name", "Type", "Worst side",
                        "I (A)", "Permanent limit (A)", "Loading (%)",
                        "Losses (MW)"]
        show["Worst side"] = show["Worst side"].map(
            {"ONE": "Side 1", "TWO": "Side 2"})
        show["I (A)"] = show["I (A)"].round(1)
        show["Loading (%)"] = show["Loading (%)"].round(1)
        show["Losses (MW)"] = show["Losses (MW)"].round(3)
        self._loading_model.set_dataframe(show)
        self._loading_caption.setText(
            f"{len(above)} elements above {self._threshold}%",
        )
        self._loading_view.resizeColumnsToContents()

    def _refresh_element_choices(self) -> None:
        vm = self._view_model
        if vm is None:
            self._element_combo.blockSignals(True)
            try:
                self._element_combo.clear()
            finally:
                self._element_combo.blockSignals(False)
            self._element_count_lbl.setText("")
            return
        candidates = list(vm.element_ids)
        id_filter = (self._id_filter.text() or "").strip()
        if id_filter:
            f = id_filter.lower()
            candidates = [e for e in candidates if f in str(e).lower()]
        self._element_count_lbl.setText(
            f"{len(candidates)} element(s) with limits",
        )
        self._element_combo.blockSignals(True)
        try:
            self._element_combo.clear()
            self._element_combo.addItems([str(e) for e in candidates])
            current = (
                self._element_id
                if self._element_id in candidates
                else (candidates[0] if candidates else None)
            )
            self._element_id = current
            if current is not None:
                idx = candidates.index(current)
                self._element_combo.setCurrentIndex(idx)
        finally:
            self._element_combo.blockSignals(False)

    def _render_selected_element(self) -> None:
        vm = self._view_model
        element_id = self._element_id
        if vm is None or not element_id:
            self._losses_lbl.setText("")
            self._plot_view.setHtml("")
            self._cleanup_plot_tmp()
            self._limits_model.set_dataframe(pd.DataFrame())
            return
        elem_limits = vm.display_limits_df[
            vm.display_limits_df["element_id"] == element_id
        ]
        if elem_limits.empty:
            self._losses_lbl.setText("")
            self._plot_view.setHtml("")
            self._cleanup_plot_tmp()
            self._limits_model.set_dataframe(pd.DataFrame())
            return
        # Losses metric.
        loss = vm.losses.get(element_id)
        if loss is not None and pd.notna(loss):
            self._losses_lbl.setText(
                f"Active-power losses: {loss:.3f} MW",
            )
        else:
            self._losses_lbl.setText(
                "Losses unavailable (run a load flow to compute p1 + p2).",
            )
        # Plotly chart → embedded HTML.
        fig = build_element_chart(
            element_id, elem_limits, vm.flows.get(element_id),
        )
        # Plotly inline HTML is ~4–5 MB, exceeding QWebEngineView's
        # ~2 MB setHtml() IPC limit. Write to a temp file instead.
        html = fig.to_html(include_plotlyjs="inline", full_html=True)
        self._load_plot_html(html)
        # Raw limits table.
        cols = [c for c in
                ("side", "name", "acceptable_duration", "value", "element_type")
                if c in elem_limits.columns]
        sort_cols = ["side", "acceptable_duration"] \
            if "acceptable_duration" in cols else cols
        show = elem_limits[cols].sort_values(sort_cols).reset_index(drop=True)
        self._limits_model.set_dataframe(show)
        self._limits_view.resizeColumnsToContents()

    def _load_plot_html(self, html: str) -> None:
        """Write *html* to a temp file and point the QWebEngineView at it.

        ``QWebEngineView.setHtml()`` silently truncates content beyond
        ~2 MB.  Plotly's inline JS alone is ~4.5 MB, so the chart would
        never render.  A temp file + ``setUrl()`` bypasses the limit.
        """
        self._cleanup_plot_tmp()
        fd, path = tempfile.mkstemp(suffix=".html", prefix="iidm_ol_")
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
