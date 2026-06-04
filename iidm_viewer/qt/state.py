"""Framework-agnostic UI state for the PySide6 prototype.

Wraps :class:`iidm_viewer.app_state.AppState` with PySide6 ``Signal`` /
``QObject`` machinery so existing connect-style listeners
(``state.network_changed.connect(...)``) keep working. The shared
network-load / load-flow / cache lifecycle lives in the base class so
the three hosts behave the same.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal

# Re-imported at this scope so existing tests can monkeypatch
# ``iidm_viewer.qt.state.run_ac`` and
# ``iidm_viewer.qt.state.network_loader.*`` against this module's
# globals. ``network_loader`` is a module so attribute patches apply
# everywhere; ``run_ac`` is a function reference, so the subclass's
# ``_run_ac`` override below reads it through this module's namespace.
from iidm_viewer import network_loader  # noqa: F401  (test patch target)
from iidm_viewer.app_state import AppState as _BaseAppState
from iidm_viewer.loadflow import LoadFlowResult, run_ac
from iidm_viewer.powsybl_worker import NetworkProxy


class AppState(_BaseAppState, QObject):
    """Single source of truth for the open network and selected VL."""

    network_changed = Signal(object)        # NetworkProxy | None
    selected_vl_changed = Signal(str)       # vl_id (empty string when cleared)
    # Emitted after each AC load-flow run. Carries the wrapper from
    # :class:`iidm_viewer.loadflow.LoadFlowResult` — the host can
    # inspect ``.status`` / ``.converged`` for the UI status routing,
    # and stash ``.report_json`` for an optional logs dialog.
    loadflow_completed = Signal(object)

    def __init__(self, parent=None) -> None:
        # Explicit two-step init: QObject needs ``parent``, the base
        # ``AppState`` needs no args. ``super().__init__()`` over a
        # mixed QObject + plain class hierarchy is fragile.
        QObject.__init__(self, parent)
        _BaseAppState.__init__(self)

    # ------------------------------------------------------------------
    # Notification hooks — emit Qt signals instead of the base class's
    # listener callbacks. Existing code uses ``Signal.connect``, so we
    # don't fire the listener registry to avoid double-emit if a caller
    # also registered via ``on_*_changed``.
    # ------------------------------------------------------------------
    def _emit_network_changed(self, network: Optional[NetworkProxy]) -> None:
        self.network_changed.emit(network)

    def _emit_selected_vl_changed(self, vl_id: Optional[str]) -> None:
        # Qt's existing contract emits an empty string when cleared
        # (the signal is typed ``Signal(str)``, not ``Signal(object)``).
        self.selected_vl_changed.emit(vl_id or "")

    def _emit_loadflow_completed(self, result: LoadFlowResult) -> None:
        self.loadflow_completed.emit(result)

    # ------------------------------------------------------------------
    # Run AC LF through this module's ``run_ac`` binding so existing
    # ``monkeypatch.setattr("iidm_viewer.qt.state.run_ac", ...)`` calls
    # still intercept the call.
    # ------------------------------------------------------------------
    def _run_ac(self, network, generic_params, provider_params) -> LoadFlowResult:
        return run_ac(network, generic_params, provider_params)
