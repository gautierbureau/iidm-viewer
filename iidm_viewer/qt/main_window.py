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

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.qt.data_explorer_tab import DataExplorerTab
from iidm_viewer.qt.map_tab import MapTab
from iidm_viewer.qt.nad_tab import NadTab
from iidm_viewer.qt.sld_tab import SldTab
from iidm_viewer.qt.state import AppState


class _Sidebar(QWidget):
    def __init__(self, on_load, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(220)
        self.setStyleSheet("background: #f6f6f6;")

        title = QLabel("IIDM Viewer\n(PySide6 preview)")
        title.setStyleSheet("font-weight: bold; padding: 12px 10px;")

        self._load_btn = QPushButton("Load network…")
        self._load_btn.clicked.connect(on_load)

        self._file_lbl = QLabel("No file loaded.")
        self._file_lbl.setWordWrap(True)
        self._file_lbl.setStyleSheet("padding: 8px 10px; color: #555; font-size: 11px;")

        self._vl_lbl = QLabel("Selected VL: —")
        self._vl_lbl.setStyleSheet("padding: 8px 10px; color: #333; font-size: 12px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(title)
        layout.addWidget(self._load_btn)
        layout.addWidget(self._file_lbl)
        layout.addWidget(self._vl_lbl)
        layout.addStretch(1)

    def set_file(self, path: Optional[str]) -> None:
        self._file_lbl.setText(os.path.basename(path) if path else "No file loaded.")

    def set_vl(self, vl_id: Optional[str]) -> None:
        self._vl_lbl.setText(f"Selected VL: {vl_id}" if vl_id else "Selected VL: —")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("IIDM Viewer — PySide6 preview")
        self.resize(1280, 800)

        self.state = AppState(self)
        self.map_tab = MapTab()
        self.nad_tab = NadTab()
        self.sld_tab = SldTab()
        self.data_tab = DataExplorerTab()

        self.tabs = QTabWidget()
        self.tabs.addTab(self.map_tab, "Network Map")
        self.tabs.addTab(self.nad_tab, "Network Area Diagram")
        self.tabs.addTab(self.sld_tab, "Single Line Diagram")
        self.tabs.addTab(self.data_tab, "Data Explorer Components")

        # The Data Explorer reports cell + bulk edits to the AppState's
        # ChangeLog so the panel below shows a unified history that
        # survives tab switches and component changes.
        self.data_tab.set_change_log(self.state.change_log)

        self.sidebar = _Sidebar(self._on_load_clicked)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        status = QStatusBar()
        status.showMessage("Ready. Load a network to begin.")
        self.setStatusBar(status)

        self.state.network_changed.connect(self._on_network_changed)
        self.state.selected_vl_changed.connect(self._on_selected_vl_changed)
        self.map_tab.substation_clicked.connect(self._on_map_substation_clicked)
        self.nad_tab.node_clicked.connect(self._on_nad_node_clicked)
        self.sld_tab.feeder_clicked.connect(self._on_sld_feeder_clicked)
        self.sld_tab.vl_navigation_requested.connect(self._on_sld_vl_navigation)
        self.sld_tab.breaker_toggled.connect(self._on_sld_breaker_toggled)
        self.data_tab.edit_applied.connect(self._on_data_edit_applied)
        self.data_tab.bulk_edit_applied.connect(self._on_data_bulk_edit_applied)
        self.data_tab.bulk_removed.connect(self._on_data_bulk_removed)

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # State → UI plumbing
    # ------------------------------------------------------------------
    def _on_network_changed(self, network) -> None:
        self.sidebar.set_vl(None)
        self.map_tab.set_network(network)
        self.nad_tab.set_network(network)
        self.sld_tab.set_network(network)
        self.data_tab.set_network(network)
        self.tabs.setCurrentWidget(self.map_tab)

    def _on_selected_vl_changed(self, vl_id: str) -> None:
        self.sidebar.set_vl(vl_id or None)
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
