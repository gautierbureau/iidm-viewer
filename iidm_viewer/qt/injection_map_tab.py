"""Injection Map tab — PySide6 host.

Composes the shared :mod:`iidm_viewer.injection_map` helpers with
PySide6 widgets:

* controls — metric (P / Q), view mode (icons / gradient) and a
  full-scale ± unit spin pre-seeded with
  :func:`~iidm_viewer.injection_map._suggest_full_scale`,
* a ``QWebEngineView`` that hosts the standalone Leaflet HTML
  returned by
  :func:`~iidm_viewer.injection_map.build_injection_map_html`,
* a caption below the map summarising exporter / importer counts and
  net injection.

All pypowsybl calls hop through the worker thread (per AGENTS.md §1)
via :func:`~iidm_viewer.injection_map._extract_injection_data`. The
data fetch runs once per network; the controls trigger an in-memory
HTML re-build without re-querying pypowsybl.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.injection_map import (
    TRANSPORT_NOMINAL_V_THRESHOLD,
    _METRIC_OPTIONS,
    _VIEW_OPTIONS,
    _extract_injection_data,
    _filter_transport,
    _suggest_full_scale,
    build_injection_map_html,
    injection_map_caption,
    metric_unit,
)
from iidm_viewer.powsybl_worker import NetworkProxy


class InjectionMapTab(QWidget):
    """Tab body. Owns the per-network data + the QWebEngineView."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        # Worker-fetched data: ``{"records": [...], "has_lf_p": bool,
        # "has_lf_q": bool}`` or ``None`` for "no substationPosition".
        self._data: Optional[dict] = None
        # Per-metric full-scale memory so flipping P↔Q restores the
        # value the user last set for the new metric.
        self._scale_by_metric: dict[str, float] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        caption = QLabel(
            "Net active or reactive power per substation. "
            "Green = net exporter (generation > load), "
            "red = net importer (load > generation). "
            "Marker size scales with the absolute net injection.",
        )
        caption.setWordWrap(True)
        caption.setStyleSheet("color: #555; padding: 4px;")
        layout.addWidget(caption)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #666; padding: 4px;")
        self._status_lbl.setWordWrap(True)
        layout.addWidget(self._status_lbl)

        # Controls row.
        self._controls = QWidget()
        controls = QHBoxLayout(self._controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(QLabel("Metric:"))
        self._metric_combo = QComboBox()
        for label, value in _METRIC_OPTIONS.items():
            self._metric_combo.addItem(label, value)
        self._metric_combo.currentIndexChanged.connect(self._on_metric_changed)
        controls.addWidget(self._metric_combo)
        controls.addSpacing(12)
        controls.addWidget(QLabel("View:"))
        self._view_combo = QComboBox()
        for label, value in _VIEW_OPTIONS.items():
            self._view_combo.addItem(label, value)
        self._view_combo.currentIndexChanged.connect(self._on_view_changed)
        controls.addWidget(self._view_combo)
        controls.addSpacing(12)
        self._scale_label = QLabel("Full-scale ± MW:")
        controls.addWidget(self._scale_label)
        self._scale_spin = QDoubleSpinBox()
        self._scale_spin.setDecimals(0)
        self._scale_spin.setSingleStep(50.0)
        self._scale_spin.setRange(1.0, 100000.0)
        self._scale_spin.setValue(500.0)
        self._scale_spin.valueChanged.connect(self._on_scale_changed)
        controls.addWidget(self._scale_spin)
        controls.addStretch(1)
        layout.addWidget(self._controls)

        self._lf_note = QLabel("")
        self._lf_note.setStyleSheet("color: #666;")
        self._lf_note.setWordWrap(True)
        self._lf_note.setVisible(False)
        layout.addWidget(self._lf_note)

        self._map_view = QWebEngineView()
        self._map_view.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding,
        )
        self._map_view.setMinimumHeight(560)
        layout.addWidget(self._map_view, 1)

        self._caption_lbl = QLabel("")
        self._caption_lbl.setStyleSheet("color: #555; padding: 4px;")
        self._caption_lbl.setWordWrap(True)
        layout.addWidget(self._caption_lbl)

        self._set_map_visible(False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        # Wipe the per-metric scale memory whenever the network changes —
        # the network's typical injection magnitudes change with it.
        self._scale_by_metric.clear()
        self.refresh()

    def refresh(self) -> None:
        """Refetch + redraw."""
        if self._network is None:
            self._data = None
            self._status_lbl.setText(
                "Load a network to see the injection map.",
            )
            self._set_map_visible(False)
            return
        try:
            self._data = _extract_injection_data(self._network)
        except Exception as exc:
            self._data = None
            self._status_lbl.setText(f"Injection map failed: {exc}")
            self._set_map_visible(False)
            return
        if self._data is None:
            self._status_lbl.setText(
                "No geographical data available. The network needs a "
                "'substationPosition' extension with latitude/longitude "
                "coordinates."
            )
            self._set_map_visible(False)
            return
        records = _filter_transport(self._data.get("records") or [])
        if not records:
            self._status_lbl.setText(
                f"No substations with a voltage level at or above "
                f"{TRANSPORT_NOMINAL_V_THRESHOLD:g} kV in this network."
            )
            self._set_map_visible(False)
            return
        self._status_lbl.setText("")
        self._set_map_visible(True)
        self._seed_default_scale(records)
        self._render_map()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _current_metric(self) -> str:
        return self._metric_combo.currentData() or "P"

    def _current_mode(self) -> str:
        return self._view_combo.currentData() or "icons"

    def _set_map_visible(self, visible: bool) -> None:
        self._status_lbl.setVisible(not visible)
        self._controls.setVisible(visible)
        self._map_view.setVisible(visible)
        self._caption_lbl.setVisible(visible)
        if not visible:
            self._lf_note.setVisible(False)

    def _seed_default_scale(self, records) -> None:
        """Pre-fill the scale spin from
        :func:`_suggest_full_scale` if the user hasn't touched it yet
        for the active metric."""
        metric = self._current_metric()
        if metric in self._scale_by_metric:
            target = self._scale_by_metric[metric]
        else:
            target = float(_suggest_full_scale(records, metric))
            self._scale_by_metric[metric] = target
        # Update the spin without triggering a re-render mid-refresh.
        self._scale_spin.blockSignals(True)
        self._scale_spin.setValue(target)
        self._scale_spin.blockSignals(False)
        self._scale_label.setText(f"Full-scale ± {metric_unit(metric)}:")

    def _update_lf_note(self) -> None:
        if self._data is None:
            self._lf_note.setVisible(False)
            return
        metric = self._current_metric()
        has_lf = (
            self._data.get("has_lf_p") if metric == "P"
            else self._data.get("has_lf_q")
        )
        if has_lf:
            self._lf_note.setVisible(False)
            return
        fallback = "p0" if metric == "P" else "q0"
        self._lf_note.setText(
            f"No terminal {metric} values populated (no load flow). "
            f"Showing scheduled setpoints (target_{metric.lower()} / "
            f"{fallback})."
        )
        self._lf_note.setVisible(True)

    def _render_map(self) -> None:
        if self._data is None:
            return
        metric = self._current_metric()
        mode = self._current_mode()
        full_scale = float(self._scale_spin.value())
        # Persist the user's pick so flipping metrics restores it.
        self._scale_by_metric[metric] = full_scale
        records = self._data.get("records") or []
        html, transport = build_injection_map_html(
            records,
            metric=metric, mode=mode, full_scale=full_scale,
        )
        self._update_lf_note()
        if not html:
            self._map_view.setHtml("")
            self._caption_lbl.setText(
                f"No substations with a voltage level at or above "
                f"{TRANSPORT_NOMINAL_V_THRESHOLD:g} kV match the filter."
            )
            return
        self._map_view.setHtml(html)
        self._caption_lbl.setText(injection_map_caption(transport, metric))

    def _on_metric_changed(self, *_args) -> None:
        # Flipping P↔Q changes the unit label + restores the per-metric
        # scale memory.
        if self._data is None:
            return
        records = _filter_transport(self._data.get("records") or [])
        self._seed_default_scale(records)
        self._render_map()

    def _on_view_changed(self, *_args) -> None:
        self._render_map()

    def _on_scale_changed(self, *_args) -> None:
        self._render_map()
