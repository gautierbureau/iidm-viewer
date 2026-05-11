"""Framework-agnostic UI state for the PySide6 prototype.

Replaces ``st.session_state`` with a single ``AppState`` QObject. The
actual network load + default-VL pick lives in
``iidm_viewer.network_loader`` so the Streamlit and NiceGUI front-ends
share the same code path. The GraalVM thread-affinity rule documented
in AGENTS.md §1 is preserved by routing every pypowsybl call through
``iidm_viewer.powsybl_worker.run``.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal

from iidm_viewer import network_loader
from iidm_viewer.change_log import ChangeLog
from iidm_viewer.loadflow import LoadFlowResult, run_ac
from iidm_viewer.powsybl_worker import NetworkProxy


class AppState(QObject):
    """Single source of truth for the open network and selected VL."""

    network_changed = Signal(object)        # NetworkProxy | None
    selected_vl_changed = Signal(str)       # vl_id (empty string when cleared)
    # Emitted after each AC load-flow run. Carries the wrapper from
    # :class:`iidm_viewer.loadflow.LoadFlowResult` — the host can
    # inspect ``.status`` / ``.converged`` for the UI status routing,
    # and stash ``.report_json`` for an optional logs dialog.
    loadflow_completed = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._selected_vl: Optional[str] = None
        # One ChangeLog per process. Reset on every network reload so
        # entries don't leak between unrelated networks.
        self.change_log = ChangeLog()

    @property
    def network(self) -> Optional[NetworkProxy]:
        return self._network

    @property
    def selected_vl(self) -> Optional[str]:
        return self._selected_vl

    def load_network_from_path(self, path: str) -> NetworkProxy:
        """Load a network and auto-select the highest-nominal-V VL.

        Both the load and the default-VL pick run on the pypowsybl
        worker thread via :mod:`iidm_viewer.network_loader`.
        """
        network = network_loader.load_from_path(path)
        default_vl = network_loader.pick_default_vl(network)
        self._network = network
        self._selected_vl = None  # cleared first so set_selected_vl emits below
        self.change_log.clear()
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

    def run_loadflow(
        self,
        generic_params: Optional[dict] = None,
        provider_params: Optional[dict] = None,
    ) -> Optional[LoadFlowResult]:
        """Run AC load flow on the open network.

        Returns ``None`` when no network is loaded. The returned
        :class:`LoadFlowResult` is also broadcast via
        :pyattr:`loadflow_completed` so peripheral panels (diagram
        caches, data-explorer refresh) can update.
        """
        if self._network is None:
            return None
        result = run_ac(self._network, generic_params, provider_params)
        self.loadflow_completed.emit(result)
        return result
