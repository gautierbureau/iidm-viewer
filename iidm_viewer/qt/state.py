"""Framework-agnostic UI state for the PySide6 prototype.

Replaces ``st.session_state`` with a single ``AppState`` QObject. All
pypowsybl access still goes through ``iidm_viewer.powsybl_worker.run``
so the GraalVM thread-affinity rule documented in AGENTS.md §1 holds
unchanged.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal

from iidm_viewer.powsybl_worker import NetworkProxy, run


class AppState(QObject):
    """Single source of truth for the open network and selected VL."""

    network_changed = Signal(object)        # NetworkProxy | None
    selected_vl_changed = Signal(str)       # vl_id (empty string when cleared)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._selected_vl: Optional[str] = None

    @property
    def network(self) -> Optional[NetworkProxy]:
        return self._network

    @property
    def selected_vl(self) -> Optional[str]:
        return self._selected_vl

    def load_network_from_path(self, path: str) -> NetworkProxy:
        """Load a network file on the pypowsybl worker thread."""
        def _load():
            import pypowsybl.network as pn
            return pn.load(path)

        network = NetworkProxy(run(_load))
        self._network = network
        self._selected_vl = None
        self.network_changed.emit(network)
        return network

    def set_selected_vl(self, vl_id: Optional[str]) -> None:
        new = vl_id or None
        if new == self._selected_vl:
            return
        self._selected_vl = new
        self.selected_vl_changed.emit(new or "")
