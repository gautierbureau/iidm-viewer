"""Top-level window for the PySide6 prototype.

Sidebar (load button + selected VL) on the left, ``QTabWidget`` with the
Network Map and Single Line Diagram tabs on the right. Wires the
killer interaction: clicking a substation on the map sets the selected
voltage level and switches to the SLD tab.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.network_loader import (
    export_network,
    filter_voltage_levels,
    get_export_formats,
    guess_mime_for_export,
    list_voltage_levels_for_selector,
)
from iidm_viewer.qt.data_explorer_tab import DataExplorerTab
from iidm_viewer.qt.extensions_explorer_tab import ExtensionsExplorerTab
from iidm_viewer.qt.injection_map_tab import InjectionMapTab
from iidm_viewer.qt.map_tab import MapTab
from iidm_viewer.qt.nad_tab import NadTab
from iidm_viewer.qt.nk_variant_dock import NkVariantDock
from iidm_viewer.qt.operational_limits_tab import OperationalLimitsTab
from iidm_viewer.qt.overview_tab import OverviewTab
from iidm_viewer.qt.pmax_visualization_tab import PmaxVisualizationTab
from iidm_viewer.qt.reactive_curves_tab import ReactiveCurvesTab
from iidm_viewer.qt.security_analysis_tab import SecurityAnalysisTab
from iidm_viewer.qt.short_circuit_analysis_tab import ShortCircuitAnalysisTab
from iidm_viewer.qt.sld_tab import SldTab
from iidm_viewer.qt.voltage_analysis_tab import VoltageAnalysisTab
from iidm_viewer.qt.state import AppState


class _Sidebar(QWidget):
    def __init__(
        self, on_load, on_run_loadflow, on_vl_selected, on_view_logs,
        on_lf_parameters, on_save_network, on_import_options,
        on_network_reduction, on_blank_network, on_view_session_script,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setFixedWidth(240)
        self.setStyleSheet("background: #f6f6f6;")

        title = QLabel("IIDM Viewer\n(PySide6 preview)")
        title.setStyleSheet("font-weight: bold; padding: 12px 10px;")

        self._load_btn = QPushButton("Load network…")
        self._load_btn.clicked.connect(on_load)

        # "Start with empty network" prompts for a network id and
        # installs a freshly-created blank pypowsybl Network. Lets
        # users build a model from scratch via the Data Explorer's
        # "Create a new …" forms — mirrors Streamlit's blank-network
        # dialog.
        self._blank_btn = QPushButton("Start with empty network")
        self._blank_btn.clicked.connect(on_blank_network)

        # "Import options…" opens the LoadOptionsDialog — sets the
        # format / params / post-processors used on the next file
        # load. Always enabled (the dialog itself is fine before any
        # network is loaded).
        self._import_opts_btn = QPushButton("Import options…")
        self._import_opts_btn.clicked.connect(on_import_options)

        # "Save network" exports the current network through pypowsybl
        # and writes the result to a path the user picks. Disabled
        # until a network is loaded — mirrors Streamlit's sidebar gate.
        self._save_btn = QPushButton("Save network")
        self._save_btn.clicked.connect(on_save_network)
        self._save_btn.setEnabled(False)

        # "Network Reduction" opens the three-mode reduction modal.
        # Irreversible — only enabled once a network is loaded.
        self._reduction_btn = QPushButton("Network Reduction")
        self._reduction_btn.clicked.connect(on_network_reduction)
        self._reduction_btn.setEnabled(False)

        self._file_lbl = QLabel("No file loaded.")
        self._file_lbl.setWordWrap(True)
        self._file_lbl.setStyleSheet("padding: 8px 10px; color: #555; font-size: 11px;")

        # VL filter + dropdown (mirrors Streamlit's vl_selector).
        # The full VL DataFrame is kept around so the filter input can
        # re-narrow the combo without re-fetching from pypowsybl.
        self._on_vl_selected = on_vl_selected
        self._vl_df = None  # populated by ``set_voltage_levels``
        vl_filter_lbl = QLabel("Voltage Level")
        vl_filter_lbl.setStyleSheet("padding: 8px 10px 0 10px; font-size: 11px; color: #555;")
        self._vl_filter = QLineEdit()
        self._vl_filter.setPlaceholderText("Filter voltage levels")
        self._vl_filter.textChanged.connect(self._on_vl_filter_changed)
        self._vl_combo = QComboBox()
        self._vl_combo.setEnabled(False)
        self._vl_combo.currentIndexChanged.connect(self._on_vl_combo_changed)

        # "Run AC Load Flow" + a gear button that opens the
        # LFParametersDialog, mirroring Streamlit's sidebar pair.
        self._run_lf_btn = QPushButton("Run AC Load Flow")
        self._run_lf_btn.clicked.connect(on_run_loadflow)
        self._run_lf_btn.setEnabled(False)
        self._lf_params_btn = QPushButton("⚙")
        self._lf_params_btn.setToolTip("Load Flow Parameters")
        self._lf_params_btn.setFixedWidth(32)
        self._lf_params_btn.clicked.connect(on_lf_parameters)
        lf_row = QHBoxLayout()
        lf_row.setSpacing(4)
        lf_row.addWidget(self._run_lf_btn, 1)
        lf_row.addWidget(self._lf_params_btn)
        self._lf_status = QLabel("")
        self._lf_status.setWordWrap(True)
        self._lf_status.setStyleSheet("padding: 4px 10px; font-size: 11px;")
        # "View Logs" opens the LFReportDialog with the cached report
        # from the most recent run. Disabled until a LF has produced a
        # report_json — mirrors Streamlit's gated button.
        self._view_logs_btn = QPushButton("View Logs")
        self._view_logs_btn.clicked.connect(on_view_logs)
        self._view_logs_btn.setEnabled(False)

        # "View live Script" opens the SessionScriptDialog. Always
        # enabled — even before a network is loaded the user can see
        # the empty log + the Recording toggle. Mirrors Streamlit's
        # session_script.show_session_script_dialog.
        self._view_script_btn = QPushButton("View live Script")
        self._view_script_btn.setToolTip(
            "Open the auto-recorded HMI-mirror script for this session."
        )
        self._view_script_btn.clicked.connect(on_view_session_script)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(title)
        layout.addWidget(self._load_btn)
        layout.addWidget(self._blank_btn)
        layout.addWidget(self._import_opts_btn)
        layout.addWidget(self._save_btn)
        layout.addWidget(self._reduction_btn)
        layout.addWidget(self._file_lbl)
        layout.addWidget(vl_filter_lbl)
        layout.addWidget(self._vl_filter)
        layout.addWidget(self._vl_combo)
        layout.addLayout(lf_row)
        layout.addWidget(self._lf_status)
        layout.addWidget(self._view_logs_btn)
        layout.addWidget(self._view_script_btn)
        layout.addStretch(1)

    # -- File / status labels --------------------------------------------
    def set_file(self, path: Optional[str]) -> None:
        self._file_lbl.setText(os.path.basename(path) if path else "No file loaded.")

    def set_loadflow_enabled(self, enabled: bool) -> None:
        self._run_lf_btn.setEnabled(enabled)

    def set_view_logs_enabled(self, enabled: bool) -> None:
        """Enable the "View Logs" button once a LF report is cached."""
        self._view_logs_btn.setEnabled(enabled)

    def set_save_enabled(self, enabled: bool) -> None:
        """Enable the "Save network" button once a network is loaded."""
        self._save_btn.setEnabled(enabled)

    def set_reduction_enabled(self, enabled: bool) -> None:
        """Enable the "Network Reduction" button once a network is loaded."""
        self._reduction_btn.setEnabled(enabled)

    def set_loadflow_status(self, text: str, ok: bool = True) -> None:
        if not text:
            self._lf_status.setText("")
            self._lf_status.setStyleSheet("padding: 4px 10px; font-size: 11px;")
            return
        color = "#0a7e2a" if ok else "#b30000"
        self._lf_status.setText(text)
        self._lf_status.setStyleSheet(
            f"padding: 4px 10px; font-size: 11px; color: {color};"
        )

    # -- VL picker -------------------------------------------------------
    def set_voltage_levels(self, vls_df) -> None:
        """Feed the dropdown with the full VL DataFrame from
        :func:`iidm_viewer.network_loader.list_voltage_levels_for_selector`.
        ``None`` (or an empty frame) clears the combo."""
        self._vl_df = vls_df
        self._vl_filter.blockSignals(True)
        self._vl_filter.clear()
        self._vl_filter.blockSignals(False)
        self._rebuild_combo()

    def set_vl(self, vl_id: Optional[str]) -> None:
        """Sync the dropdown to an externally-set VL (e.g. map click)."""
        if vl_id is None or self._vl_df is None:
            return
        for i in range(self._vl_combo.count()):
            if self._vl_combo.itemData(i) == vl_id:
                if self._vl_combo.currentIndex() != i:
                    self._vl_combo.blockSignals(True)
                    self._vl_combo.setCurrentIndex(i)
                    self._vl_combo.blockSignals(False)
                return

    # -- Internals -------------------------------------------------------
    def _on_vl_filter_changed(self, _text: str) -> None:
        self._rebuild_combo()

    def _on_vl_combo_changed(self, _idx: int) -> None:
        vl_id = self._vl_combo.currentData()
        if vl_id:
            self._on_vl_selected(str(vl_id))

    def _rebuild_combo(self) -> None:
        # NOTE: programmatic repopulation never fires the selection
        # callback — the network's default-VL pick (highest V) flows in
        # via ``set_vl`` from the AppState's listener loop, and we
        # don't want to clobber it with the alphabetical first item.
        if self._vl_df is None or self._vl_df.empty:
            self._vl_combo.blockSignals(True)
            self._vl_combo.clear()
            self._vl_combo.setEnabled(False)
            self._vl_combo.blockSignals(False)
            return
        filtered = filter_voltage_levels(self._vl_df, self._vl_filter.text())
        # Preserve the current selection across re-filters when possible.
        current = self._vl_combo.currentData()
        self._vl_combo.blockSignals(True)
        self._vl_combo.clear()
        if filtered.empty:
            self._vl_combo.setEnabled(False)
        else:
            self._vl_combo.setEnabled(True)
            for _, row in filtered.iterrows():
                kv = (
                    f" ({row['nominal_v']:.0f} kV)"
                    if "nominal_v" in row and row["nominal_v"] == row["nominal_v"]  # NaN guard
                    else ""
                )
                self._vl_combo.addItem(f"{row['display']}{kv}", userData=row["id"])
            target_idx = 0
            if current:
                for i in range(self._vl_combo.count()):
                    if self._vl_combo.itemData(i) == current:
                        target_idx = i
                        break
            self._vl_combo.setCurrentIndex(target_idx)
        self._vl_combo.blockSignals(False)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("IIDM Viewer — PySide6 preview")
        self.resize(1280, 800)

        self.state = AppState(self)
        self.overview_tab = OverviewTab()
        self.map_tab = MapTab()
        self.nad_tab = NadTab()
        self.nad_tab.set_cache_backend(self.state.cache_backend)
        self.sld_tab = SldTab()
        self.sld_tab.set_cache_backend(self.state.cache_backend)
        self.sld_tab.set_state(self.state)
        self.data_tab = DataExplorerTab()
        self.extensions_tab = ExtensionsExplorerTab()
        self.reactive_curves_tab = ReactiveCurvesTab()
        self.operational_limits_tab = OperationalLimitsTab()
        self.security_analysis_tab = SecurityAnalysisTab()
        self.short_circuit_tab = ShortCircuitAnalysisTab()
        self.pmax_visualization_tab = PmaxVisualizationTab()
        self.voltage_analysis_tab = VoltageAnalysisTab()
        self.injection_map_tab = InjectionMapTab()

        self.tabs = QTabWidget()
        self.tabs.addTab(self.overview_tab, "Overview")
        self.tabs.addTab(self.map_tab, "Network Map")
        self.tabs.addTab(self.nad_tab, "Network Area Diagram")
        self.tabs.addTab(self.sld_tab, "Single Line Diagram")
        self.tabs.addTab(self.data_tab, "Data Explorer Components")
        self.tabs.addTab(self.extensions_tab, "Data Explorer Extensions")
        self.tabs.addTab(self.reactive_curves_tab, "Reactive Capability Curves")
        self.tabs.addTab(self.operational_limits_tab, "Operational Limits")
        self.tabs.addTab(self.security_analysis_tab, "Security Analysis")
        self.tabs.addTab(self.short_circuit_tab, "Short Circuit Analysis")
        self.tabs.addTab(self.pmax_visualization_tab, "Pmax Visualization")
        self.tabs.addTab(self.voltage_analysis_tab, "Voltage Analysis")
        self.tabs.addTab(self.injection_map_tab, "Injection Map")

        # The Data Explorer reports cell + bulk edits to the AppState's
        # ChangeLog so the panel below shows a unified history that
        # survives tab switches and component changes.
        self.data_tab.set_change_log(self.state.change_log)

        self.sidebar = _Sidebar(
            self._on_load_clicked,
            self._on_run_loadflow_clicked,
            self._on_sidebar_vl_selected,
            self._on_view_logs_clicked,
            self._on_lf_parameters_clicked,
            self._on_save_network_clicked,
            self._on_import_options_clicked,
            self._on_network_reduction_clicked,
            self._on_blank_network_clicked,
            self._on_view_session_script_clicked,
        )

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        # N-K variant dock — registered on the right side, closed by
        # default so the picker stays out of the way until the user
        # wants to build a contingency variant.
        self.nk_variant_dock = NkVariantDock()
        self.nk_variant_dock_widget = QDockWidget("N-K Variant", self)
        self.nk_variant_dock_widget.setWidget(self.nk_variant_dock)
        self.nk_variant_dock_widget.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea,
        )
        self.addDockWidget(
            Qt.RightDockWidgetArea, self.nk_variant_dock_widget,
        )
        self.nk_variant_dock_widget.setVisible(False)
        self.nk_variant_dock.set_state(self.state)

        status = QStatusBar()
        status.showMessage("Ready. Load a network to begin.")
        self.setStatusBar(status)

        self.state.network_changed.connect(self._on_network_changed)
        self.state.loadflow_completed.connect(self._on_loadflow_completed)
        self.state.selected_vl_changed.connect(self._on_selected_vl_changed)
        self.map_tab.substation_clicked.connect(self._on_map_substation_clicked)
        self.nad_tab.node_clicked.connect(self._on_nad_node_clicked)
        self.sld_tab.feeder_clicked.connect(self._on_sld_feeder_clicked)
        self.sld_tab.vl_navigation_requested.connect(self._on_sld_vl_navigation)
        self.sld_tab.breaker_toggled.connect(self._on_sld_breaker_toggled)
        self.data_tab.edit_applied.connect(self._on_data_edit_applied)
        self.data_tab.bulk_edit_applied.connect(self._on_data_bulk_edit_applied)
        self.data_tab.bulk_removed.connect(self._on_data_bulk_removed)
        self.data_tab.loadflow_requested.connect(self._on_run_loadflow_clicked)

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------
    def _on_blank_network_clicked(self) -> None:
        """Mirror Streamlit's "Start with empty network" dialog.

        Prompts for a network id (defaulting to ``"network"``), then
        installs a fresh pypowsybl ``create_empty`` Network. The
        AppState's listener loop refreshes every tab against the new
        (empty) topology — the user can then build it up via the
        Data Explorer's "Create a new …" forms.
        """
        network_id, ok = QInputDialog.getText(
            self,
            "Start with empty network",
            "Network id:",
            text="network",
        )
        if not ok:
            return
        try:
            self.state.create_empty_network(network_id or "network")
        except Exception as exc:
            QMessageBox.critical(
                self, "Empty network failed", f"Failed to create: {exc}",
            )
            return
        self.sidebar.set_file(None)
        self.statusBar().showMessage(
            f"Started empty network — id: {network_id or 'network'}.",
        )

    def _on_load_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load network",
            os.getcwd(),
            "IIDM / CIM / UCTE / MATPOWER (*.xiidm *.iidm *.xml *.zip *.mat *.uct);;All files (*)",
        )
        if not path:
            return
        self.statusBar().showMessage(f"Loading {os.path.basename(path)}…")
        try:
            self.state.load_network_from_path(path)
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
            self.statusBar().showMessage("Load failed.")
            return
        self.sidebar.set_file(path)
        self.statusBar().showMessage(f"Loaded {os.path.basename(path)}.")

    def open_file(self, path: str) -> None:
        """Programmatic load — used by the CLI for `iidm-viewer-pyside FILE`."""
        self.state.load_network_from_path(path)
        self.sidebar.set_file(path)
        self.statusBar().showMessage(f"Loaded {os.path.basename(path)}.")

    def _on_run_loadflow_clicked(self) -> None:
        if self.state.network is None:
            return
        self.statusBar().showMessage("Running AC load flow…")
        self.sidebar.set_loadflow_status("Running…", ok=True)
        try:
            self.state.run_loadflow()
        except Exception as exc:
            self.sidebar.set_loadflow_status(f"Failed: {exc}", ok=False)
            self.statusBar().showMessage(f"Load flow failed: {exc}")

    def _on_view_logs_clicked(self) -> None:
        """Open the LFReportDialog with the cached ``report_json``.

        The button is gated by ``set_view_logs_enabled``, but the
        AppState may have been cleared between enable and click — fall
        back to a status-bar hint rather than throwing.
        """
        report_json = self.state.last_report_json
        if not report_json:
            self.statusBar().showMessage(
                "No load flow report available. Run a load flow first.",
            )
            return
        from iidm_viewer.qt.lf_report_dialog import LFReportDialog
        dlg = LFReportDialog(report_json, self)
        dlg.exec()

    def _on_view_session_script_clicked(self) -> None:
        """Open the SessionScriptDialog showing the auto-recorded log.

        Always available — the dialog handles the empty-log case
        gracefully so the user can pause / resume Recording even
        before loading a network.
        """
        from iidm_viewer.qt.session_script_dialog import SessionScriptDialog
        SessionScriptDialog(self).exec()

    def _on_network_reduction_clicked(self) -> None:
        """Open the three-mode reduction dialog.

        On a successful Apply, the dialog calls
        :meth:`AppState.notify_network_changed` so every listener
        (diagram caches, data explorer, sidebar VL picker) refreshes
        against the reduced topology — same effect as Streamlit's
        ``invalidate_on_network_replace``.
        """
        if self.state.network is None:
            self.statusBar().showMessage("No network loaded.")
            return
        from iidm_viewer.network_reduction_actions import list_voltage_level_ids
        from iidm_viewer.qt.network_reduction_dialog import NetworkReductionDialog

        try:
            vl_ids = list_voltage_level_ids(self.state.network)
        except Exception:
            vl_ids = []
        dlg = NetworkReductionDialog(self.state.network, vl_ids, parent=self)
        dlg.exec()
        if dlg.applied:
            self.state.notify_network_changed()
            self.statusBar().showMessage("Network reduction applied.")

    def _on_import_options_clicked(self) -> None:
        """Mirror Streamlit's "Import options…" dialog.

        Opens :class:`LoadOptionsDialog` with the AppState-cached
        format / params / post-processors as initial values; on Save
        writes them back so the next ``load_network_from_path`` picks
        them up.
        """
        from iidm_viewer.io_options_schema import (
            get_import_formats,
            get_import_post_processors,
        )
        from iidm_viewer.qt.load_options_dialog import LoadOptionsDialog
        try:
            formats = get_import_formats()
        except Exception as exc:
            QMessageBox.critical(self, "Import options", f"Failed to list formats: {exc}")
            return
        try:
            post_processors = get_import_post_processors()
        except Exception:
            post_processors = []
        dlg = LoadOptionsDialog(
            formats=formats,
            post_processors=post_processors,
            current_format=self.state.import_format,
            current_params=self.state.import_params,
            current_post_processors=self.state.import_post_processors,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        self.state.import_format = dlg.format
        self.state.import_params = dlg.params
        self.state.import_post_processors = dlg.post_processors
        n_params = len(dlg.params)
        n_pp = len(dlg.post_processors)
        fmt_label = dlg.format or "auto-detect"
        self.statusBar().showMessage(
            f"Import options updated — format: {fmt_label}, "
            f"{n_params} param override(s), {n_pp} post-processor(s).",
        )

    def _on_save_network_clicked(self) -> None:
        """Mirror Streamlit's "Save network" dialog.

        Two steps:

        * Pick an export format from pypowsybl's available list (uses
          the shared :func:`network_loader.get_export_formats`).
        * Pick a save path via ``QFileDialog`` — pre-fills the filename
          with the source basename + the format's natural extension.

        The export itself runs through the shared
        :func:`network_loader.export_network` (worker-routed, unwraps
        single-file ZIPs), so the bytes the user gets are the same as
        Streamlit's download.
        """
        if self.state.network is None:
            self.statusBar().showMessage("No network loaded.")
            return
        try:
            formats = get_export_formats()
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"Failed to list formats: {exc}")
            return
        if not formats:
            QMessageBox.warning(self, "Save network", "No export formats available.")
            return
        from iidm_viewer.qt.save_network_dialog import SaveNetworkDialog
        dlg = SaveNetworkDialog(formats, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        fmt = dlg.selected_format
        if not fmt:
            return

        # File dialog. Pre-fill with the loaded filename + the natural
        # extension for the chosen format (XIIDM → .xiidm, etc.).
        suggested = f"network.{fmt.lower()}"
        path, _ = QFileDialog.getSaveFileName(
            self, f"Save network as {fmt}", suggested,
            f"{fmt} (*.{fmt.lower()})",
        )
        if not path:
            return

        self.statusBar().showMessage(f"Exporting to {fmt}…")
        try:
            data, ext = export_network(
                self.state.network, fmt,
                parameters=dlg.parameters or None,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"Export failed: {exc}")
            self.statusBar().showMessage(f"Export failed: {exc}")
            return
        # Reflect the natural extension reported by ``export_network``
        # (XIIDM unwraps to ``.xiidm``, multi-file → ``.zip``).
        if not path.lower().endswith(f".{ext.lower()}"):
            path = f"{path}.{ext.lower()}"
        try:
            with open(path, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", f"Write failed: {exc}")
            return
        self.statusBar().showMessage(f"Network saved to {os.path.basename(path)}.")

    def _on_lf_parameters_clicked(self) -> None:
        """Open the LFParametersDialog and persist the result onto AppState.

        The dialog reads / writes the same dicts that
        ``AppState.run_loadflow`` forwards to pypowsybl, so a subsequent
        "Run AC Load Flow" click uses the new overrides automatically.
        """
        from iidm_viewer.qt.lf_parameters_dialog import LFParametersDialog
        dlg = LFParametersDialog(
            generic_overrides=self.state.lf_generic_params,
            provider_overrides=self.state.lf_provider_params,
            parent=self,
        )
        if dlg.exec() == QDialog.Accepted:
            self.state.lf_generic_params = dlg.generic_params
            self.state.lf_provider_params = dlg.provider_params
            self.statusBar().showMessage("Load Flow parameters updated.")

    def _on_loadflow_completed(self, result) -> None:
        """Refresh peripheral views once the flow returns.

        LF rewrites the network's flows (P / Q / I on branches) and
        bus voltages. The NAD / SLD bundles bake those into the
        rendered SVG, so the per-VL diagram caches need a flush;
        the Data Explorer's enriched columns also change.
        """
        ok = bool(result and result.converged)
        status = result.status if result else "UNKNOWN"
        self.sidebar.set_loadflow_status(f"Status: {status}", ok=ok)
        # AppState now holds ``last_report_json`` — enable "View Logs"
        # whenever a non-empty report was produced.
        self.sidebar.set_view_logs_enabled(bool(self.state.last_report_json))
        self.statusBar().showMessage(
            f"AC load flow: {status}",
        )
        # Flush diagram caches (P/Q labels change) and refresh the
        # currently-shown VL diagrams. ``AppState.run_loadflow`` has
        # already popped the NAD and SLD slots in the shared backend
        # via ``invalidate_load_flow``; this just re-renders.
        if self.state.selected_vl:
            self.nad_tab.show_voltage_level(self.state.selected_vl)
            self.sld_tab.show_voltage_level(self.state.selected_vl)
        # Refresh the Data Explorer in case the user is looking at
        # something the LF touched (lines/transformers with new flows).
        self.data_tab.set_network(self.state.network)
        # Extensions tab also caches per-network data — refresh it so
        # LF-touched extensions (e.g. branch flows) reflect the run.
        self.extensions_tab.set_network(self.state.network)
        # Reactive Curves: post-LF the gen ``q`` column flips PV gens
        # from ``needs_lf`` to an actionable status; re-run the
        # classification.
        self.reactive_curves_tab.refresh()
        # Operational Limits: post-LF branch I + p1/p2 change → loading
        # table + losses metric + chart need a redraw.
        self.operational_limits_tab.refresh()
        # Pmax Visualization: needs both bus v_mag and line p1 — both
        # come from the LF — so a refresh after a run is the only way
        # to populate it.
        self.pmax_visualization_tab.refresh()
        # Voltage Analysis: bus v_mag + shunt/SVC q come from the LF —
        # refresh so the summary, the per-pu drill-down, and the
        # current-Q metrics reflect the post-LF state.
        self.voltage_analysis_tab.refresh()
        # Injection Map: terminal p/q populate post-LF — refresh so the
        # markers track realised values instead of scheduled setpoints.
        self.injection_map_tab.refresh()
        # Overview: branch p1/p2 (losses) + generator/load p come from
        # the LF — refresh so the country-totals table fills in actuals
        # and the losses metrics populate.
        self.overview_tab.refresh()

    # ------------------------------------------------------------------
    # State → UI plumbing
    # ------------------------------------------------------------------
    def _on_network_changed(self, network) -> None:
        # Refresh the VL dropdown before the rest — the default-VL pick
        # that fires next will land on a populated combo.
        if network is None:
            self.sidebar.set_voltage_levels(None)
        else:
            try:
                vls_df = list_voltage_levels_for_selector(network)
            except Exception:
                vls_df = None
            self.sidebar.set_voltage_levels(vls_df)
        self.sidebar.set_loadflow_enabled(network is not None)
        self.sidebar.set_loadflow_status("")
        # AppState wipes ``last_report_json`` on a fresh load; reflect that.
        self.sidebar.set_view_logs_enabled(False)
        self.sidebar.set_save_enabled(network is not None)
        self.sidebar.set_reduction_enabled(network is not None)
        # Surface the N-K dock once a network is loaded so it's
        # discoverable; keep it hidden when there's nothing to outage.
        self.nk_variant_dock_widget.setVisible(network is not None)
        self.overview_tab.set_network(network)
        self.map_tab.set_network(network)
        self.nad_tab.set_network(network)
        self.sld_tab.set_network(network)
        self.data_tab.set_network(network)
        self.extensions_tab.set_network(network)
        self.reactive_curves_tab.set_network(network)
        self.operational_limits_tab.set_network(network)
        self.security_analysis_tab.set_network(network)
        self.short_circuit_tab.set_network(network)
        self.pmax_visualization_tab.set_network(network)
        self.voltage_analysis_tab.set_network(network)
        self.injection_map_tab.set_network(network)
        self.tabs.setCurrentWidget(self.map_tab)

    def _on_sidebar_vl_selected(self, vl_id: str) -> None:
        """Forward a dropdown pick into the AppState — same path as a
        map / NAD / SLD click. The state's listener loop fans out to
        the data tab + the diagrams from there."""
        self.state.set_selected_vl(vl_id)

    def _on_selected_vl_changed(self, vl_id: str) -> None:
        self.sidebar.set_vl(vl_id or None)
        # Push the active VL into the data tab so its "Filter by VL"
        # checkbox can use it.
        self.data_tab.set_selected_vl(vl_id or None)
        # Mirror to the reactive-curves tab so its "Only generators in
        # VL X" checkbox label tracks the active selection.
        self.reactive_curves_tab.set_selected_vl(vl_id or None)
        # Same for the Pmax tab's "Only lines connected to VL X" toggle.
        self.pmax_visualization_tab.set_selected_vl(vl_id or None)
        if vl_id:
            # Both diagram tabs follow the selection; they cache by VL
            # so re-centering on tab focus is essentially free.
            self.nad_tab.show_voltage_level(vl_id)
            self.sld_tab.show_voltage_level(vl_id)

    def _on_map_substation_clicked(self, vl_ids) -> None:
        """Killer interaction #1 — Map → SLD.

        Picks the highest-V voltage level of the clicked substation
        (the map JS already ordered them that way), sets it as the
        selected VL — which triggers SLD + NAD generation — and pulls
        the SLD tab to the front. No script rerun, no websocket
        round-trip, no rebuild of unrelated widgets.
        """
        if not vl_ids:
            return
        vl_id = vl_ids[0]
        self.tabs.setCurrentWidget(self.sld_tab)
        self.state.set_selected_vl(vl_id)

    def _on_data_bulk_removed(self, component: str, removed_ids) -> None:
        """A deletion always changes topology — flush the diagram caches
        and re-render the current VL so the user sees the holes."""
        self.nad_tab._cache.clear()
        self.sld_tab._cache.clear()
        if self.state.selected_vl:
            self.nad_tab.show_voltage_level(self.state.selected_vl)
            self.sld_tab.show_voltage_level(self.state.selected_vl)
        self.statusBar().showMessage(
            f"{component}: removed {len(removed_ids)} element(s)"
        )

    def _on_data_bulk_edit_applied(self, component: str, ids, attribute: str, new_value, prev_map) -> None:
        from iidm_viewer.component_registry import TOPOLOGY_AFFECTING_ATTRIBUTES
        if attribute in TOPOLOGY_AFFECTING_ATTRIBUTES:
            self.nad_tab._cache.clear()
            self.sld_tab._cache.clear()
            if self.state.selected_vl:
                self.nad_tab.show_voltage_level(self.state.selected_vl)
                self.sld_tab.show_voltage_level(self.state.selected_vl)
        self.statusBar().showMessage(
            f"{component}: bulk {attribute} = {new_value} applied to {len(ids)} rows"
        )

    def _on_data_edit_applied(self, component: str, element_id: str, attribute: str, new_value, prev) -> None:
        """Drop NAD / SLD caches when an edit can change diagram geometry.

        Most edits (target_p, target_q, voltage setpoints) leave the
        SVG topology unchanged, so the diagram caches stay valid.
        Switch / breaker toggles and connection flips need a regen;
        the cheap way to be correct is to wipe the caches and let the
        next ``show_voltage_level`` redraw on demand.
        """
        from iidm_viewer.component_registry import TOPOLOGY_AFFECTING_ATTRIBUTES
        if attribute in TOPOLOGY_AFFECTING_ATTRIBUTES:
            self.nad_tab._cache.clear()
            self.sld_tab._cache.clear()
            if self.state.selected_vl:
                self.nad_tab.show_voltage_level(self.state.selected_vl)
                self.sld_tab.show_voltage_level(self.state.selected_vl)
        self.statusBar().showMessage(
            f"{component}/{element_id}/{attribute}: {prev} → {new_value}"
        )

    def _on_sld_vl_navigation(self, vl_id: str) -> None:
        """User clicked an "→ next voltage level" arrow on the SLD.

        Same path as Map / NAD click: update ``selected_vl`` and the
        SLD + NAD tabs follow via their state listeners. Mirrors
        Streamlit's ``diagrams.render_sld_tab`` handler.
        """
        if not vl_id:
            return
        self.state.set_selected_vl(vl_id)

    def _on_sld_breaker_toggled(self, switch_id: str, new_open: bool) -> None:
        """User clicked a switch/breaker symbol on the SLD.

        Mirrors Streamlit's behaviour (``diagrams.render_sld_tab``):
        toggle the switch via the shared ``toggle_switch``, record the
        change in the ChangeLog under the canonical ``Switches`` /
        ``open`` keys, and let the NAD/SLD caches flush via the
        existing bulk_edit_applied path.
        """
        from iidm_viewer.component_registry import toggle_switch
        if self.state.network is None or not switch_id:
            return
        try:
            before, after = toggle_switch(self.state.network, switch_id, bool(new_open))
        except Exception as exc:
            self.statusBar().showMessage(f"Switch toggle failed: {exc}")
            return
        # Record in the change log so the user can revert via the panel.
        self.state.change_log.record(
            "Switches", switch_id, "open", before, after,
        )
        # Topology-affecting attribute: flush diagram caches +
        # re-render the current VL so the new switch state is visible.
        self.nad_tab._cache.clear()
        self.sld_tab._cache.clear()
        if self.state.selected_vl:
            self.nad_tab.show_voltage_level(self.state.selected_vl)
            self.sld_tab.show_voltage_level(self.state.selected_vl)
        self.statusBar().showMessage(
            f"Switch {switch_id}: open={before} → open={after}"
        )

    def _on_sld_feeder_clicked(self, payload: dict) -> None:
        """Killer interaction #3 — SLD feeder → Map substation.

        The SLD bundle reports the equipment that lives at the end of
        the clicked feeder bay; :func:`navigation.resolve_feeder_substation`
        walks pypowsybl to find the substation "on the other side"
        (or the local one for injections), and the Map tab flies to it.
        """
        from iidm_viewer.navigation import resolve_feeder_substation

        if self.state.network is None:
            return
        equipment_id = payload.get("equipment_id")
        equipment_type = payload.get("equipment_type")
        current_vl = payload.get("current_vl_id") or self.state.selected_vl
        if not equipment_id or not current_vl:
            return
        substation_id = resolve_feeder_substation(
            self.state.network, str(current_vl), str(equipment_id), equipment_type,
        )
        if not substation_id:
            self.statusBar().showMessage(
                f"No substation known for {equipment_type or 'feeder'} {equipment_id}"
            )
            return
        self.tabs.setCurrentWidget(self.map_tab)
        self.map_tab.focus_substation(substation_id)
        self.statusBar().showMessage(
            f"Map: focused substation {substation_id} "
            f"(via {equipment_type or 'feeder'} {equipment_id})"
        )

    def _on_nad_node_clicked(self, vl_id: str) -> None:
        """Killer interaction #2 — NAD → SLD.

        The user picks a node on the Network Area Diagram and lands on
        its Single Line Diagram immediately. Same signal-driven path
        as the map → SLD jump; the NAD itself also re-centers on the
        new VL (handled by ``_on_selected_vl_changed``) so returning
        to the NAD tab shows the relevant area.
        """
        if not vl_id:
            return
        self.tabs.setCurrentWidget(self.sld_tab)
        self.state.set_selected_vl(vl_id)


def run_app(initial_file: Optional[str] = None) -> int:
    """Boot the QApplication and the main window.

    A second call inside the same process reuses the already-existing
    ``QApplication`` instance — convenient for tests and for embedding.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    if initial_file:
        window.open_file(initial_file)
    return app.exec()
