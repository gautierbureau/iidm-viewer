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
from iidm_viewer.loadflow import LoadFlowResult, run_ac
from iidm_viewer.powsybl_worker import NetworkProxy


_NetworkListener = Callable[[Optional[NetworkProxy]], None]
_VlListener = Callable[[Optional[str]], None]
_LoadFlowListener = Callable[[LoadFlowResult], None]


class AppState:
    """Single source of truth for the open network and selected VL."""

    def __init__(self) -> None:
        self._network: Optional[NetworkProxy] = None
        self._selected_vl: Optional[str] = None
        self._network_listeners: list[_NetworkListener] = []
        self._vl_listeners: list[_VlListener] = []
        self._loadflow_listeners: list[_LoadFlowListener] = []
        # Last AC-LF report payload — cached so the sidebar's "View
        # Logs" button can open the dialog without re-running the LF.
        # Cleared on every new network load.
        self._last_report_json: Optional[str] = None
        # Persisted LF parameter overrides — set by the LF parameters
        # dialog and forwarded by :meth:`run_loadflow`. Empty dicts
        # mean "use pypowsybl's defaults".
        self.lf_generic_params: dict = {}
        self.lf_provider_params: dict = {}
        # Persisted import-side overrides — set by the LoadOptions
        # dialog and threaded through the upload handler so the next
        # file load applies them. ``import_format`` is the explicit
        # format override (``None`` means "auto-detect").
        self.import_format: Optional[str] = None
        self.import_params: dict = {}
        self.import_post_processors: list = []
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

    @property
    def last_report_json(self) -> Optional[str]:
        """JSON-encoded report from the most recent ``run_loadflow()`` —
        ``None`` when no LF has been run for the current network."""
        return self._last_report_json

    # ------------------------------------------------------------------
    # Listener registration
    # ------------------------------------------------------------------
    def on_network_changed(self, listener: _NetworkListener) -> None:
        self._network_listeners.append(listener)

    def on_selected_vl_changed(self, listener: _VlListener) -> None:
        self._vl_listeners.append(listener)

    def on_loadflow_completed(self, listener: _LoadFlowListener) -> None:
        self._loadflow_listeners.append(listener)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def load_network_from_path(self, path: str) -> NetworkProxy:
        """Load a network and apply it. Convenience for synchronous callers
        (startup, tests). NiceGUI's upload handler should instead pull the
        loader call into ``asyncio.to_thread`` and finish with
        :meth:`install_network` so the listener callbacks run on the
        event-loop thread (where NiceGUI's slot stack is populated).

        Delegates to :mod:`iidm_viewer.network_loader` for both the load
        itself and the "highest nominal V" default-VL pick so the
        Streamlit, PySide6 and NiceGUI hosts share one code path.
        """
        network = network_loader.load_from_path(path)
        self.install_network(network)
        return network

    def install_network(self, network: NetworkProxy) -> None:
        """Apply a pre-loaded network and broadcast listeners.

        Split from :meth:`load_network_from_path` so the heavy load can
        happen on a worker thread (``asyncio.to_thread``) while listener
        callbacks still fire on the caller's thread — required for
        NiceGUI, where UI mutations need the page slot stack to be
        populated by the event loop.
        """
        default_vl = network_loader.pick_default_vl(network)
        self._network = network
        self._selected_vl = None
        self._last_report_json = None
        self.change_log.clear()
        for listener in list(self._network_listeners):
            listener(network)
        if default_vl:
            self.set_selected_vl(default_vl)

    def set_selected_vl(self, vl_id: Optional[str]) -> None:
        new = vl_id or None
        if new == self._selected_vl:
            return
        self._selected_vl = new
        for listener in list(self._vl_listeners):
            listener(new)

    def notify_network_changed(self) -> None:
        """Re-broadcast the same network after an in-place mutation.

        Used by the "Network Reduction" dialog so all listeners
        (diagram caches, data explorer, VL picker) refresh against
        the reduced topology without going through a full reload.
        """
        network = self._network
        if network is None:
            return
        self._selected_vl = None
        self._last_report_json = None
        self.change_log.clear()
        default_vl = network_loader.pick_default_vl(network)
        for listener in list(self._network_listeners):
            listener(network)
        if default_vl:
            self.set_selected_vl(default_vl)

    def run_loadflow(
        self,
        generic_params: Optional[dict] = None,
        provider_params: Optional[dict] = None,
    ) -> Optional[LoadFlowResult]:
        """Run AC load flow on the open network and broadcast the result.

        Returns ``None`` when no network is loaded.
        """
        if self._network is None:
            return None
        # Fall back to the AppState-cached parameters set by the
        # LF parameters dialog when no explicit override is passed.
        if generic_params is None:
            generic_params = self.lf_generic_params or None
        if provider_params is None:
            provider_params = self.lf_provider_params or None
        result = run_ac(self._network, generic_params, provider_params)
        self._last_report_json = getattr(result, "report_json", None)
        for listener in list(self._loadflow_listeners):
            listener(result)
        return result
