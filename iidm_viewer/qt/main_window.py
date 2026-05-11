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
