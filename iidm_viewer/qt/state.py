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
        # Last AC-LF report payload — cached so the sidebar's
        # "View Logs" button can open the dialog without re-running
        # the load flow. Cleared on every new network load.
        self._last_report_json: Optional[str] = None
        # Persisted LF parameter overrides — set by the LFParametersDialog
        # and forwarded by :meth:`run_loadflow`. Empty dicts mean
        # "use pypowsybl's defaults" and are the initial state.
        self.lf_generic_params: dict = {}
        self.lf_provider_params: dict = {}
        # Persisted import-side overrides — set by the LoadOptionsDialog
        # and threaded through :meth:`load_network_from_path` so the
        # next file load applies them. ``import_format`` is the explicit
        # format override (``None`` means "auto-detect from extension").
        self.import_format: Optional[str] = None
        self.import_params: dict = {}
        self.import_post_processors: list = []
        # One ChangeLog per process. Reset on every network reload so
        # entries don't leak between unrelated networks.
        self.change_log = ChangeLog()

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

    def load_network_from_path(self, path: str) -> NetworkProxy:
        """Load a network and auto-select the highest-nominal-V VL.

        Both the load and the default-VL pick run on the pypowsybl
        worker thread via :mod:`iidm_viewer.network_loader`. The
        ``import_params`` / ``import_post_processors`` AppState fields
        (set by the LoadOptionsDialog) are threaded through so the
        next load applies them automatically.
        """
        network = network_loader.load_from_path(
            path,
            parameters=self.import_params or None,
            post_processors=self.import_post_processors or None,
        )
        default_vl = network_loader.pick_default_vl(network)
        self._network = network
        self._selected_vl = None  # cleared first so set_selected_vl emits below
        self._last_report_json = None
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

    def notify_network_changed(self) -> None:
        """Re-broadcast the *same* network as if it had been freshly
        loaded.

        Used after irreversible in-place mutations (e.g. network
        reduction) so the sidebar's VL picker, the diagram tabs and
        the data explorer all refresh against the new topology
        without having to round-trip through ``load_network_from_path``.
        Mirrors the cache-flush effect Streamlit gets from
        ``invalidate_on_network_replace``.
        """
        network = self._network
        if network is None:
            return
        # Reset the selected VL + the cached LF report — neither
        # survives an arbitrary topology change.
        self._selected_vl = None
        self._last_report_json = None
        self.change_log.clear()
        # Pick a fresh default-VL (highest nominal V) on the reduced
        # network and broadcast the change to every listener.
        default_vl = network_loader.pick_default_vl(network)
        self.network_changed.emit(network)
        if default_vl:
            self.set_selected_vl(default_vl)

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
        # Fall back to the AppState-cached parameters (set by the
        # "LF Parameters" dialog) when the caller doesn't override.
        if generic_params is None:
            generic_params = self.lf_generic_params or None
        if provider_params is None:
            provider_params = self.lf_provider_params or None
        result = run_ac(self._network, generic_params, provider_params)
        self._last_report_json = getattr(result, "report_json", None)
        self.loadflow_completed.emit(result)
        return result
