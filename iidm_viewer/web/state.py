"""Framework-agnostic UI state for the NiceGUI prototype.

Thin subclass of :class:`iidm_viewer.app_state.AppState`. The base
class's default listener-callback notification model is exactly what
NiceGUI uses, so this subclass only re-exports the load-flow runner
so existing ``monkeypatch.setattr("iidm_viewer.web.state.run_ac", …)``
calls still intercept LF runs in tests.
"""
from __future__ import annotations

# Re-imported at this scope so test fixtures patching
# ``iidm_viewer.web.state.run_ac`` (and ``iidm_viewer.web.state.network_loader.*``)
# affect what :meth:`AppState._run_ac` actually calls.
from iidm_viewer import network_loader  # noqa: F401  (test patch target)
from iidm_viewer.app_state import (
    AppState as _BaseAppState,
    LoadFlowListener,
    NetworkListener,
    VlListener,
)
from iidm_viewer.loadflow import LoadFlowResult, run_ac


class AppState(_BaseAppState):
    """Single source of truth for the open network and selected VL.

    Re-exported under :mod:`iidm_viewer.web.state` so existing NiceGUI
    code that imports ``iidm_viewer.web.state.AppState`` keeps working.
    """

    def _run_ac(self, network, generic_params, provider_params) -> LoadFlowResult:
        return run_ac(network, generic_params, provider_params)


__all__ = ["AppState", "LoadFlowListener", "NetworkListener", "VlListener"]
