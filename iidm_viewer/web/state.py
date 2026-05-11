"""Framework-agnostic UI state for the NiceGUI prototype.

Plain Python observer pattern — no NiceGUI-specific types here so the
state can be exercised in unit tests without booting a server. The
pypowsybl worker (``iidm_viewer.powsybl_worker``) is reused as-is so
the GraalVM thread-affinity rule from AGENTS.md §1 holds.
"""
from __future__ import annotations

from typing import Callable, Optional

from iidm_viewer.powsybl_worker import NetworkProxy, run


_NetworkListener = Callable[[Optional[NetworkProxy]], None]
_VlListener = Callable[[Optional[str]], None]


class AppState:
    """Single source of truth for the open network and selected VL."""

    def __init__(self) -> None:
        self._network: Optional[NetworkProxy] = None
        self._selected_vl: Optional[str] = None
        self._network_listeners: list[_NetworkListener] = []
        self._vl_listeners: list[_VlListener] = []

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    @property
    def network(self) -> Optional[NetworkProxy]:
        return self._network

    @property
    def selected_vl(self) -> Optional[str]:
        return self._selected_vl

    # ------------------------------------------------------------------
    # Listener registration
    # ------------------------------------------------------------------
    def on_network_changed(self, listener: _NetworkListener) -> None:
        self._network_listeners.append(listener)

    def on_selected_vl_changed(self, listener: _VlListener) -> None:
        self._vl_listeners.append(listener)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def load_network_from_path(self, path: str) -> NetworkProxy:
        """Load a network and auto-select the highest-nominal-V VL.

        Mirrors :class:`iidm_viewer.qt.state.AppState.load_network_from_path`
        so both prototypes behave the same on open.
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
        self._selected_vl = None
        for listener in list(self._network_listeners):
            listener(network)
        if default_vl:
            self.set_selected_vl(default_vl)
        return network

    def set_selected_vl(self, vl_id: Optional[str]) -> None:
        new = vl_id or None
        if new == self._selected_vl:
            return
        self._selected_vl = new
        for listener in list(self._vl_listeners):
            listener(new)
