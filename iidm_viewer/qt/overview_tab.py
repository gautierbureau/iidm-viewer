"""Overview tab — PySide6 host.

Composes the shared :mod:`iidm_viewer.network_info_core` core with
PySide6 widgets:

* a four-metric header (ID / Name / Format / Case Date),
* a "Generation and Consumption by Country" ``QTableView``,
* a "Network Losses" row of three metrics + an optional per-country
  ``QTableView``,
* a collapsible "Component Statistics" grid of metric labels.

All pypowsybl calls hop through the worker thread (per AGENTS.md §1)
via :func:`iidm_viewer.network_info_core.compute_overview_data`.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.network_info_core import (
    COUNTRY_TOTALS_DISPLAY_COLUMNS,
    LOSSES_BY_COUNTRY_COLUMNS,
    build_country_totals_display,
    build_losses_by_country_display,
    compute_overview_data,
    country_totals_has_lf,
)
from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.qt.data_explorer_tab import PandasTableModel


_METRIC_STYLE = (
    "padding: 6px 10px; border: 1px solid #ddd; "
    "border-radius: 4px; background: #fafafa;"
)


def _new_metric_label() -> QLabel:
    lbl = QLabel("—")
    lbl.setStyleSheet(_METRIC_STYLE)
    lbl.setWordWrap(True)
    return lbl


def _new_table(max_height: Optional[int] = None) -> QTableView:
    view = QTableView()
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setEditTriggers(QAbstractItemView.NoEditTriggers)
    view.setAlternatingRowColors(True)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    view.verticalHeader().setVisible(False)
    view.setSortingEnabled(True)
    if max_height is not None:
        view.setMaximumHeight(max_height)
    return view


class OverviewTab(QWidget):
    """Tab body. Owns the per-network DataFrames + the table models."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._placeholder = QLabel("Load a network to see the overview.")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #666; padding: 8px;")
        layout.addWidget(self._placeholder)

        # ------------------------------------------------------------------
        # Metadata header — Network ID / Name / Format / Case Date.
        # ------------------------------------------------------------------
        self._meta_row = QWidget()
        meta_layout = QHBoxLayout(self._meta_row)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        self._meta_id = _new_metric_label()
        self._meta_name = _new_metric_label()
        self._meta_format = _new_metric_label()
        self._meta_case_date = _new_metric_label()
        for lbl in (
            self._meta_id, self._meta_name,
            self._meta_format, self._meta_case_date,
        ):
            meta_layout.addWidget(lbl, 1)
        layout.addWidget(self._meta_row)

        # ------------------------------------------------------------------
        # Country totals
        # ------------------------------------------------------------------
        self._country_group = QGroupBox("Generation and Consumption by Country")
        cg_layout = QVBoxLayout(self._country_group)
        self._country_empty_lbl = QLabel(
            "No generation or consumption data available.",
        )
        self._country_empty_lbl.setStyleSheet("color: #666;")
        cg_layout.addWidget(self._country_empty_lbl)
        self._country_lf_caption = QLabel(
            "Actual values populate once a load flow has run.",
        )
        self._country_lf_caption.setStyleSheet("color: #555;")
        cg_layout.addWidget(self._country_lf_caption)
        self._country_table = _new_table(max_height=260)
        self._country_model = PandasTableModel()
        self._country_table.setModel(self._country_model)
        cg_layout.addWidget(self._country_table)
        layout.addWidget(self._country_group)

        # ------------------------------------------------------------------
        # Losses
        # ------------------------------------------------------------------
        self._losses_group = QGroupBox("Network Losses")
        ll_layout = QVBoxLayout(self._losses_group)
        self._losses_empty_lbl = QLabel(
            "No loss data available (run a load flow first).",
        )
        self._losses_empty_lbl.setStyleSheet("color: #666;")
        ll_layout.addWidget(self._losses_empty_lbl)
        metrics_row = QHBoxLayout()
        self._losses_total_lbl = _new_metric_label()
        self._losses_lines_lbl = _new_metric_label()
        self._losses_xfmr_lbl = _new_metric_label()
        for lbl in (
            self._losses_total_lbl,
            self._losses_lines_lbl,
            self._losses_xfmr_lbl,
        ):
            metrics_row.addWidget(lbl, 1)
        ll_layout.addLayout(metrics_row)
        self._losses_by_country_caption = QLabel(
            "Losses by country — cross-border branches split 50/50.",
        )
        self._losses_by_country_caption.setStyleSheet("color: #555;")
        ll_layout.addWidget(self._losses_by_country_caption)
        self._losses_by_country_table = _new_table(max_height=220)
        self._losses_by_country_model = PandasTableModel()
        self._losses_by_country_table.setModel(self._losses_by_country_model)
        ll_layout.addWidget(self._losses_by_country_table)
        layout.addWidget(self._losses_group)

        # ------------------------------------------------------------------
        # Component statistics (collapsible)
        # ------------------------------------------------------------------
        self._counts_group = QGroupBox("Component Statistics")
        cs_layout = QVBoxLayout(self._counts_group)
        self._counts_toggle = QToolButton()
        self._counts_toggle.setText("Show component counts")
        self._counts_toggle.setCheckable(True)
        self._counts_toggle.setChecked(False)
        self._counts_toggle.setArrowType(Qt.RightArrow)
        self._counts_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._counts_toggle.toggled.connect(self._on_counts_toggled)
        cs_layout.addWidget(
            self._counts_toggle, alignment=Qt.AlignLeft,
        )
        self._counts_body = QWidget()
        self._counts_grid = QGridLayout(self._counts_body)
        self._counts_grid.setContentsMargins(0, 6, 0, 0)
        self._counts_body.setVisible(False)
        cs_layout.addWidget(self._counts_body)
        self._counts_empty_lbl = QLabel(
            "No components found in this network.",
        )
        self._counts_empty_lbl.setStyleSheet("color: #666;")
        self._counts_empty_lbl.setVisible(False)
        cs_layout.addWidget(self._counts_empty_lbl)
        layout.addWidget(self._counts_group)
        layout.addStretch(1)

        self._set_data_visible(False)

    # ------------------------------------------------------------------
    # Public API (mirrors the other Qt tabs).
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self.refresh()

    def refresh(self) -> None:
        """Recompute every section + redraw."""
        if self._network is None:
            self._placeholder.setText("Load a network to see the overview.")
            self._set_data_visible(False)
            return
        try:
            data = compute_overview_data(self._network)
        except Exception as exc:
            self._placeholder.setText(f"Overview failed: {exc}")
            self._set_data_visible(False)
            return
        self._set_data_visible(True)
        self._render_metadata(data.metadata)
        self._render_country_totals(data.country_totals)
        self._render_losses(data.losses, data.losses_by_country)
        self._render_component_counts(data.component_counts)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _set_data_visible(self, visible: bool) -> None:
        self._placeholder.setVisible(not visible)
        self._meta_row.setVisible(visible)
        self._country_group.setVisible(visible)
        self._losses_group.setVisible(visible)
        self._counts_group.setVisible(visible)

    def _render_metadata(self, metadata) -> None:
        self._meta_id.setText(
            f"<b>Network ID</b><br>{metadata.network_id or '—'}",
        )
        self._meta_name.setText(
            f"<b>Name</b><br>{metadata.name or '—'}",
        )
        self._meta_format.setText(
            f"<b>Format</b><br>{metadata.source_format or '—'}",
        )
        self._meta_case_date.setText(
            f"<b>Case Date</b><br>{metadata.case_date or '—'}",
        )

    def _render_country_totals(self, df: pd.DataFrame) -> None:
        if df.empty:
            self._country_empty_lbl.setVisible(True)
            self._country_lf_caption.setVisible(False)
            self._country_table.setVisible(False)
            self._country_model.set_dataframe(
                pd.DataFrame(columns=COUNTRY_TOTALS_DISPLAY_COLUMNS),
            )
            return
        self._country_empty_lbl.setVisible(False)
        self._country_lf_caption.setVisible(not country_totals_has_lf(df))
        self._country_table.setVisible(True)
        self._country_model.set_dataframe(build_country_totals_display(df))
        self._country_table.resizeColumnsToContents()

    def _render_losses(self, losses: dict, by_country: pd.Series) -> None:
        has_data = bool(losses.get("has_data"))
        if not has_data:
            self._losses_empty_lbl.setVisible(True)
            self._losses_total_lbl.setVisible(False)
            self._losses_lines_lbl.setVisible(False)
            self._losses_xfmr_lbl.setVisible(False)
            self._losses_by_country_caption.setVisible(False)
            self._losses_by_country_table.setVisible(False)
            self._losses_by_country_model.set_dataframe(
                pd.DataFrame(columns=LOSSES_BY_COUNTRY_COLUMNS),
            )
            return
        self._losses_empty_lbl.setVisible(False)
        self._losses_total_lbl.setVisible(True)
        self._losses_lines_lbl.setVisible(True)
        self._losses_xfmr_lbl.setVisible(True)
        self._losses_total_lbl.setText(
            f"<b>Total losses</b><br>{losses['total']:.2f} MW",
        )
        self._losses_lines_lbl.setText(
            f"<b>Line losses</b><br>{losses['lines']:.2f} MW",
        )
        self._losses_xfmr_lbl.setText(
            f"<b>Transformer losses</b><br>{losses['transformers']:.2f} MW",
        )
        if by_country.empty:
            self._losses_by_country_caption.setVisible(False)
            self._losses_by_country_table.setVisible(False)
            self._losses_by_country_model.set_dataframe(
                pd.DataFrame(columns=LOSSES_BY_COUNTRY_COLUMNS),
            )
            return
        self._losses_by_country_caption.setVisible(True)
        self._losses_by_country_table.setVisible(True)
        self._losses_by_country_model.set_dataframe(
            build_losses_by_country_display(by_country),
        )
        self._losses_by_country_table.resizeColumnsToContents()

    def _render_component_counts(self, counts: dict[str, int]) -> None:
        # Drop the previous labels — counts change with each load.
        while self._counts_grid.count():
            item = self._counts_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if not counts:
            self._counts_empty_lbl.setVisible(True)
            self._counts_body.setVisible(False)
            return
        self._counts_empty_lbl.setVisible(False)
        columns = 4
        for i, (label, count) in enumerate(counts.items()):
            metric = _new_metric_label()
            metric.setText(f"<b>{label}</b><br>{count}")
            self._counts_grid.addWidget(metric, i // columns, i % columns)
        # Re-show the body if the user had it expanded.
        self._counts_body.setVisible(self._counts_toggle.isChecked())

    def _on_counts_toggled(self, checked: bool) -> None:
        self._counts_toggle.setArrowType(
            Qt.DownArrow if checked else Qt.RightArrow,
        )
        self._counts_toggle.setText(
            "Hide component counts" if checked else "Show component counts",
        )
        self._counts_body.setVisible(checked)
