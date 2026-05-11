"""Framework-agnostic UI state for the NiceGUI prototype.

Plain Python observer pattern — no NiceGUI-specific types here so the
state can be exercised in unit tests without booting a server. The
actual network load + default-VL pick live in
:mod:`iidm_viewer.network_loader`, shared with the Streamlit and
PySide6 front-ends. The GraalVM thread-affinity rule from AGENTS.md
§1 is preserved.
"""
from __future__ import annotations

from typing import Callable, Optional

from iidm_viewer import network_loader
from iidm_viewer.change_log import ChangeLog
from iidm_viewer.powsybl_worker import NetworkProxy


_NetworkListener = Callable[[Optional[NetworkProxy]], None]
_VlListener = Callable[[Optional[str]], None]


class AppState:
    """Single source of truth for the open network and selected VL."""

    def __init__(self) -> None:
        self._network: Optional[NetworkProxy] = None
        self._selected_vl: Optional[str] = None
        self._network_listeners: list[_NetworkListener] = []
        self._vl_listeners: list[_VlListener] = []
        # One ChangeLog per process. Reset on every network reload.
        self.change_log = ChangeLog()

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

        Delegates to :mod:`iidm_viewer.network_loader` for both the
        load itself and the "highest nominal V" default-VL pick so
        the Streamlit, PySide6 and NiceGUI hosts share one code path.
        """
        network = network_loader.load_from_path(path)
        default_vl = network_loader.pick_default_vl(network)
        self._network = network
        self._selected_vl = None
        self.change_log.clear()
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
