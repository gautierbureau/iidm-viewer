"""Framework-agnostic AC load-flow helpers.

Hosts the worker-routed ``run_ac`` call, the provider-parameters
descriptor fetch, and the schema of generic pypowsybl LF parameters
(name, type, default, description, options). The Streamlit
``state.run_loadflow`` and ``lf_parameters._GENERIC_PARAMS`` both
delegate here; the PySide6 + NiceGUI prototypes' Run-Load-Flow
buttons call straight into :func:`run_ac`.

No streamlit / Qt / NiceGUI imports.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from iidm_viewer.powsybl_worker import NetworkProxy, run


# Generic load-flow parameter schema. Each tuple is
# ``(name, type, default, description, options?)`` where ``type``
# is one of ``"bool"`` / ``"enum"`` / ``"float"``. Streamlit reads
# this to render its parameter dialog; the prototypes can do the
# same when they grow one.
GENERIC_PARAMETERS: list[tuple] = [
    ("voltage_init_mode", "enum", "UNIFORM_VALUES",
     "Voltage initialization mode",
     ["UNIFORM_VALUES", "PREVIOUS_VALUES", "DC_VALUES"]),
    ("transformer_voltage_control_on", "bool", False,
     "Enable transformer voltage control"),
    ("phase_shifter_regulation_on", "bool", False,
     "Enable phase-shifter regulation"),
    ("use_reactive_limits", "bool", True,
     "Use generator reactive limits"),
    ("shunt_compensator_voltage_control_on", "bool", False,
     "Enable shunt compensator voltage control"),
    ("distributed_slack", "bool", True,
     "Distribute slack on generators"),
    ("balance_type", "enum", "PROPORTIONAL_TO_GENERATION_P_MAX",
     "Active power balance type",
     ["PROPORTIONAL_TO_GENERATION_P_MAX", "PROPORTIONAL_TO_GENERATION_P",
      "PROPORTIONAL_TO_GENERATION_REMAINING_MARGIN",
      "PROPORTIONAL_TO_GENERATION_PARTICIPATION_FACTOR",
      "PROPORTIONAL_TO_LOAD", "PROPORTIONAL_TO_CONFORM_LOAD"]),
    ("dc_use_transformer_ratio", "bool", True,
     "Use transformer ratio in DC mode"),
    ("hvdc_ac_emulation", "bool", True,
     "Enable HVDC AC emulation"),
    ("read_slack_bus", "bool", True,
     "Read slack bus from network"),
    ("write_slack_bus", "bool", True,
     "Write slack bus to network"),
    ("dc_power_factor", "float", 1.0,
     "Power factor for DC load flow"),
]


# ---------------------------------------------------------------------------
# Result wrapper
# ---------------------------------------------------------------------------
class LoadFlowResult:
    """Lightweight wrapper around a pypowsybl AC load-flow run.

    Holds the raw ``LoadFlowResult`` list, the report-node JSON, and
    a convenience ``.status`` string. Hosts can stash the JSON for an
    optional "View Logs" dialog and inspect ``.converged`` for a
    quick success/warning routing.
    """

    __slots__ = ("results", "report_json", "status", "converged")

    def __init__(self, results, report_json: str) -> None:
        self.results = results
        self.report_json = report_json
        try:
            self.status = results[0].status.name if results else "UNKNOWN"
        except Exception:
            self.status = "UNKNOWN"
        self.converged = self.status == "CONVERGED"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LoadFlowResult(status={self.status!r}, n={len(self.results) if self.results else 0})"


# ---------------------------------------------------------------------------
# Provider parameters
# ---------------------------------------------------------------------------
def get_provider_parameters_df() -> pd.DataFrame:
    """Return pypowsybl's per-provider LF parameters descriptor table.

    Worker-thread bound. Each host can cache as it sees fit
    (Streamlit uses session_state; the prototypes generally don't —
    a single LF dialog open per session is cheap).
    """
    def _do():
        import pypowsybl.loadflow as lf
        return lf.get_provider_parameters()

    return run(_do)


# ---------------------------------------------------------------------------
# Run AC LF
# ---------------------------------------------------------------------------
def run_ac(
    network: NetworkProxy,
    generic_params: Optional[dict[str, Any]] = None,
    provider_params: Optional[dict[str, Any]] = None,
) -> LoadFlowResult:
    """Run an AC load flow on the pypowsybl worker thread.

    ``generic_params`` is a dict of overrides for the
    :data:`GENERIC_PARAMETERS` keys; unset keys keep pypowsybl's
    defaults. ``provider_params`` is similarly forwarded to
    ``lf.Parameters.provider_parameters``.

    Returns a :class:`LoadFlowResult` carrying the raw
    pypowsybl result list, the report-node JSON (for an optional
    logs dialog), and a ``status`` / ``converged`` shortcut.
    """
    raw = object.__getattribute__(network, "_obj")
    generic = generic_params or {}
    provider = provider_params or {}

    def _do():
        import pypowsybl.loadflow as lf
        import pypowsybl.report as r
        params = lf.Parameters(**generic)
        if provider:
            params.provider_parameters = {k: str(v) for k, v in provider.items()}
        rn = r.ReportNode(task_key="loadFlowTask", default_name="Load Flow")
        results = lf.run_ac(raw, parameters=params, report_node=rn)
        # Extract the JSON inside the worker so the ReportNode handle
        # doesn't escape to the calling thread.
        return results, rn.to_json()

    results, report_json = run(_do)
    return LoadFlowResult(results, report_json)
