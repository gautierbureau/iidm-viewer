"""Voltage Analysis tab — PySide6 host.

Composes the shared :mod:`iidm_viewer.voltage_analysis_core` core with
PySide6 widgets:

* **Bus voltages** — summary ``QTableView`` keyed by nominal voltage,
  plus a drill-down table for one nominal where ``V (pu)`` outside the
  lo/hi band turns red.
* **Geographical voltage map** — Leaflet markers per VL (or fanned,
  or per-substation worst) coloured by per-unit deviation from
  nominal. Hosted in a ``QWebEngineView`` and driven by
  :func:`iidm_viewer.voltage_map.build_voltage_map_html`.
* **Reactive compensation** — three shunt groups (capacitive,
  inductive, unknown) and one SVC group; each renders a metrics row
  plus a sortable detail table.

All pypowsybl calls hop through the worker thread (per AGENTS.md §1)
via :func:`iidm_viewer.voltage_analysis_core.compute_voltage_analysis`
and :func:`iidm_viewer.voltage_map._extract_voltage_map_data`.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.qt.data_explorer_tab import PandasTableModel
from iidm_viewer.voltage_analysis_core import (
    BUS_DETAIL_COLUMNS,
    SHUNT_DISPLAY_COLUMNS,
    SVC_DISPLAY_COLUMNS,
    build_bus_detail,
    build_bus_summary,
    build_shunt_display,
    build_svc_display,
    bus_pu_classify,
    compute_voltage_analysis,
    has_loadflow,
    list_nominal_voltages,
    shunt_totals,
    split_shunts_by_b,
    svc_totals,
)
from iidm_viewer.voltage_map import (
    _LAYOUT_OPTIONS,
    _VIEW_OPTIONS,
    TRANSPORT_NOMINAL_V_THRESHOLD,
    _extract_voltage_map_data,
    build_voltage_map_html,
    nominal_voltage_options,
    voltage_map_caption,
)


_WARNING_BRUSH = QBrush(QColor(255, 75, 75))
_WARNING_FG = QBrush(QColor("white"))


class _BusDetailModel(PandasTableModel):
    """Pandas-backed model that colours out-of-band ``V (pu)`` cells.

    The lo/hi thresholds come from the tab's spin boxes; the model
    classifies via the shared :func:`bus_pu_classify` so PySide6 +
    Streamlit + NiceGUI render identical semantics.
    """

    _pu_col: int = -1
    _lo: float = 0.95
    _hi: float = 1.05

    def set_thresholds(self, lo: float, hi: float) -> None:
        self._lo, self._hi = lo, hi
        if self.rowCount() == 0:
            return
        top = self.index(0, 0)
        bot = self.index(self.rowCount() - 1, self.columnCount() - 1)
        self.dataChanged.emit(
            top, bot, [Qt.BackgroundRole, Qt.ForegroundRole],
        )

    def set_dataframe(self, df, editable_cols=None) -> None:  # type: ignore[override]
        super().set_dataframe(df, editable_cols)
        self._pu_col = (
            df.columns.get_loc("V (pu)") if "V (pu)" in df.columns else -1
        )

    def data(self, index, role=Qt.DisplayRole):  # type: ignore[override]
        if not index.isValid():
            return None
        col = index.column()
        if role in (Qt.BackgroundRole, Qt.ForegroundRole) and col == self._pu_col:
            value = self._df.iat[index.row(), col]
            kind = bus_pu_classify(value, self._lo, self._hi)
            if kind == "warning":
                return _WARNING_BRUSH if role == Qt.BackgroundRole else _WARNING_FG
            return None
        return super().data(index, role)


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


def _metric_label() -> QLabel:
    lbl = QLabel("—")
    lbl.setStyleSheet(
        "padding: 4px 8px; border: 1px solid #ddd; "
        "border-radius: 4px; background: #fafafa;",
    )
    return lbl


class _ShuntGroupWidget(QGroupBox):
    """Metrics row + detail table for one shunt group (cap / ind / unknown)."""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, parent)
        layout = QVBoxLayout(self)

        self._info = QLabel("")
        self._info.setStyleSheet("color: #666;")
        self._info.setVisible(False)
        layout.addWidget(self._info)

        metrics_row = QHBoxLayout()
        self._active_lbl = _metric_label()
        self._available_lbl = _metric_label()
        self._capacity_lbl = _metric_label()
        for lbl in (
            self._active_lbl, self._available_lbl, self._capacity_lbl,
        ):
            metrics_row.addWidget(lbl, 1)
        layout.addLayout(metrics_row)

        self._table = _new_table(max_height=220)
        self._model = PandasTableModel()
        self._table.setModel(self._model)
        layout.addWidget(self._table)

    def set_empty(self, message: str) -> None:
        self._info.setText(message)
        self._info.setVisible(True)
        self._active_lbl.setText("—")
        self._available_lbl.setText("—")
        self._capacity_lbl.setText("—")
        self._model.set_dataframe(pd.DataFrame(columns=SHUNT_DISPLAY_COLUMNS))
        self._table.setVisible(False)

    def set_group(self, group: pd.DataFrame, has_lf: bool) -> None:
        self._info.setVisible(False)
        active, available, capacity = shunt_totals(group)
        label_active = "Active (MVAr)" if has_lf else "Estimated (MVAr)"
        self._active_lbl.setText(f"{label_active}: {active:.2f}")
        self._available_lbl.setText(
            f"Available not activated (MVAr): {available:.2f}",
        )
        self._capacity_lbl.setText(
            f"Total capacity (MVAr): {capacity:.2f}",
        )
        self._model.set_dataframe(build_shunt_display(group))
        self._table.setVisible(True)
        self._table.resizeColumnsToContents()


class VoltageAnalysisTab(QWidget):
    """Tab body. Owns the per-network DataFrames + the bus-detail model."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._buses: pd.DataFrame = pd.DataFrame()
        self._shunts: pd.DataFrame = pd.DataFrame()
        self._svcs: pd.DataFrame = pd.DataFrame()
        # Map data — one worker hop per network; controls drive the
        # HTML re-render in-memory without re-querying pypowsybl.
        self._map_data: Optional[dict] = None

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

        self._placeholder = QLabel(
            "Load a network to see voltage analysis.",
        )
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #666; padding: 8px;")
        layout.addWidget(self._placeholder)

        # ------------------------------------------------------------------
        # Bus voltages
        # ------------------------------------------------------------------
        self._bus_group = QGroupBox("Bus voltages by nominal level")
        bus_layout = QVBoxLayout(self._bus_group)

        self._lf_warning = QLabel(
            "Voltage magnitudes are not available — run a load flow first.",
        )
        self._lf_warning.setStyleSheet(
            "color: #b35a00; padding: 4px; background: #fff7e6; "
            "border: 1px solid #ffd591; border-radius: 4px;",
        )
        self._lf_warning.setVisible(False)
        bus_layout.addWidget(self._lf_warning)

        self._summary_table = _new_table(max_height=220)
        self._summary_model = PandasTableModel()
        self._summary_table.setModel(self._summary_model)
        bus_layout.addWidget(self._summary_table)

        bus_layout.addWidget(QLabel("Bus detail"))

        controls_row = QHBoxLayout()
        controls_row.addWidget(QLabel("Nominal voltage (kV):"))
        self._nom_combo = QComboBox()
        self._nom_combo.setMinimumWidth(120)
        self._nom_combo.currentTextChanged.connect(self._on_nominal_changed)
        controls_row.addWidget(self._nom_combo)
        controls_row.addSpacing(20)
        controls_row.addWidget(QLabel("Low threshold (pu):"))
        self._lo_spin = QDoubleSpinBox()
        self._lo_spin.setDecimals(3)
        self._lo_spin.setSingleStep(0.01)
        self._lo_spin.setRange(0.0, 2.0)
        self._lo_spin.setValue(0.95)
        self._lo_spin.valueChanged.connect(self._on_threshold_changed)
        controls_row.addWidget(self._lo_spin)
        controls_row.addWidget(QLabel("High threshold (pu):"))
        self._hi_spin = QDoubleSpinBox()
        self._hi_spin.setDecimals(3)
        self._hi_spin.setSingleStep(0.01)
        self._hi_spin.setRange(0.0, 2.0)
        self._hi_spin.setValue(1.05)
        self._hi_spin.valueChanged.connect(self._on_threshold_changed)
        controls_row.addWidget(self._hi_spin)
        controls_row.addStretch(1)
        bus_layout.addLayout(controls_row)

        self._detail_caption = QLabel("")
        self._detail_caption.setStyleSheet("color: #555;")
        bus_layout.addWidget(self._detail_caption)

        self._detail_table = _new_table()
        self._detail_model = _BusDetailModel()
        self._detail_table.setModel(self._detail_model)
        bus_layout.addWidget(self._detail_table)

        layout.addWidget(self._bus_group)

        # ------------------------------------------------------------------
        # Geographical voltage map
        # ------------------------------------------------------------------
        self._map_group = QGroupBox("Geographical voltage map")
        map_layout = QVBoxLayout(self._map_group)

        self._map_status_lbl = QLabel("")
        self._map_status_lbl.setStyleSheet("color: #666;")
        self._map_status_lbl.setWordWrap(True)
        map_layout.addWidget(self._map_status_lbl)

        # Controls — same affordances as Streamlit (nominal filter,
        # layout, view mode, full-scale ± pu).
        self._map_controls = QWidget()
        controls = QHBoxLayout(self._map_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(QLabel("Nominal:"))
        self._map_nom_combo = QComboBox()
        self._map_nom_combo.setMinimumWidth(150)
        self._map_nom_combo.currentIndexChanged.connect(self._on_map_changed)
        controls.addWidget(self._map_nom_combo)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Layout:"))
        self._map_layout_combo = QComboBox()
        for label in _LAYOUT_OPTIONS:
            self._map_layout_combo.addItem(label, _LAYOUT_OPTIONS[label])
        self._map_layout_combo.currentIndexChanged.connect(self._on_map_changed)
        controls.addWidget(self._map_layout_combo)
        controls.addSpacing(12)
        controls.addWidget(QLabel("View:"))
        self._map_view_combo = QComboBox()
        for label in _VIEW_OPTIONS:
            self._map_view_combo.addItem(label, _VIEW_OPTIONS[label])
        self._map_view_combo.currentIndexChanged.connect(self._on_map_changed)
        controls.addWidget(self._map_view_combo)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Full-scale ± pu:"))
        self._map_vrange_spin = QDoubleSpinBox()
        self._map_vrange_spin.setDecimals(3)
        self._map_vrange_spin.setSingleStep(0.005)
        self._map_vrange_spin.setRange(0.005, 0.5)
        self._map_vrange_spin.setValue(0.05)
        self._map_vrange_spin.valueChanged.connect(self._on_map_changed)
        controls.addWidget(self._map_vrange_spin)
        controls.addStretch(1)
        map_layout.addWidget(self._map_controls)

        self._map_view = QWebEngineView()
        self._map_view.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding,
        )
        self._map_view.setMinimumHeight(560)
        map_layout.addWidget(self._map_view, 1)

        self._map_caption = QLabel("")
        self._map_caption.setStyleSheet("color: #555; padding: 4px;")
        self._map_caption.setWordWrap(True)
        map_layout.addWidget(self._map_caption)

        layout.addWidget(self._map_group)

        # ------------------------------------------------------------------
        # Reactive compensation
        # ------------------------------------------------------------------
        self._reactive_group = QGroupBox("Reactive compensation")
        reactive_layout = QVBoxLayout(self._reactive_group)

        self._reactive_caption = QLabel(
            "Current Q — Q from the network file when available, otherwise "
            "estimated as −b × V²_nom. Sign convention: Q < 0 for "
            "capacitors, Q > 0 for reactors.",
        )
        self._reactive_caption.setWordWrap(True)
        self._reactive_caption.setStyleSheet(
            "color: #1b5e8b; padding: 6px; background: #eef5fb; "
            "border: 1px solid #b6d7ee; border-radius: 4px;",
        )
        reactive_layout.addWidget(self._reactive_caption)

        # Shunt sub-section.
        self._shunt_section_label = QLabel("Shunt compensators")
        self._shunt_section_label.setStyleSheet("font-weight: bold;")
        reactive_layout.addWidget(self._shunt_section_label)

        self._shunt_lf_note = QLabel(
            "No load flow — injections estimated as b × nominal_v².",
        )
        self._shunt_lf_note.setStyleSheet("color: #666;")
        self._shunt_lf_note.setVisible(False)
        reactive_layout.addWidget(self._shunt_lf_note)

        self._shunt_empty_lbl = QLabel(
            "No shunt compensators in this network.",
        )
        self._shunt_empty_lbl.setStyleSheet("color: #666;")
        self._shunt_empty_lbl.setVisible(False)
        reactive_layout.addWidget(self._shunt_empty_lbl)

        self._cap_group = _ShuntGroupWidget(
            "Capacitive (b > 0, Q < 0) — injects reactive power, raises voltage",
        )
        reactive_layout.addWidget(self._cap_group)
        self._ind_group = _ShuntGroupWidget(
            "Inductive (b < 0, Q > 0) — absorbs reactive power, lowers voltage",
        )
        reactive_layout.addWidget(self._ind_group)
        self._unk_group = _ShuntGroupWidget(
            "Unclassified (b per section unknown — fully disconnected)",
        )
        reactive_layout.addWidget(self._unk_group)

        # SVC sub-section.
        self._svc_section_label = QLabel("Static VAR compensators")
        self._svc_section_label.setStyleSheet("font-weight: bold;")
        reactive_layout.addWidget(self._svc_section_label)

        self._svc_box = QGroupBox("")
        svc_layout = QVBoxLayout(self._svc_box)
        svc_metrics = QHBoxLayout()
        self._svc_active_lbl = _metric_label()
        self._svc_range_lbl = _metric_label()
        svc_metrics.addWidget(self._svc_active_lbl, 1)
        svc_metrics.addWidget(self._svc_range_lbl, 1)
        svc_layout.addLayout(svc_metrics)
        self._svc_table = _new_table(max_height=220)
        self._svc_model = PandasTableModel()
        self._svc_table.setModel(self._svc_model)
        svc_layout.addWidget(self._svc_table)
        reactive_layout.addWidget(self._svc_box)

        self._reactive_empty_lbl = QLabel(
            "No reactive compensation equipment found in this network.",
        )
        self._reactive_empty_lbl.setStyleSheet("color: #666;")
        self._reactive_empty_lbl.setVisible(False)
        reactive_layout.addWidget(self._reactive_empty_lbl)

        layout.addWidget(self._reactive_group)
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
            self._buses = pd.DataFrame()
            self._shunts = pd.DataFrame()
            self._svcs = pd.DataFrame()
            self._map_data = None
            self._placeholder.setText("Load a network to see voltage analysis.")
            self._set_data_visible(False)
            return
        try:
            data = compute_voltage_analysis(self._network)
        except Exception as exc:
            self._buses = pd.DataFrame()
            self._shunts = pd.DataFrame()
            self._svcs = pd.DataFrame()
            self._map_data = None
            self._placeholder.setText(f"Voltage analysis failed: {exc}")
            self._set_data_visible(False)
            return
        self._buses = data.buses
        self._shunts = data.shunts
        self._svcs = data.svcs
        if self._buses.empty:
            self._map_data = None
            self._placeholder.setText(
                "No bus data available in this network.",
            )
            self._set_data_visible(False)
            return
        # Map is best-effort — a failure here shouldn't hide the bus
        # and reactive sections.
        try:
            self._map_data = _extract_voltage_map_data(self._network)
        except Exception:
            self._map_data = None
        self._set_data_visible(True)
        self._refresh_bus_section()
        self._refresh_map_section()
        self._refresh_reactive_section()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _set_data_visible(self, visible: bool) -> None:
        self._placeholder.setVisible(not visible)
        self._bus_group.setVisible(visible)
        self._map_group.setVisible(visible)
        self._reactive_group.setVisible(visible)

    def _refresh_bus_section(self) -> None:
        lf = has_loadflow(self._buses)
        self._lf_warning.setVisible(not lf)
        self._summary_model.set_dataframe(build_bus_summary(self._buses))
        self._summary_table.resizeColumnsToContents()

        # The detail picker + table only make sense once an LF has run
        # (no v_mag means no v_pu means no bands to colour).
        self._nom_combo.setEnabled(lf)
        self._lo_spin.setEnabled(lf)
        self._hi_spin.setEnabled(lf)
        self._detail_table.setVisible(lf)
        self._detail_caption.setVisible(lf)
        if not lf:
            self._detail_model.set_dataframe(
                pd.DataFrame(columns=BUS_DETAIL_COLUMNS),
            )
            return

        nom_options = list_nominal_voltages(self._buses)
        previous = self._nom_combo.currentText()
        self._nom_combo.blockSignals(True)
        self._nom_combo.clear()
        self._nom_combo.addItems([str(v) for v in nom_options])
        try:
            prev_f = float(previous) if previous else None
        except ValueError:
            prev_f = None
        if prev_f is not None and prev_f in nom_options:
            self._nom_combo.setCurrentText(previous)
        elif nom_options:
            self._nom_combo.setCurrentIndex(0)
        self._nom_combo.blockSignals(False)
        self._render_detail()

    def _render_detail(self) -> None:
        text = self._nom_combo.currentText()
        if not text:
            self._detail_model.set_dataframe(
                pd.DataFrame(columns=BUS_DETAIL_COLUMNS),
            )
            self._detail_caption.setText("")
            return
        try:
            nominal = float(text)
        except ValueError:
            return
        lo = float(self._lo_spin.value())
        hi = float(self._hi_spin.value())
        df = build_bus_detail(self._buses, nominal)
        self._detail_model.set_thresholds(lo, hi)
        self._detail_model.set_dataframe(df)
        self._detail_table.resizeColumnsToContents()
        if df.empty:
            outside = 0
        else:
            outside = int(
                df["V (pu)"]
                .apply(lambda v: bus_pu_classify(v, lo, hi) == "warning")
                .sum()
            )
        self._detail_caption.setText(
            f"{len(df)} buses at {nominal} kV — "
            f"{outside} outside [{lo:.3f}, {hi:.3f}] pu"
        )

    def _on_nominal_changed(self, _text: str) -> None:
        self._render_detail()

    def _on_threshold_changed(self, _value: float) -> None:
        self._render_detail()

    # ------------------------------------------------------------------
    # Geographical voltage map
    # ------------------------------------------------------------------
    def _refresh_map_section(self) -> None:
        """Rebuild the nominal-voltage picker + render the map for the
        current control values. Called once per network — the
        :meth:`_on_map_changed` slot drives subsequent re-renders."""
        if self._map_data is None:
            self._map_status_lbl.setText(
                "No geographical data available. The network needs a "
                "'substationPosition' extension with latitude/longitude "
                "coordinates."
            )
            self._map_status_lbl.setVisible(True)
            self._map_controls.setVisible(False)
            self._map_view.setVisible(False)
            self._map_caption.setText("")
            return

        records = self._map_data.get("records") or []
        has_lf = bool(self._map_data.get("has_lf"))
        transport = [
            r for r in records
            if r["nominal_v"] >= TRANSPORT_NOMINAL_V_THRESHOLD
        ]
        if not transport:
            self._map_status_lbl.setText(
                f"No voltage levels at or above {TRANSPORT_NOMINAL_V_THRESHOLD:g} kV "
                "with geographical coordinates in this network."
            )
            self._map_status_lbl.setVisible(True)
            self._map_controls.setVisible(False)
            self._map_view.setVisible(False)
            self._map_caption.setText("")
            return
        if not has_lf:
            self._map_status_lbl.setText(
                "Voltage magnitudes are not available on the map — "
                "run a load flow first."
            )
            self._map_status_lbl.setVisible(True)
            self._map_controls.setVisible(False)
            self._map_view.setVisible(False)
            self._map_caption.setText("")
            return

        self._map_status_lbl.setVisible(False)
        self._map_controls.setVisible(True)
        self._map_view.setVisible(True)

        # Rebuild the nominal picker; preserve the selection across
        # refreshes (a fresh LF leaves the network shape unchanged).
        previous = self._map_nom_combo.currentData()
        self._map_nom_combo.blockSignals(True)
        self._map_nom_combo.clear()
        self._map_nom_combo.addItem("All nominal voltages", None)
        noms = nominal_voltage_options(transport)
        counts: dict[float, int] = {}
        for r in transport:
            counts[round(r["nominal_v"], 3)] = counts.get(
                round(r["nominal_v"], 3), 0,
            ) + 1
        for nv in noms:
            self._map_nom_combo.addItem(
                f"{nv:g} kV ({counts.get(nv, 0)} VL)", float(nv),
            )
        if previous is not None:
            idx = self._map_nom_combo.findData(previous)
            if idx >= 0:
                self._map_nom_combo.setCurrentIndex(idx)
        self._map_nom_combo.blockSignals(False)

        self._render_map()

    def _render_map(self) -> None:
        if self._map_data is None:
            return
        records = self._map_data.get("records") or []
        sel_nom = self._map_nom_combo.currentData()
        layout_value = self._map_layout_combo.currentData() or "per_vl"
        mode_value = self._map_view_combo.currentData() or "icons"
        v_range = float(self._map_vrange_spin.value())
        html, display = build_voltage_map_html(
            records,
            sel_nom=sel_nom,
            layout=layout_value,
            mode=mode_value,
            v_range=v_range,
        )
        if not html:
            self._map_view.setHtml("")
            self._map_caption.setText("No voltage levels match the current filter.")
            return
        self._map_view.setHtml(html)
        self._map_caption.setText(
            voltage_map_caption(display, sel_nom=sel_nom, layout=layout_value),
        )

    def _on_map_changed(self, *_args) -> None:
        self._render_map()

    def _refresh_reactive_section(self) -> None:
        has_shunts = not self._shunts.empty
        has_svcs = not self._svcs.empty

        if not has_shunts and not has_svcs:
            self._reactive_empty_lbl.setVisible(True)
            self._reactive_caption.setVisible(False)
            self._shunt_section_label.setVisible(False)
            self._shunt_lf_note.setVisible(False)
            self._shunt_empty_lbl.setVisible(False)
            self._cap_group.setVisible(False)
            self._ind_group.setVisible(False)
            self._unk_group.setVisible(False)
            self._svc_section_label.setVisible(False)
            self._svc_box.setVisible(False)
            return

        self._reactive_empty_lbl.setVisible(False)
        self._reactive_caption.setVisible(True)

        # Shunts
        self._shunt_section_label.setVisible(True)
        if has_shunts:
            self._shunt_empty_lbl.setVisible(False)
            has_lf = bool(self._shunts["q"].notna().any())
            self._shunt_lf_note.setVisible(not has_lf)
            cap, ind, unk = split_shunts_by_b(self._shunts)
            self._cap_group.setVisible(True)
            if cap.empty:
                self._cap_group.set_empty(
                    "No capacitive shunt compensators in this network.",
                )
            else:
                self._cap_group.set_group(cap, has_lf)
            self._ind_group.setVisible(True)
            if ind.empty:
                self._ind_group.set_empty(
                    "No inductive shunt compensators in this network.",
                )
            else:
                self._ind_group.set_group(ind, has_lf)
            if unk.empty:
                self._unk_group.setVisible(False)
            else:
                self._unk_group.setVisible(True)
                self._unk_group.set_group(unk, has_lf)
        else:
            self._shunt_lf_note.setVisible(False)
            self._shunt_empty_lbl.setVisible(True)
            self._cap_group.setVisible(False)
            self._ind_group.setVisible(False)
            self._unk_group.setVisible(False)

        # SVCs
        self._svc_section_label.setVisible(True)
        if has_svcs:
            self._svc_box.setVisible(True)
            has_lf = bool(self._svcs["current_q_mvar"].notna().any())
            active, total_range = svc_totals(self._svcs)
            if has_lf:
                self._svc_active_lbl.setText(
                    f"Active injection (MVAr): {active:.2f}",
                )
            else:
                self._svc_active_lbl.setText(
                    "Active injection (MVAr): — (run a load flow first)",
                )
            self._svc_range_lbl.setText(
                f"Total controllable range (MVAr): {total_range:.2f}",
            )
            self._svc_model.set_dataframe(build_svc_display(self._svcs))
            self._svc_table.resizeColumnsToContents()
        else:
            self._svc_box.setVisible(True)
            self._svc_active_lbl.setText(
                "No static VAR compensators in this network.",
            )
            self._svc_range_lbl.setText("")
            self._svc_model.set_dataframe(
                pd.DataFrame(columns=SVC_DISPLAY_COLUMNS),
            )
