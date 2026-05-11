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
        """Load a network file and auto-select a default voltage level.

        The default matches the Streamlit ``vl_selector``: the VL with
        the highest nominal voltage (e.g. 400 kV). Both diagram tabs
        react to ``selected_vl_changed`` and can render immediately
        after load — no "select a VL first" empty state.
        """
        def _load_and_default():
            import pypowsybl.network as pn
            net = pn.load(path)
            vls = net.get_voltage_levels()
            default = None
            if vls is not None and not vls.empty:
                if "nominal_v" in vls.columns:
                    default = str(vls["nominal_v"].idxmax())
                else:
                    default = str(vls.index[0])
            return net, default

        net, default_vl = run(_load_and_default)
        network = NetworkProxy(net)
        self._network = network
        self._selected_vl = None  # cleared first so set_selected_vl emits below
        self.network_changed.emit(network)
        if default_vl:
            self.set_selected_vl(default_vl)
        return network

    def set_selected_vl(self, vl_id: Optional[str]) -> None:
        new = vl_id or None
        if new == self._selected_vl:
            return
        self._selected_vl = new
        self.selected_vl_changed.emit(new or "")
