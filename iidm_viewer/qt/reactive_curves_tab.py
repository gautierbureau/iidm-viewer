"""Reactive Capability Curves tab — PySide6 host.

Composes the shared :class:`~iidm_viewer.reactive_curves.ReactiveCurvesViewModel`
with PySide6 widgets:

* a generator picker (``QComboBox``) + optional VL narrow checkbox,
* a five-cell metrics row (``target_p`` / ``target_q`` / ``min_q`` /
  ``max_q`` / regulation type) plus a sensitivity caption,
* a Plotly chart rendered into a ``QWebEngineView`` (the same widget
  that hosts the SLD / NAD bundles),
* a "Target P/Q containment" panel with the four subset frames in
  ``QTableView``s.

All pypowsybl calls hop through the worker thread (per AGENTS.md §1)
via :func:`iidm_viewer.reactive_curves.build_reactive_curves_view_model`.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

import pandas as pd
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
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

from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.qt.data_explorer_tab import PandasTableModel
from iidm_viewer.variants import INITIAL_VARIANT_ID, NK_VARIANT_ID
from iidm_viewer.reactive_curves import (
    STATUS_DIAMOND_COLOR,
    ReactiveCurvesViewModel,
    build_containment_summary,
    build_generator_plot_data,
    build_reactive_curves_view_model,
    compute_target_v_q_sensitivity,
)


class ReactiveCurvesTab(QWidget):
    """Tab body. Owns the view model + selection state."""

    # Emitted when the user picks a different VL via the "Only generators
    # in VL X" path. The MainWindow doesn't currently consume this — the
    # checkbox just narrows in place — but the signal keeps the tab
    # symmetrical with the others.
    vl_filter_toggled = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._selected_vl: Optional[str] = None
        self._view_model: Optional[ReactiveCurvesViewModel] = None
        self._gen_id: Optional[str] = None
        self._plot_tmp: Optional[str] = None  # temp file for Plotly HTML
        self._variant_id = INITIAL_VARIANT_ID
        self._view_mode = "N"
        self._state = None  # set via set_state

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # View-mode combo — disabled until an N-K variant exists.
        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("View:"))
        self._view_mode_combo = QComboBox()
        # 'Side-by-side' adds a comparison strip showing the
        # containment counts (Inside / Edge / Outside / Unknown) for
        # the N and N-K variants. The per-gen plot above stays
        # single-pane and tracks ``self._variant_id``.
        self._view_mode_combo.addItems(["N", "N-K", "Side-by-side"])
        self._view_mode_combo.setEnabled(False)
        self._view_mode_combo.currentTextChanged.connect(
            self._on_view_mode_changed,
        )
        view_row.addWidget(self._view_mode_combo)
        view_row.addStretch(1)
        layout.addLayout(view_row)

        # Top row: VL narrow checkbox + generator picker.
        top_row = QHBoxLayout()
        self._only_vl_checkbox = QCheckBox("Only generators in selected VL")
        self._only_vl_checkbox.setVisible(False)
        self._only_vl_checkbox.stateChanged.connect(self._on_only_vl_toggled)
        top_row.addWidget(self._only_vl_checkbox)
        top_row.addSpacing(20)
        top_row.addWidget(QLabel("Generator:"))
        self._gen_combo = QComboBox()
        self._gen_combo.setMinimumWidth(220)
        self._gen_combo.currentTextChanged.connect(self._on_gen_changed)
        top_row.addWidget(self._gen_combo)
        self._gen_count_lbl = QLabel("")
        self._gen_count_lbl.setStyleSheet("color: #666;")
        top_row.addWidget(self._gen_count_lbl)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        # Metric row.
        metrics = QHBoxLayout()
        self._target_p_lbl = QLabel("target_p: —")
        self._target_q_lbl = QLabel("target_q: —")
        self._min_q_lbl = QLabel("min_q @ tp: —")
        self._max_q_lbl = QLabel("max_q @ tp: —")
        self._type_lbl = QLabel("Type: —")
        for lbl in (
            self._target_p_lbl, self._target_q_lbl,
            self._min_q_lbl, self._max_q_lbl, self._type_lbl,
        ):
            lbl.setStyleSheet(
                "padding: 4px 8px; border: 1px solid #ddd; "
                "border-radius: 4px; background: #fafafa;"
            )
            metrics.addWidget(lbl, 1)
        layout.addLayout(metrics)
        self._sensitivity_lbl = QLabel("")
        self._sensitivity_lbl.setStyleSheet("color: #555; padding: 2px 4px;")
        self._sensitivity_lbl.setWordWrap(True)
        self._sensitivity_lbl.setVisible(False)
        layout.addWidget(self._sensitivity_lbl)

        # Plot view (Plotly HTML inside QWebEngineView).
        self._plot_view = QWebEngineView()
        self._plot_view.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding,
        )
        self._plot_view.setMinimumHeight(420)
        layout.addWidget(self._plot_view, 1)
        self._plot_caption = QLabel("")
        self._plot_caption.setStyleSheet("color: #555; padding: 2px 4px;")
        layout.addWidget(self._plot_caption)

        # Placeholder shown when there's no data.
        self._placeholder = QLabel("Load a network to see capability curves.")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #666; padding: 8px;")
        layout.addWidget(self._placeholder)

        # Containment summary group.
        self._summary_group = QGroupBox("Target P/Q containment")
        self._summary_group.setCheckable(True)
        self._summary_group.setChecked(False)
        self._summary_group.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred,
        )
        summary_layout = QVBoxLayout(self._summary_group)
        self._summary_metrics_lbl = QLabel("")
        self._summary_metrics_lbl.setStyleSheet("padding: 2px 4px;")
        summary_layout.addWidget(self._summary_metrics_lbl)
        self._summary_caption = QLabel("")
        self._summary_caption.setStyleSheet("color: #555; padding: 2px 4px;")
        self._summary_caption.setWordWrap(True)
        self._summary_caption.setVisible(False)
        summary_layout.addWidget(self._summary_caption)
        # Four subset tables, hidden when empty.
        self._subset_views: dict = {}
        for key, label in (
            ("pq_outside", "PQ outside (target_q infeasible)"),
            ("pv_saturated", "PV saturated (LF clamped Q → switched to PQ)"),
            ("pq_edge", "PQ on edge"),
            ("pv_near_saturation", "PV near saturation"),
        ):
            header = QLabel(label)
            header.setStyleSheet("font-weight: bold; padding: 4px 2px;")
            header.setVisible(False)
            table = QTableView()
            table.setMaximumHeight(180)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
            table.setVisible(False)
            summary_layout.addWidget(header)
            summary_layout.addWidget(table)
            self._subset_views[key] = (header, table)
        layout.addWidget(self._summary_group)

        # Side-by-side comparison group — hidden until 'Side-by-side'
        # is picked on the view-mode combo. Two columns: containment
        # bucket counts for N and N-K so the user can spot which gens
        # flipped state under the contingency.
        self._sxs_group = QGroupBox("Containment summary — N vs N-K")
        sxs_layout = QHBoxLayout(self._sxs_group)
        self._sxs_n_lbl = QLabel("N (base):\n—")
        self._sxs_n_lbl.setStyleSheet(
            "padding: 6px 12px; border: 1px solid #ddd; "
            "border-radius: 4px; background: #fafafa;",
        )
        self._sxs_n_lbl.setWordWrap(True)
        self._sxs_nk_lbl = QLabel("N-K (contingency):\n—")
        self._sxs_nk_lbl.setStyleSheet(
            "padding: 6px 12px; border: 1px solid #ddd; "
            "border-radius: 4px; background: #fafafa;",
        )
        self._sxs_nk_lbl.setWordWrap(True)
        sxs_layout.addWidget(self._sxs_n_lbl, 1)
        sxs_layout.addWidget(self._sxs_nk_lbl, 1)
        self._sxs_group.setVisible(False)
        layout.addWidget(self._sxs_group)

    # ------------------------------------------------------------------
    # Public API (mirrors the other Qt tabs).
    # ------------------------------------------------------------------
    def set_state(self, state) -> None:
        """Wire the tab to the host's :class:`AppState` so the view-mode
        combo can enable / disable in response to N-K variant lifecycle
        events."""
        self._state = state
        state.nk_variant_changed.connect(self._on_nk_variant_changed)
        state.nk_loadflow_completed.connect(lambda _r: self.refresh())
        self._on_nk_variant_changed(state.nk_variant_id)

    def _on_nk_variant_changed(self, variant_id) -> None:
        active = variant_id == NK_VARIANT_ID
        self._view_mode_combo.setEnabled(active)
        if not active:
            self._view_mode_combo.blockSignals(True)
            self._view_mode_combo.setCurrentText("N")
            self._view_mode_combo.blockSignals(False)
            self._view_mode = "N"
            self._sxs_group.setVisible(False)
            if self._variant_id != INITIAL_VARIANT_ID:
                self._variant_id = INITIAL_VARIANT_ID
                self.refresh()

    def _on_view_mode_changed(self, txt: str) -> None:
        if txt == self._view_mode:
            return
        self._view_mode = txt
        if txt == "Side-by-side":
            self._variant_id = INITIAL_VARIANT_ID
            self._sxs_group.setVisible(True)
        elif txt == "N-K":
            self._variant_id = NK_VARIANT_ID
            self._sxs_group.setVisible(False)
        else:
            self._variant_id = INITIAL_VARIANT_ID
            self._sxs_group.setVisible(False)
        self.refresh()

    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._view_model = None
        self._gen_id = None
        self.refresh()

    def set_selected_vl(self, vl_id: Optional[str]) -> None:
        self._selected_vl = vl_id or None
        if vl_id:
            self._only_vl_checkbox.setText(f"Only generators in VL {vl_id}")
            self._only_vl_checkbox.setVisible(True)
        else:
            self._only_vl_checkbox.setVisible(False)
            self._only_vl_checkbox.setChecked(False)
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the view model + redraw everything."""
        if self._network is None:
            self._view_model = None
            self._set_visible_data(False)
            self._placeholder.setText("Load a network to see capability curves.")
            self._placeholder.setVisible(True)
            self._render_summary(None)
            return
        only_vl = (
            self._selected_vl
            if (self._only_vl_checkbox.isChecked() and self._selected_vl)
            else None
        )
        try:
            vm = build_reactive_curves_view_model(
                self._network, only_vl=only_vl,
                variant_id=self._variant_id,
            )
        except Exception as exc:
            self._view_model = None
            self._placeholder.setText(f"Reactive curves failed: {exc}")
            self._placeholder.setVisible(True)
            self._set_visible_data(False)
            self._render_summary(None)
            return
        if vm is None or vm.gens_df.empty:
            self._view_model = None
            self._placeholder.setText(
                "No generators with reactive limits in this network."
            )
            self._placeholder.setVisible(True)
            self._set_visible_data(False)
            self._render_summary(None)
            return
        self._view_model = vm
        self._placeholder.setVisible(False)
        self._set_visible_data(True)
        gen_ids = list(vm.gens_df.index)
        self._gen_count_lbl.setText(
            f"{len(gen_ids)} generator(s) with reactive limits"
        )
        # Refresh combo without re-firing the change signal.
        self._gen_combo.blockSignals(True)
        try:
            self._gen_combo.clear()
            self._gen_combo.addItems([str(g) for g in gen_ids])
            current = self._gen_id if self._gen_id in gen_ids else gen_ids[0]
            self._gen_id = current
            idx = gen_ids.index(current)
            self._gen_combo.setCurrentIndex(idx)
        finally:
            self._gen_combo.blockSignals(False)
        self._render_selected_gen()
        self._render_summary(vm)
        if self._view_mode == "Side-by-side":
            self._render_sxs_summary()

    def _render_sxs_summary(self) -> None:
        """Populate the N vs N-K containment-counts strip. Reuses the
        per-(net_key, lf_gen[variant_id], variant_id) classification
        cache so subsequent renders are cheap."""
        if self._network is None:
            return
        try:
            n_vm = build_reactive_curves_view_model(
                self._network, variant_id=INITIAL_VARIANT_ID,
            )
            nk_vm = build_reactive_curves_view_model(
                self._network, variant_id=NK_VARIANT_ID,
            )
        except Exception:
            return
        if n_vm is None or nk_vm is None:
            self._sxs_n_lbl.setText(
                "N (base):\nBuild + run an N-K LF to populate."
            )
            self._sxs_nk_lbl.setText(
                "N-K (contingency):\nBuild + run an N-K LF to populate."
            )
            return
        n_sum = build_containment_summary(n_vm.classified, n_vm.gens_df)
        nk_sum = build_containment_summary(nk_vm.classified, nk_vm.gens_df)

        def _fmt(label: str, s) -> str:
            saturated = (
                f" (PV→PQ: {s.n_saturated})" if s.n_saturated else ""
            )
            return (
                f"<b>{label}</b><br>"
                f"Inside: {s.n_inside}<br>"
                f"Edge/Near: {s.n_warning}<br>"
                f"Outside/Saturated: {s.n_action}{saturated}<br>"
                f"Unknown/Needs LF: {s.n_unknown}"
            )

        self._sxs_n_lbl.setText(_fmt("N (base)", n_sum))
        self._sxs_nk_lbl.setText(_fmt("N-K (contingency)", nk_sum))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _set_visible_data(self, visible: bool) -> None:
        for w in (
            self._target_p_lbl, self._target_q_lbl,
            self._min_q_lbl, self._max_q_lbl, self._type_lbl,
            self._plot_view, self._plot_caption,
            self._summary_group,
        ):
            w.setVisible(visible)

    def _on_only_vl_toggled(self, _state: int) -> None:
        self.vl_filter_toggled.emit(self._only_vl_checkbox.isChecked())
        self.refresh()

    def _on_gen_changed(self, gen_id: str) -> None:
        if not gen_id:
            return
        self._gen_id = gen_id
        self._render_selected_gen()

    def _render_selected_gen(self) -> None:
        vm = self._view_model
        gen_id = self._gen_id
        if vm is None or gen_id is None or gen_id not in vm.gens_df.index:
            for lbl, prefix in (
                (self._target_p_lbl, "target_p"),
                (self._target_q_lbl, "target_q"),
                (self._min_q_lbl, "min_q @ tp"),
                (self._max_q_lbl, "max_q @ tp"),
                (self._type_lbl, "Type"),
            ):
                lbl.setText(f"{prefix}: —")
            self._sensitivity_lbl.setVisible(False)
            self._plot_view.setHtml("")
            self._cleanup_plot_tmp()
            self._plot_caption.setText("")
            return
        gen_row = vm.gens_df.loc[gen_id]
        classified_row = (
            vm.classified.loc[gen_id]
            if gen_id in vm.classified.index
            else pd.Series(dtype="object")
        )
        self._target_p_lbl.setText(
            f"target_p: {gen_row.get('target_p', float('nan')):.1f} MW"
        )
        self._target_q_lbl.setText(
            f"target_q: {gen_row.get('target_q', float('nan')):.1f} MVar"
        )
        self._min_q_lbl.setText(
            f"min_q @ tp: {gen_row.get('min_q_at_target_p', float('nan')):.1f} MVar"
        )
        self._max_q_lbl.setText(
            f"max_q @ tp: {gen_row.get('max_q_at_target_p', float('nan')):.1f} MVar"
        )
        self._type_lbl.setText(f"Type: {classified_row.get('regulation', '?')}")

        # Sensitivity caption — only for voltage-regulating gens.
        self._sensitivity_lbl.setVisible(False)
        if bool(gen_row.get("voltage_regulator_on", False)):
            try:
                sens = compute_target_v_q_sensitivity(self._network, gen_id)
            except Exception:
                sens = None
            if sens is not None:
                dq_dv, q_ref = sens
                self._sensitivity_lbl.setText(
                    f"dQ_bus / dV_target ≈ {dq_dv:+.2f} MVar/kV "
                    f"(BUS_REACTIVE_POWER ref = {q_ref:.2f} MVar)."
                )
                self._sensitivity_lbl.setVisible(True)

        self._render_plot(vm, gen_id)

    def _render_plot(self, vm: ReactiveCurvesViewModel, gen_id: str) -> None:
        plot_data = build_generator_plot_data(
            gen_id, vm.gens_df, vm.curves_df, vm.classified, vm.curve_gen_ids,
        )
        if plot_data is None:
            self._plot_view.setHtml("")
            self._cleanup_plot_tmp()
            self._plot_caption.setText("")
            return
        import plotly.graph_objects as go

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=plot_data.polygon_p, y=plot_data.polygon_q,
            fill="toself",
            fillcolor="rgba(99, 110, 250, 0.15)",
            line=dict(color="rgb(99, 110, 250)"),
            name=plot_data.curve_label,
        ))
        if plot_data.operating_point is not None:
            op_p, op_q = plot_data.operating_point
            fig.add_trace(go.Scatter(
                x=[op_p], y=[op_q],
                mode="markers",
                marker=dict(size=12, color="red", symbol="x"),
                name=f"Operating (P={op_p:.1f}, Q={op_q:.1f})",
            ))
        if plot_data.target_point is not None:
            tp, tq, status, regulation = plot_data.target_point
            fig.add_trace(go.Scatter(
                x=[tp], y=[tq],
                mode="markers",
                marker=dict(
                    size=12,
                    color=STATUS_DIAMOND_COLOR.get(status, "green"),
                    symbol="diamond",
                ),
                name=(
                    f"Target [{regulation}] (P={tp:.1f}, Q={tq:.1f}, {status})"
                ),
            ))
        fig.update_layout(
            xaxis_title="P (MW)",
            yaxis_title="Q (MVar)",
            title=f"Reactive Capability Curve — {gen_id}",
            showlegend=True,
            margin=dict(l=40, r=20, t=40, b=40),
        )
        # ``include_plotlyjs="inline"`` embeds the full plotly.js library
        # so the QWebEngineView renders offline (no internet, no CDN).
        # The resulting HTML is ~4–5 MB, which exceeds QWebEngineView's
        # ~2 MB limit for setHtml(). Write to a temp file and load via
        # setUrl() instead — the browser reads from disk with no size cap.
        html = fig.to_html(include_plotlyjs="inline", full_html=True)
        self._load_plot_html(html)
        if plot_data.has_curve and plot_data.curve_points is not None:
            self._plot_caption.setText(
                f"{len(plot_data.curve_points)} curve points for {gen_id}"
            )
        else:
            self._plot_caption.setText(
                f"Min-max reactive limits for {gen_id}"
            )

    def _load_plot_html(self, html: str) -> None:
        """Write *html* to a temp file and point the QWebEngineView at it.

        ``QWebEngineView.setHtml()`` silently truncates content beyond
        ~2 MB (the IPC limit after percent-encoding).  Plotly's inline JS
        alone is ~4.5 MB, so the chart would never render.  Writing to a
        temp file and loading via ``setUrl()`` bypasses the limit entirely.
        """
        self._cleanup_plot_tmp()
        fd, path = tempfile.mkstemp(suffix=".html", prefix="iidm_rcc_")
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

    def _render_summary(self, vm: Optional[ReactiveCurvesViewModel]) -> None:
        if vm is None:
            self._summary_metrics_lbl.setText("")
            self._summary_caption.setVisible(False)
            for header, table in self._subset_views.values():
                header.setVisible(False)
                table.setVisible(False)
            return
        summary = build_containment_summary(vm.classified, vm.gens_df)
        line = (
            f"Inside: {summary.n_inside}   "
            f"·   Edge/Near: {summary.n_warning}   "
            f"·   Outside/Saturated: {summary.n_action}"
        )
        if summary.n_saturated:
            line += f"  (PV → PQ: {summary.n_saturated})"
        line += f"   ·   Unknown/Needs LF: {summary.n_unknown}"
        self._summary_metrics_lbl.setText(line)
        if summary.n_needs_lf:
            self._summary_caption.setText(
                f"{summary.n_needs_lf} PV generator(s) need a load flow to "
                "evaluate their operating point against the diagram."
            )
            self._summary_caption.setVisible(True)
        else:
            self._summary_caption.setVisible(False)
        for key in ("pq_outside", "pv_saturated", "pq_edge", "pv_near_saturation"):
            header, table = self._subset_views[key]
            df = getattr(summary, key)
            if df.empty:
                header.setVisible(False)
                table.setVisible(False)
                table.setModel(None)
                continue
            header.setVisible(True)
            table.setVisible(True)
            display_df = df.reset_index()
            table.setModel(PandasTableModel(display_df))
            table.resizeColumnsToContents()
