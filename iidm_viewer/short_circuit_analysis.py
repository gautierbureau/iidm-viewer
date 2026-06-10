"""Framework-agnostic core for the Short Circuit Analysis tab.

This module owns the pypowsybl integration each UI host (Streamlit
``short_circuit_analysis_tab``, PySide6, NiceGUI) composes into its
own widget tree. No streamlit / Qt / NiceGUI imports here — the
Streamlit rendering + per-session caching live in
:mod:`iidm_viewer.short_circuit_analysis_tab`.

Public API:

* Vocabulary constants (``FAULT_TYPES``, ``STUDY_TYPES``,
  ``DEFAULT_HV_FLOOR_KV``) shared across hosts.
* :func:`get_nominal_voltages` — worker-routed pypowsybl fetch. No
  caching here; hosts wrap with their own.
* :func:`build_bus_faults` — bus-fault list builder.
* :func:`default_sc_params` / :func:`make_sc_params` — parameter dict
  helpers so every host produces the same shape.
* :func:`run_short_circuit_analysis` — the AC short-circuit runner
  with a serialised result dict the prototypes consume.
* :func:`build_summary_dataframe` — pure DataFrame builder that
  flattens the result dict into the per-fault summary every host
  renders.
* :func:`count_failures` / :func:`count_with_violations` — the two
  metrics the result view header shows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from iidm_viewer import script_recorder
from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Vocabulary constants
# ---------------------------------------------------------------------------
FAULT_TYPES: list[str] = ["THREE_PHASE", "SINGLE_PHASE_TO_GROUND"]
STUDY_TYPES: list[str] = ["SUB_TRANSIENT", "TRANSIENT"]
# The Streamlit tab pre-selects every nominal voltage at or above this
# floor — anything below is rarely interesting for a fault study.
DEFAULT_HV_FLOOR_KV: float = 380.0


def format_fault_type(value: str) -> str:
    """Human label for a fault-type code (shared across UI hosts)."""
    if value == "THREE_PHASE":
        return "3-phase (THREE_PHASE)"
    if value == "SINGLE_PHASE_TO_GROUND":
        return "Single-phase to ground"
    return value


# ---------------------------------------------------------------------------
# Worker-routed pypowsybl fetchers
# ---------------------------------------------------------------------------
def get_nominal_voltages(network: NetworkProxy) -> list[float]:
    """Return the sorted set of nominal voltages present in the network.

    Worker-routed; no caching here. Hosts that need it (Streamlit) wrap
    with their own session-state cache.
    """
    raw = object.__getattribute__(network, "_obj")

    def _fetch() -> list[float]:
        try:
            df = raw.get_voltage_levels(attributes=["nominal_v"])
            return sorted(df["nominal_v"].dropna().unique().tolist())
        except Exception:
            return []

    return run(_fetch)


def default_hv_preselect(voltages: list[float]) -> list[float]:
    """Return the voltages from *voltages* at or above the HV floor."""
    return [v for v in voltages if v >= DEFAULT_HV_FLOOR_KV]


# ---------------------------------------------------------------------------
# Fault list builder
# ---------------------------------------------------------------------------
def build_bus_faults(
    network: NetworkProxy,
    nominal_v_set: Optional[set] = None,
    fault_type: str = "THREE_PHASE",
) -> list[dict]:
    """Build a bus-fault definition for every bus, optionally filtered by
    nominal voltage.

    Both the bus table and the VL table are fetched in a single worker
    call. Returns ``[{"id": "SC_<bus_id>", "element_id": bus_id,
    "fault_type": fault_type}]``.
    """
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        buses = raw.get_buses(attributes=["voltage_level_id"])
        vl_df = (
            raw.get_voltage_levels(attributes=["nominal_v"])
            if nominal_v_set else None
        )
        return buses, vl_df

    buses, vl_df = run(_gather)

    if buses.empty:
        return []

    if nominal_v_set and vl_df is not None and not vl_df.empty:
        def _matches(row):
            vl_id = row.get("voltage_level_id")
            if vl_id and vl_id in vl_df.index:
                return vl_df.at[vl_id, "nominal_v"] in nominal_v_set
            return False
        buses = buses[buses.apply(_matches, axis=1)]

    return [
        {"id": f"SC_{bid}", "element_id": bid, "fault_type": fault_type}
        for bid in buses.index
    ]


# ---------------------------------------------------------------------------
# Parameter helpers
# ---------------------------------------------------------------------------
def default_sc_params() -> dict:
    """Return the parameter dict every host falls back to."""
    return {
        "study_type": "SUB_TRANSIENT",
        "with_feeder_result": True,
        "with_limit_violations": True,
        "min_voltage_drop_proportional_threshold": 0.0,
    }


def make_sc_params(
    study_type: str = "SUB_TRANSIENT",
    with_feeder_result: bool = True,
    with_limit_violations: bool = True,
    min_voltage_drop_percent: float = 0.0,
) -> dict:
    """Build a parameter dict from UI-friendly values.

    ``min_voltage_drop_percent`` is the human-facing 0–100 % value
    coming off a number input; the runner expects a 0–1 ratio.
    """
    return {
        "study_type": study_type,
        "with_feeder_result": bool(with_feeder_result),
        "with_limit_violations": bool(with_limit_violations),
        "min_voltage_drop_proportional_threshold": float(
            min_voltage_drop_percent
        ) / 100.0,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_short_circuit_analysis(
    network: NetworkProxy,
    faults: list[dict],
    sc_params: Optional[dict] = None,
) -> dict:
    """Run short circuit analysis on the worker thread.

    *faults* is a list of ``{"id": str, "element_id": bus_id,
    "fault_type": str}`` dicts produced by :func:`build_bus_faults`
    (or any compatible builder).

    *sc_params* is a plain dict of scalar options read from the main
    thread::

        {
            "study_type": "SUB_TRANSIENT" | "TRANSIENT",
            "with_feeder_result": bool,
            "with_limit_violations": bool,
            "min_voltage_drop_proportional_threshold": float,
        }

    Returns a serialised dict safe for cross-thread / session-state
    storage::

        {
            "fault_results": {fault_id: {
                "status": str,
                "short_circuit_power_mva": float | None,
                "current_kA": float | None,
                "feeder_results": DataFrame,
                "limit_violations": DataFrame,
            }},
            "faults": list[dict],
        }
    """
    raw = object.__getattribute__(network, "_obj")
    sc_params = sc_params or {}

    def _run_sc():
        import pypowsybl.shortcircuit as sc

        analysis = sc.create_analysis()
        for f in faults:
            analysis.set_bus_fault(f["id"], f["element_id"], 0.0, 0.0)

        params = sc.Parameters(
            study_type=sc.ShortCircuitStudyType.__members__.get(
                sc_params.get("study_type", "SUB_TRANSIENT"),
                sc.ShortCircuitStudyType.SUB_TRANSIENT,
            ),
            with_feeder_result=sc_params.get("with_feeder_result", True),
            with_limit_violations=sc_params.get("with_limit_violations", True),
            min_voltage_drop_proportional_threshold=float(
                sc_params.get("min_voltage_drop_proportional_threshold", 0.0)
            ),
        )

        result = analysis.run(raw, parameters=params)

        # Serialise all results before they leave the worker thread.
        fr_df = result.fault_results          # DataFrame indexed by fault_id
        feeder_df_all = result.feeder_results  # may be multi-indexed
        viol_df_all = result.limit_violations  # may be multi-indexed

        def _filter_by_fault(df: pd.DataFrame, fid: str) -> pd.DataFrame:
            if df.empty:
                return pd.DataFrame()
            try:
                if isinstance(df.index, pd.MultiIndex):
                    lvl_vals = df.index.get_level_values(0)
                    return df[lvl_vals == fid].reset_index(drop=True)
                return df[df.index == fid].reset_index(drop=True)
            except Exception:
                return pd.DataFrame()

        fault_results: dict = {}
        for f in faults:
            fid = f["id"]
            if fid in fr_df.index:
                row = fr_df.loc[fid]
                status_val = row.get("status", "UNKNOWN")
                status_str = (
                    status_val.name
                    if hasattr(status_val, "name")
                    else str(status_val)
                )
                pwr_raw = row.get("short_circuit_power", None)
                pwr = (
                    float(pwr_raw)
                    if pwr_raw is not None and pd.notna(pwr_raw)
                    else None
                )
                cur_raw = row.get("current", None)
                cur_a = (
                    float(cur_raw)
                    if cur_raw is not None and pd.notna(cur_raw)
                    else None
                )
                cur_ka = cur_a / 1000.0 if cur_a is not None else None
            else:
                status_str = "UNKNOWN"
                pwr = None
                cur_ka = None

            fault_results[fid] = {
                "status": status_str,
                "short_circuit_power_mva": pwr,
                "current_kA": cur_ka,
                "feeder_results": _filter_by_fault(feeder_df_all, fid),
                "limit_violations": _filter_by_fault(viol_df_all, fid),
            }

        return {
            "fault_results": fault_results,
            "faults": faults,
        }

    sc_result = run(_run_sc)
    script_recorder.record_run_short_circuit_analysis(faults, sc_params)
    return sc_result


# ---------------------------------------------------------------------------
# Pure summary helpers (used by every host's results view)
# ---------------------------------------------------------------------------
SUMMARY_COLUMNS: list[str] = [
    "Fault",
    "Bus",
    "Status",
    "Fault power (MVA)",
    "Fault current (kA)",
    "Violations",
]


def build_summary_dataframe(results: dict) -> pd.DataFrame:
    """Flatten *results* into the per-fault summary every host renders.

    Columns: ``Fault``, ``Bus``, ``Status``, ``Fault power (MVA)``,
    ``Fault current (kA)``, ``Violations``. Empty results yield an
    empty DataFrame with the canonical columns so downstream filters /
    table widgets can rely on the schema.
    """
    faults: list[dict] = results.get("faults", []) if results else []
    fault_results: dict = results.get("fault_results", {}) if results else {}
    rows = []
    for f in faults:
        fid = f["id"]
        fr = fault_results.get(fid, {})
        viol_df: pd.DataFrame = fr.get("limit_violations", pd.DataFrame())
        pwr = fr.get("short_circuit_power_mva")
        cur = fr.get("current_kA")
        rows.append({
            "Fault": fid,
            "Bus": f["element_id"],
            "Status": fr.get("status", "UNKNOWN"),
            "Fault power (MVA)": round(pwr, 1) if pwr is not None else None,
            "Fault current (kA)": round(cur, 3) if cur is not None else None,
            "Violations": 0 if viol_df.empty else len(viol_df),
        })
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)


def count_failures(summary_df: pd.DataFrame) -> int:
    """Number of faults whose status is anything but ``CONVERGED``."""
    if summary_df.empty:
        return 0
    return int((summary_df["Status"] != "CONVERGED").sum())


def count_with_violations(summary_df: pd.DataFrame) -> int:
    """Number of faults that produced at least one limit violation."""
    if summary_df.empty:
        return 0
    return int((summary_df["Violations"] > 0).sum())


def max_fault_power_mva(summary_df: pd.DataFrame) -> float:
    """Max ``Fault power (MVA)`` in *summary_df*, ``0.0`` if empty / all-NaN."""
    if summary_df.empty:
        return 0.0
    col = summary_df["Fault power (MVA)"].dropna()
    return float(col.max()) if not col.empty else 0.0


# ---------------------------------------------------------------------------
# View-model — host-agnostic state container for the Short Circuit tab
# ---------------------------------------------------------------------------
@dataclass
class ShortCircuitViewModel:
    """Mutable state container for the Short Circuit Analysis tab.

    All three hosts (Streamlit, PySide6, NiceGUI) carry the same two
    pieces of state — the bus-fault list and the last run's result
    dict; this dataclass captures both and exposes the derived
    summary / metric helpers so the per-host code reads off the
    view-model rather than re-deriving them from the raw results.

    Like :class:`SecurityAnalysisViewModel`, hosts hold one instance
    per session and route every state change (set_faults,
    store_results, clear) through it so the cross-host behaviour
    stays in lockstep.
    """

    faults: list[dict] = field(default_factory=list)
    results: Optional[dict] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Reset faults + results — call on network swap / reduction."""
        self.faults.clear()
        self.results = None

    def clear_results(self) -> None:
        """Reset only the results (e.g. when the user reconfigures
        before re-running)."""
        self.results = None

    # ------------------------------------------------------------------
    # Faults
    # ------------------------------------------------------------------
    def set_faults(self, faults) -> None:
        """Replace the fault list — the build helpers always emit the
        whole list, so a setter is what every host needs."""
        self.faults = list(faults or [])

    def fault_ids(self) -> list[str]:
        return [f.get("id", "") for f in self.faults]

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    def store_results(self, results: dict) -> None:
        """Store the dict returned by :func:`run_short_circuit_analysis`."""
        self.results = results

    def has_results(self) -> bool:
        return bool(self.results)

    # ------------------------------------------------------------------
    # Derived helpers — wrap the existing summary / metric functions
    # so hosts don't reach for them directly.
    # ------------------------------------------------------------------
    def summary_df(self) -> pd.DataFrame:
        """One row per fault, shaped by :data:`SUMMARY_COLUMNS`. Empty
        when no results, with the canonical schema preserved so
        downstream filters / table widgets stay safe."""
        if not self.results:
            return pd.DataFrame(columns=SUMMARY_COLUMNS)
        return build_summary_dataframe(self.results)

    def failure_count(self) -> int:
        return count_failures(self.summary_df())

    def with_violations_count(self) -> int:
        return count_with_violations(self.summary_df())

    def max_fault_power_mva(self) -> float:
        return max_fault_power_mva(self.summary_df())

    def fault_options(self) -> list[str]:
        """``Fault`` column from the summary — what the host's
        per-fault drill-down combo populates from."""
        df = self.summary_df()
        if df.empty:
            return []
        return list(df["Fault"].astype(str))
