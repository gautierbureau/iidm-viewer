"""Framework-agnostic core for the Security Analysis tab.

This module owns the pypowsybl integration each UI host (Streamlit
``security_analysis_tab``, PySide6, NiceGUI) composes into its own
widget tree. No streamlit / Qt / NiceGUI imports here — the
Streamlit-only rendering + per-session caching live in
:mod:`iidm_viewer.security_analysis_tab`.

Public API:

* Vocabulary constants (``ELEMENT_TYPES``, ``AUTO_MODES``,
  ``MANUAL_TYPES``, ``MANUAL_TYPE_IDS_KEY``, ``MANUAL_GROUPINGS``,
  ``CTX_TYPES``, ``ACTION_TYPES``, ``CONDITION_TYPES``,
  ``VIOLATION_TYPES``, ``SIDES``) shared across hosts.
* :func:`get_nominal_voltages` / :func:`get_element_ids` —
  worker-routed pypowsybl fetchers. No caching here; hosts wrap with
  their own.
* :func:`build_n1_contingencies` / :func:`build_n2_contingencies` —
  contingency-list builders, lifted out of ``state.py``.
* :func:`apply_action` — pypowsybl ``add_*_action`` dispatcher.
* :func:`run_security_analysis` — the big AC SA runner with the
  serialised result dict the Streamlit Results tab consumes.
* :func:`action_summary` — pure one-line description used by every
  host's action-list rendering.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Optional

import pandas as pd

from iidm_viewer import script_recorder
from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Vocabulary constants
# ---------------------------------------------------------------------------
ELEMENT_TYPES: list[str] = ["Lines", "2-Winding Transformers"]
AUTO_MODES: list[str] = ["N-1", "N-2"]
MANUAL_TYPES: list[str] = [
    "Lines",
    "2-Winding Transformers",
    "3-Winding Transformers",
    "Generators",
]
MANUAL_TYPE_IDS_KEY: dict[str, str] = {
    "Lines": "lines",
    "2-Winding Transformers": "two_windings_transformers",
    "3-Winding Transformers": "three_windings_transformers",
    "Generators": "generators",
}
MANUAL_GROUPINGS: list[str] = [
    "One contingency per element (N-1)",
    "Single grouped contingency (N-k)",
]
CTX_TYPES: list[str] = ["ALL", "NONE", "SPECIFIC"]
ACTION_TYPES: list[str] = [
    "SWITCH",
    "TERMINALS_CONNECTION",
    "GENERATOR_ACTIVE_POWER",
    "LOAD_ACTIVE_POWER",
    "PHASE_TAP_CHANGER_POSITION",
    "RATIO_TAP_CHANGER_POSITION",
    "SHUNT_COMPENSATOR_POSITION",
]
CONDITION_TYPES: list[str] = [
    "TRUE_CONDITION",
    "ANY_VIOLATION_CONDITION",
    "ALL_VIOLATION_CONDITION",
    "AT_LEAST_ONE_VIOLATION_CONDITION",
]
VIOLATION_TYPES: list[str] = [
    "CURRENT",
    "ACTIVE_POWER",
    "APPARENT_POWER",
    "LOW_VOLTAGE",
    "HIGH_VOLTAGE",
]
SIDES: list[str] = ["NONE", "ONE", "TWO"]


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


def get_element_ids(network: NetworkProxy) -> dict[str, list[str]]:
    """Fetch every element-id list the Security Analysis config needs.

    One worker hop returns all the categories the configuration UI
    needs to populate its dropdowns:

    * ``branches``: lines + 2-winding transformers
    * ``lines`` / ``two_windings_transformers`` /
      ``three_windings_transformers``
    * ``voltage_levels`` / ``switches`` / ``generators`` /
      ``loads`` / ``shunt_compensators``
    * ``phase_tap_changers`` / ``ratio_tap_changers``: transformers
      that carry the matching tap changer
    * ``connectables``: lines + 2WTs + generators (the most common
      terminals-connection action targets)
    """
    raw = object.__getattribute__(network, "_obj")

    def _gather() -> dict[str, list[str]]:
        lines = list(raw.get_lines(attributes=[]).index)
        t2w = list(raw.get_2_windings_transformers(attributes=[]).index)
        t3w = list(raw.get_3_windings_transformers(attributes=[]).index)
        vls = list(raw.get_voltage_levels(attributes=[]).index)
        switches = list(raw.get_switches(attributes=[]).index)
        gens = list(raw.get_generators(attributes=[]).index)
        loads = list(raw.get_loads(attributes=[]).index)
        shunts = list(raw.get_shunt_compensators(attributes=[]).index)
        ptc_df = raw.get_phase_tap_changers(attributes=[])
        ptc_ids = sorted(set(ptc_df.index)) if not ptc_df.empty else []
        rtc_df = raw.get_ratio_tap_changers(attributes=[])
        rtc_ids = sorted(set(rtc_df.index)) if not rtc_df.empty else []
        return {
            "branches": sorted(lines + t2w),
            "lines": sorted(lines),
            "two_windings_transformers": sorted(t2w),
            "three_windings_transformers": sorted(t3w),
            "voltage_levels": sorted(vls),
            "switches": sorted(switches),
            "generators": sorted(gens),
            "loads": sorted(loads),
            "shunt_compensators": sorted(shunts),
            "phase_tap_changers": ptc_ids,
            "ratio_tap_changers": rtc_ids,
            "connectables": sorted(lines + t2w + gens),
        }

    return run(_gather)


# ---------------------------------------------------------------------------
# Contingency builders
# ---------------------------------------------------------------------------
def build_n1_contingencies(
    network: NetworkProxy,
    element_type: str,
    nominal_v_set: Optional[set] = None,
) -> list[dict]:
    """Build N-1 contingency definitions for every element of ``element_type``.

    When ``nominal_v_set`` is provided, only elements whose terminal
    voltage levels carry a nominal voltage in the set are kept. Returns
    ``[{"id": "N1_<element_id>", "element_id": eid, "element_ids": [eid]}]``.
    """
    if element_type == "Lines":
        getter = "get_lines"
    elif element_type == "2-Winding Transformers":
        getter = "get_2_windings_transformers"
    else:
        return []

    vl_cols = ["voltage_level1_id", "voltage_level2_id"]
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        elem_df = getattr(raw, getter)(attributes=vl_cols)
        vl_df = (
            raw.get_voltage_levels(attributes=["nominal_v"])
            if nominal_v_set else None
        )
        return elem_df, vl_df

    elem_df, vl_df = run(_gather)
    if elem_df.empty:
        return []

    if nominal_v_set and vl_df is not None and not vl_df.empty:
        def _matches(row):
            for col in vl_cols:
                vl_id = row.get(col)
                if vl_id and vl_id in vl_df.index:
                    if vl_df.at[vl_id, "nominal_v"] in nominal_v_set:
                        return True
            return False

        elem_df = elem_df[elem_df.apply(_matches, axis=1)]

    return [
        {"id": f"N1_{eid}", "element_id": eid, "element_ids": [eid]}
        for eid in elem_df.index
    ]


# ---------------------------------------------------------------------------
# Manual contingency normalizer (host-agnostic — shared by the Streamlit
# Security Analysis tab and the N-K variant picker docks)
# ---------------------------------------------------------------------------
PER_ELEMENT_GROUPING = "per_element"  # one N-1 contingency per element
SINGLE_GROUPING = "single"            # one N-k contingency for the whole selection

# Mapping from the Streamlit radio labels (:data:`MANUAL_GROUPINGS`) to
# the canonical tokens above. Exposed so each host can offer its own
# widget labels while still funnelling through the normalizer.
MANUAL_GROUPING_TOKENS: dict[str, str] = {
    MANUAL_GROUPINGS[0]: PER_ELEMENT_GROUPING,
    MANUAL_GROUPINGS[1]: SINGLE_GROUPING,
}


def validate_manual_contingency(
    element_ids,
    grouping: str,
    group_id: Optional[str],
) -> list[str]:
    """Return a list of error strings for a manual-picker selection.

    Empty list ⇒ the selection is well-formed. The host decides how to
    surface the errors (toast / status label / inline caption).
    """
    errors: list[str] = []
    if not element_ids:
        errors.append("Pick at least one element.")
    if grouping not in (PER_ELEMENT_GROUPING, SINGLE_GROUPING):
        errors.append(f"Unknown grouping: {grouping!r}")
    elif grouping == SINGLE_GROUPING and not (group_id or "").strip():
        errors.append("A contingency id is required for grouped mode.")
    return errors


def normalize_manual_contingency(
    element_type: str,
    element_ids,
    grouping: str,
    group_id: Optional[str],
) -> list[dict]:
    """Translate a manual picker selection into the canonical
    contingency shape.

    ``element_type`` is one of :data:`MANUAL_TYPES` and is currently
    surfaced only for symmetry with future split-by-type validations —
    every entry uses pypowsybl's id-based contingency model so
    ``element_type`` isn't part of the output dict.

    Returns:

    * ``grouping == "per_element"`` →
      ``[{"id": "N1_<eid>", "element_id": eid, "element_ids": [eid]}, …]``
      (one entry per element, identical shape to
      :func:`build_n1_contingencies` so downstream readers can pull
      either source without branching).
    * ``grouping == "single"`` →
      ``[{"id": <group_id>, "element_ids": [...]}]`` — one grouped
      contingency carrying every selected id.

    Raises :class:`ValueError` (joined errors from
    :func:`validate_manual_contingency`) on invalid input.
    """
    del element_type  # currently unused; kept for forward compatibility
    errors = validate_manual_contingency(element_ids, grouping, group_id)
    if errors:
        raise ValueError("; ".join(errors))
    if grouping == PER_ELEMENT_GROUPING:
        return [
            {"id": f"N1_{eid}", "element_id": eid, "element_ids": [eid]}
            for eid in element_ids
        ]
    cid = (group_id or "").strip()
    return [{"id": cid, "element_ids": list(element_ids)}]


def build_n2_contingencies(
    network: NetworkProxy,
    element_type: str,
    nominal_v_set: Optional[set] = None,
) -> list[dict]:
    """Build N-2 contingency definitions for every unique pair of elements.

    Pairs are unordered ``(A, B)`` with ``A < B`` by element id. Returns
    ``[{"id": "N2_<a>_<b>", "element_ids": [a, b]}]``.
    """
    n1 = build_n1_contingencies(network, element_type, nominal_v_set)
    ids = sorted(c["element_id"] for c in n1)
    return [
        {"id": f"N2_{a}_{b}", "element_ids": [a, b]}
        for a, b in combinations(ids, 2)
    ]


# ---------------------------------------------------------------------------
# Action dispatcher
# ---------------------------------------------------------------------------
def apply_action(analysis, action: dict) -> None:
    """Dispatch a single action dict to the right pypowsybl ``add_*_action`` call.

    Supported action types (extend here to add more):

    - ``SWITCH``: ``switch_id``, ``open``
    - ``TERMINALS_CONNECTION``: ``element_id``, ``opening``, optional ``side``
    - ``GENERATOR_ACTIVE_POWER``: ``generator_id``, ``is_relative``, ``active_power``
    - ``LOAD_ACTIVE_POWER``: ``load_id``, ``is_relative``, ``active_power``
    - ``PHASE_TAP_CHANGER_POSITION``: ``transformer_id``, ``is_relative``,
      ``tap_position``, optional ``side``
    - ``RATIO_TAP_CHANGER_POSITION``: ``transformer_id``, ``is_relative``,
      ``tap_position``, optional ``side``
    - ``SHUNT_COMPENSATOR_POSITION``: ``shunt_id``, ``section``
    """
    from pypowsybl._pypowsybl import Side

    action_id = action["action_id"]
    atype = action["type"]
    side = Side.__members__.get(action.get("side", "NONE"), Side.NONE)

    if atype == "SWITCH":
        analysis.add_switch_action(
            action_id, action["switch_id"], bool(action["open"])
        )
    elif atype == "TERMINALS_CONNECTION":
        analysis.add_terminals_connection_action(
            action_id,
            action["element_id"],
            side=side,
            opening=bool(action.get("opening", True)),
        )
    elif atype == "GENERATOR_ACTIVE_POWER":
        analysis.add_generator_active_power_action(
            action_id,
            action["generator_id"],
            bool(action["is_relative"]),
            float(action["active_power"]),
        )
    elif atype == "LOAD_ACTIVE_POWER":
        analysis.add_load_active_power_action(
            action_id,
            action["load_id"],
            bool(action["is_relative"]),
            float(action["active_power"]),
        )
    elif atype == "PHASE_TAP_CHANGER_POSITION":
        analysis.add_phase_tap_changer_position_action(
            action_id,
            action["transformer_id"],
            bool(action["is_relative"]),
            int(action["tap_position"]),
            side=side,
        )
    elif atype == "RATIO_TAP_CHANGER_POSITION":
        analysis.add_ratio_tap_changer_position_action(
            action_id,
            action["transformer_id"],
            bool(action["is_relative"]),
            int(action["tap_position"]),
            side=side,
        )
    elif atype == "SHUNT_COMPENSATOR_POSITION":
        analysis.add_shunt_compensator_position_action(
            action_id,
            action["shunt_id"],
            int(action["section"]),
        )
    else:
        raise ValueError(f"Unsupported action type: {atype!r}")


def action_summary(action: dict) -> str:
    """One-line human description of an action dict.

    Pure helper so every host renders the same caption in its action
    list (Streamlit markdown, NiceGUI label, Qt ``QLabel``).
    """
    atype = action["type"]
    aid = action["action_id"]
    if atype == "SWITCH":
        verb = "open" if action.get("open") else "close"
        return f"`{aid}` — **SWITCH** {verb} `{action['switch_id']}`"
    if atype == "TERMINALS_CONNECTION":
        verb = "open" if action.get("opening", True) else "close"
        side = action.get("side", "NONE")
        extra = "" if side == "NONE" else f" (side {side})"
        return f"`{aid}` — **TERMINALS** {verb} `{action['element_id']}`{extra}"
    if atype == "GENERATOR_ACTIVE_POWER":
        rel = "Δ" if action.get("is_relative") else "="
        return (
            f"`{aid}` — **GEN P** `{action['generator_id']}` "
            f"{rel}{action['active_power']:g} MW"
        )
    if atype == "LOAD_ACTIVE_POWER":
        rel = "Δ" if action.get("is_relative") else "="
        return (
            f"`{aid}` — **LOAD P** `{action['load_id']}` "
            f"{rel}{action['active_power']:g} MW"
        )
    if atype == "PHASE_TAP_CHANGER_POSITION":
        rel = "Δ" if action.get("is_relative") else "="
        return (
            f"`{aid}` — **PTC** `{action['transformer_id']}` "
            f"{rel}{action['tap_position']}"
        )
    if atype == "RATIO_TAP_CHANGER_POSITION":
        rel = "Δ" if action.get("is_relative") else "="
        return (
            f"`{aid}` — **RTC** `{action['transformer_id']}` "
            f"{rel}{action['tap_position']}"
        )
    if atype == "SHUNT_COMPENSATOR_POSITION":
        return (
            f"`{aid}` — **SHUNT** `{action['shunt_id']}` "
            f"section={action['section']}"
        )
    return f"`{aid}` — **{atype}**"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_security_analysis(
    network: NetworkProxy,
    contingencies: list[dict],
    monitored_elements: Optional[list[dict]] = None,
    limit_reductions: Optional[list[dict]] = None,
    actions: Optional[list[dict]] = None,
    operator_strategies: Optional[list[dict]] = None,
    contingencies_json_paths: Optional[list[str]] = None,
    actions_json_paths: Optional[list[str]] = None,
    operator_strategies_json_paths: Optional[list[str]] = None,
) -> dict:
    """Run AC security analysis on the worker thread.

    The signature is the same as the legacy ``state.run_security_analysis``
    (which now re-exports this); see that docstring for the full
    contract. Returns a serialized dict the Streamlit / Qt / NiceGUI
    tabs can stash in their per-host state container.

    Side effect: records the run via :mod:`iidm_viewer.script_recorder`
    so the Session Script dialog can replay it.
    """
    from iidm_viewer.lf_parameters import get_lf_parameters

    raw = object.__getattribute__(network, "_obj")
    generic, provider = get_lf_parameters()
    monitored_elements = monitored_elements or []
    limit_reductions = limit_reductions or []
    actions = actions or []
    operator_strategies = operator_strategies or []
    contingencies_json_paths = contingencies_json_paths or []
    actions_json_paths = actions_json_paths or []
    operator_strategies_json_paths = operator_strategies_json_paths or []

    def _run_sa():
        import pypowsybl.security as sa
        import pypowsybl.loadflow as lf
        from pypowsybl.flowdecomposition import ContingencyContextType
        from pypowsybl._pypowsybl import ConditionType, ViolationType

        analysis = sa.create_analysis()
        for c in contingencies:
            eids = list(
                c.get("element_ids")
                or ([c["element_id"]] if "element_id" in c else []),
            )
            if len(eids) == 1:
                analysis.add_single_element_contingency(eids[0], c["id"])
            elif len(eids) > 1:
                analysis.add_multiple_elements_contingency(eids, c["id"])
        for p in contingencies_json_paths:
            analysis.add_contingencies_from_json_file(p)

        for me in monitored_elements:
            ctx_name = me.get("contingency_context_type", "ALL")
            ctx = ContingencyContextType.__members__.get(
                ctx_name, ContingencyContextType.ALL,
            )
            analysis.add_monitored_elements(
                contingency_context_type=ctx,
                contingency_ids=me.get("contingency_ids") or None,
                branch_ids=me.get("branch_ids") or None,
                voltage_level_ids=me.get("voltage_level_ids") or None,
                three_windings_transformer_ids=me.get(
                    "three_windings_transformer_ids",
                ) or None,
            )

        if limit_reductions:
            lr_df = pd.DataFrame(limit_reductions).set_index("limit_type")
            analysis.add_limit_reductions(lr_df)

        for action in actions:
            apply_action(analysis, action)
        for p in actions_json_paths:
            analysis.add_actions_from_json_file(p)

        for strat in operator_strategies:
            cond_name = strat.get("condition_type", "TRUE_CONDITION")
            cond = ConditionType.__members__.get(
                cond_name, ConditionType.TRUE_CONDITION,
            )
            vtype_names = strat.get("violation_types") or []
            vtypes = [
                ViolationType.__members__[n]
                for n in vtype_names
                if n in ViolationType.__members__
            ] or None
            vsubjects = list(strat.get("violation_subject_ids") or []) or None
            analysis.add_operator_strategy(
                strat["operator_strategy_id"],
                strat["contingency_id"],
                list(strat["action_ids"]),
                condition_type=cond,
                violation_subject_ids=vsubjects,
                violation_types=vtypes,
            )
        for p in operator_strategies_json_paths:
            analysis.add_operator_strategies_from_json_file(p)

        lf_params = lf.Parameters(**generic)
        if provider:
            lf_params.provider_parameters = {
                k: str(v) for k, v in provider.items()
            }
        params = sa.Parameters(load_flow_parameters=lf_params)

        result = analysis.run_ac(raw, parameters=params)

        # Serialize the native pypowsybl JSON view so the caller can
        # download it after the result object goes out of scope on the
        # worker.
        import os as _os
        import tempfile as _tempfile

        with _tempfile.NamedTemporaryFile(suffix=".json", delete=False) as _tf:
            _json_path = _tf.name
        try:
            result.export_to_json(_json_path)
            with open(_json_path, "rb") as _fh:
                json_export_bytes = _fh.read()
        finally:
            try:
                _os.unlink(_json_path)
            except OSError:
                pass

        pre_result = result.pre_contingency_result
        pre_viol = pd.DataFrame(pre_result.limit_violations)

        def _select(
            df: pd.DataFrame,
            contingency_id: Optional[str],
            strategy_id: str = "",
        ) -> pd.DataFrame:
            """Slice a multi-indexed result DF by
            ``(contingency_id, operator_strategy_id)``.

            Index levels are
            ``(contingency_id, operator_strategy_id, element_id)``.
            ``""`` means "no contingency" for level 0 and "no strategy"
            for level 1. Returns an empty DataFrame if ``df`` is empty
            or the keys are absent.
            """
            if df is None or df.empty:
                return pd.DataFrame()
            try:
                if isinstance(df.index, pd.MultiIndex):
                    cid_key = "" if contingency_id is None else contingency_id
                    lvl0 = df.index.get_level_values(0)
                    mask = lvl0 == cid_key
                    if df.index.nlevels >= 3:
                        lvl1 = df.index.get_level_values(1)
                        mask = mask & (lvl1 == strategy_id)
                        return df[mask].reset_index(level=[0, 1], drop=True)
                    return df[mask].reset_index(level=0, drop=True)
                return df.copy()
            except Exception:
                return pd.DataFrame()

        branch_all = (
            pd.DataFrame(result.branch_results)
            if result.branch_results is not None else pd.DataFrame()
        )
        bus_all = (
            pd.DataFrame(result.bus_results)
            if result.bus_results is not None else pd.DataFrame()
        )
        t3w_all = (
            pd.DataFrame(result.three_windings_transformer_results)
            if result.three_windings_transformer_results is not None
            else pd.DataFrame()
        )

        post: dict = {}
        for cid, cr in result.post_contingency_results.items():
            post[cid] = {
                "status": cr.status.name,
                "limit_violations": pd.DataFrame(cr.limit_violations),
                "branch_results": _select(branch_all, cid),
                "bus_results": _select(bus_all, cid),
                "three_windings_transformer_results": _select(t3w_all, cid),
            }

        os_results: dict = {}
        for sid, osr in result.operator_strategy_results.items():
            strat = next(
                (s for s in operator_strategies
                 if s["operator_strategy_id"] == sid),
                None,
            )
            cid = strat["contingency_id"] if strat else None
            os_results[sid] = {
                "status": osr.status.name,
                "limit_violations": pd.DataFrame(osr.limit_violations),
                "branch_results": _select(branch_all, cid, sid),
                "bus_results": _select(bus_all, cid, sid),
                "three_windings_transformer_results": _select(
                    t3w_all, cid, sid,
                ),
                "contingency_id": cid,
                "action_ids": list(strat["action_ids"]) if strat else [],
            }

        return {
            "pre_status": pre_result.status.name,
            "pre_violations": pre_viol,
            "pre_branch_results": _select(branch_all, None),
            "pre_bus_results": _select(bus_all, None),
            "pre_3wt_results": _select(t3w_all, None),
            "post": post,
            "operator_strategies": os_results,
            "contingencies": contingencies,
            "json_export": json_export_bytes,
        }

    sa_result = run(_run_sa)
    script_recorder.record_run_security_analysis(
        contingencies,
        monitored_elements,
        limit_reductions,
        actions,
        operator_strategies,
        contingencies_json_paths,
        actions_json_paths,
        operator_strategies_json_paths,
        generic,
        provider,
    )
    return sa_result


# ---------------------------------------------------------------------------
# Advanced-configuration builders / validators / summaries
#
# Each host (Streamlit / PySide6 / NiceGUI) renders its own widgets, then
# calls the matching ``make_*`` builder + ``validate_*`` checker so the
# config-dict shape and the validation rules live in one place. The
# ``*_summary`` helpers give every host the same one-line description.
# ---------------------------------------------------------------------------

# Declarative per-action-type field registry. ``id_key`` names the
# :func:`get_element_ids` bucket that feeds the element selector;
# ``fields`` describes the remaining widgets (kinds: ``id`` / ``bool`` /
# ``float`` / ``int`` / ``choice``).
ACTION_FIELDS: dict[str, dict] = {
    "SWITCH": {
        "id_key": "switches",
        "fields": [
            {"name": "switch_id", "label": "Switch", "kind": "id"},
            {"name": "open", "label": "Open switch", "kind": "bool",
             "default": True},
        ],
    },
    "TERMINALS_CONNECTION": {
        "id_key": "connectables",
        "fields": [
            {"name": "element_id", "label": "Element (line / 2WT / generator)",
             "kind": "id"},
            {"name": "side", "label": "Side", "kind": "choice",
             "options": SIDES, "default": "NONE"},
            {"name": "opening", "label": "Open (disconnect)", "kind": "bool",
             "default": True},
        ],
    },
    "GENERATOR_ACTIVE_POWER": {
        "id_key": "generators",
        "fields": [
            {"name": "generator_id", "label": "Generator", "kind": "id"},
            {"name": "is_relative", "label": "Relative change", "kind": "bool",
             "default": True},
            {"name": "active_power", "label": "Active power (MW)",
             "kind": "float", "default": -10.0},
        ],
    },
    "LOAD_ACTIVE_POWER": {
        "id_key": "loads",
        "fields": [
            {"name": "load_id", "label": "Load", "kind": "id"},
            {"name": "is_relative", "label": "Relative change", "kind": "bool",
             "default": True},
            {"name": "active_power", "label": "Active power (MW)",
             "kind": "float", "default": -10.0},
        ],
    },
    "PHASE_TAP_CHANGER_POSITION": {
        "id_key": "phase_tap_changers",
        "fields": [
            {"name": "transformer_id", "label": "Transformer", "kind": "id"},
            {"name": "is_relative", "label": "Relative change", "kind": "bool",
             "default": False},
            {"name": "tap_position", "label": "Tap position", "kind": "int",
             "default": 0},
            {"name": "side", "label": "Side (3WTs only)", "kind": "choice",
             "options": SIDES, "default": "NONE"},
        ],
    },
    "RATIO_TAP_CHANGER_POSITION": {
        "id_key": "ratio_tap_changers",
        "fields": [
            {"name": "transformer_id", "label": "Transformer", "kind": "id"},
            {"name": "is_relative", "label": "Relative change", "kind": "bool",
             "default": False},
            {"name": "tap_position", "label": "Tap position", "kind": "int",
             "default": 0},
            {"name": "side", "label": "Side (3WTs only)", "kind": "choice",
             "options": SIDES, "default": "NONE"},
        ],
    },
    "SHUNT_COMPENSATOR_POSITION": {
        "id_key": "shunt_compensators",
        "fields": [
            {"name": "shunt_id", "label": "Shunt compensator", "kind": "id"},
            {"name": "section", "label": "Section count", "kind": "int",
             "default": 0},
        ],
    },
}


# --- Monitored elements ----------------------------------------------------
def validate_monitored_element(
    ctx_type: str,
    contingency_ids,
    branch_ids,
    voltage_level_ids,
    three_windings_transformer_ids,
) -> list[str]:
    """Validate a monitored-element rule before it goes into the run."""
    errors: list[str] = []
    if not (branch_ids or voltage_level_ids or three_windings_transformer_ids):
        errors.append("Pick at least one branch, voltage level or 3WT.")
    if ctx_type == "SPECIFIC" and not contingency_ids:
        errors.append("Pick at least one contingency for SPECIFIC context.")
    return errors


def make_monitored_element(
    ctx_type: str,
    contingency_ids=None,
    branch_ids=None,
    voltage_level_ids=None,
    three_windings_transformer_ids=None,
) -> dict:
    """Build a monitored-element dict for :func:`run_security_analysis`."""
    return {
        "contingency_context_type": ctx_type,
        "contingency_ids": (
            list(contingency_ids)
            if ctx_type == "SPECIFIC" and contingency_ids else None
        ),
        "branch_ids": list(branch_ids) if branch_ids else None,
        "voltage_level_ids": (
            list(voltage_level_ids) if voltage_level_ids else None
        ),
        "three_windings_transformer_ids": (
            list(three_windings_transformer_ids)
            if three_windings_transformer_ids else None
        ),
    }


def monitored_element_summary(entry: dict) -> str:
    """One-line description of a monitored-element rule."""
    parts = [f"context={entry.get('contingency_context_type', 'ALL')}"]
    if entry.get("contingency_context_type") == "SPECIFIC":
        parts.append(
            f"contingencies={', '.join(entry.get('contingency_ids') or [])}"
        )
    for key, label in (
        ("branch_ids", "branches"),
        ("voltage_level_ids", "VLs"),
        ("three_windings_transformer_ids", "3WTs"),
    ):
        ids = entry.get(key) or []
        if ids:
            parts.append(f"{label}={len(ids)}")
    return " · ".join(parts)


# --- Limit reductions ------------------------------------------------------
def validate_limit_reduction(
    value: float, permanent: bool, temporary: bool,
) -> list[str]:
    """Validate a limit-reduction entry."""
    errors: list[str] = []
    if not (permanent or temporary):
        errors.append("Pick at least one of 'Permanent' or 'Temporary'.")
    if not (0.0 <= value <= 1.0):
        errors.append("Reduction value must be in [0, 1].")
    return errors


def make_limit_reduction(
    value: float,
    permanent: bool,
    temporary: bool,
    *,
    min_temporary_duration: int = 0,
    max_temporary_duration: int = 0,
    country: str = "",
    min_voltage: float = 0.0,
    max_voltage: float = 0.0,
) -> dict:
    """Build a limit-reduction dict for :func:`run_security_analysis`.

    Optional fields are only added when set to a non-default value —
    pypowsybl treats absent keys as "no filter".
    """
    entry: dict = {
        "limit_type": "CURRENT",
        "permanent": bool(permanent),
        "temporary": bool(temporary),
        "value": float(value),
        "contingency_context": "ALL",
    }
    if temporary and min_temporary_duration > 0:
        entry["min_temporary_duration"] = int(min_temporary_duration)
    if temporary and max_temporary_duration > 0:
        entry["max_temporary_duration"] = int(max_temporary_duration)
    if country and country.strip():
        entry["country"] = country.strip().upper()
    if min_voltage > 0:
        entry["min_voltage"] = float(min_voltage)
    if max_voltage > 0:
        entry["max_voltage"] = float(max_voltage)
    return entry


def limit_reduction_summary(entry: dict) -> str:
    """One-line description of a limit-reduction entry."""
    scope = []
    if entry.get("permanent"):
        scope.append("permanent")
    if entry.get("temporary"):
        scope.append("temporary")
    parts = [
        f"value={entry.get('value')} on {' + '.join(scope) or '?'} "
        f"{entry.get('limit_type', 'CURRENT')}"
    ]
    extras = [
        f"{k}={entry[k]}"
        for k in ("min_temporary_duration", "max_temporary_duration",
                  "country", "min_voltage", "max_voltage")
        if k in entry
    ]
    if extras:
        parts.append(" · ".join(extras))
    return "  ·  ".join(parts)


# --- Remedial actions ------------------------------------------------------
def validate_action(
    action_type: str,
    action_id: str,
    fields: dict,
    existing_ids,
) -> list[str]:
    """Validate a remedial-action definition.

    Checks the id is present + unique, the type is known, and every
    ``id``-kind field carries a non-empty value.
    """
    errors: list[str] = []
    aid = (action_id or "").strip()
    if not aid:
        errors.append("Action ID is required.")
    elif aid in set(existing_ids or ()):
        errors.append(f"Action ID '{aid}' already exists.")
    spec = ACTION_FIELDS.get(action_type)
    if spec is None:
        errors.append(f"Unknown action type: {action_type!r}")
        return errors
    for fdef in spec["fields"]:
        if fdef["kind"] == "id" and not fields.get(fdef["name"]):
            errors.append(
                f"{fdef['label']} is required for a {action_type} action."
            )
    return errors


def make_action(action_type: str, action_id: str, fields: dict) -> dict:
    """Build a remedial-action dict for :func:`run_security_analysis`."""
    return {"action_id": str(action_id).strip(), "type": action_type, **fields}


# --- Operator strategies ---------------------------------------------------
def validate_operator_strategy(
    strategy_id: str,
    action_ids,
    existing_ids,
) -> list[str]:
    """Validate an operator-strategy definition."""
    errors: list[str] = []
    sid = (strategy_id or "").strip()
    if not sid:
        errors.append("Strategy ID is required.")
    elif sid in set(existing_ids or ()):
        errors.append(f"Strategy ID '{sid}' already exists.")
    if not action_ids:
        errors.append("Pick at least one action.")
    return errors


def make_operator_strategy(
    strategy_id: str,
    contingency_id: str,
    action_ids,
    condition_type: str = "TRUE_CONDITION",
    violation_subject_ids=None,
    violation_types=None,
) -> dict:
    """Build an operator-strategy dict for :func:`run_security_analysis`."""
    return {
        "operator_strategy_id": str(strategy_id).strip(),
        "contingency_id": contingency_id,
        "action_ids": list(action_ids),
        "condition_type": condition_type,
        "violation_subject_ids": list(violation_subject_ids or []),
        "violation_types": list(violation_types or []),
    }


def operator_strategy_summary(entry: dict) -> str:
    """One-line description of an operator strategy."""
    parts = [
        f"`{entry.get('operator_strategy_id')}` ← `{entry.get('contingency_id')}`",
        f"condition={entry.get('condition_type', 'TRUE_CONDITION')}",
        f"actions={', '.join(entry.get('action_ids') or [])}",
    ]
    subjects = entry.get("violation_subject_ids") or []
    vtypes = entry.get("violation_types") or []
    if subjects:
        parts.append(f"subjects={', '.join(subjects)}")
    if vtypes:
        parts.append(f"violation_types={', '.join(vtypes)}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Result summary — host-agnostic table the PySide6 / NiceGUI tabs render
# ---------------------------------------------------------------------------
def summarize_security_results(results: dict) -> pd.DataFrame:
    """One row per contingency: ``contingency_id`` / ``status`` /
    ``violations`` (the limit-violation count).

    Pure reduction over the dict :func:`run_security_analysis`
    returns, so every host renders the same Results overview. Returns
    an empty frame when there are no post-contingency results.
    """
    rows = []
    for cid, cr in (results.get("post") or {}).items():
        viol = cr.get("limit_violations")
        n = 0 if viol is None or getattr(viol, "empty", True) else len(viol)
        rows.append({
            "contingency_id": cid,
            "status": cr.get("status", "?"),
            "violations": n,
        })
    return pd.DataFrame(
        rows, columns=["contingency_id", "status", "violations"],
    )


# ---------------------------------------------------------------------------
# View-model — host-agnostic state container for the Security Analysis tab
# ---------------------------------------------------------------------------
@dataclass
class SecurityAnalysisViewModel:
    """Mutable state container for the Security Analysis configuration.

    All three hosts (Streamlit, PySide6, NiceGUI) maintain the same
    five lists + results + element_ids; this dataclass captures them
    in one shape and exposes the add / remove / clear flows so the
    state machine lives in one place. Each host wires its widgets to
    these methods and reads back ``results`` to render the Results
    sub-tab.

    The dict shapes inside the lists come from :func:`make_monitored_element`,
    :func:`make_limit_reduction`, :func:`make_action` and
    :func:`make_operator_strategy`, so any consumer of the run helpers
    (Streamlit ``run_security_analysis`` callers, the prototype tabs)
    can pass the lists straight through.

    The add helpers (``add_monitored``, ``add_reduction``, ``add_action``,
    ``add_strategy``) return a list of validation error strings; an
    empty list signals "appended". Hosts surface the list in their
    native error widget. ``set_contingencies`` and ``set_strategies``
    accept a pre-built dict (so the auto-builders and manual UIs can
    feed in already-validated entries) and replace / append as
    requested.
    """

    contingencies: list[dict] = field(default_factory=list)
    monitored: list[dict] = field(default_factory=list)
    reductions: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    strategies: list[dict] = field(default_factory=list)
    results: Optional[dict] = None
    element_ids: dict[str, list[str]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Reset every list + results — call on network swap / reduction."""
        self.contingencies.clear()
        self.monitored.clear()
        self.reductions.clear()
        self.actions.clear()
        self.strategies.clear()
        self.results = None
        self.element_ids.clear()

    def clear_results(self) -> None:
        """Reset only the results (e.g. when the user reconfigures
        before re-running)."""
        self.results = None

    # ------------------------------------------------------------------
    # Element-id cache (the host fills it from ``get_element_ids``)
    # ------------------------------------------------------------------
    def set_element_ids(self, ids: dict[str, list[str]]) -> None:
        self.element_ids = dict(ids or {})

    # ------------------------------------------------------------------
    # Contingencies
    # ------------------------------------------------------------------
    def set_contingencies(self, contingencies) -> None:
        """Replace the contingency list — used by the auto N-1/N-2
        builders which always emit the whole set."""
        self.contingencies = list(contingencies or [])

    def append_contingency(self, contingency: dict) -> None:
        """Append a single manual contingency to the list."""
        self.contingencies.append(dict(contingency))

    def remove_contingency(self, index: int) -> None:
        if 0 <= index < len(self.contingencies):
            self.contingencies.pop(index)

    def contingency_ids(self) -> list[str]:
        return [c.get("id", "") for c in self.contingencies]

    # ------------------------------------------------------------------
    # Monitored elements
    # ------------------------------------------------------------------
    def add_monitored(
        self,
        ctx_type: str,
        contingency_ids=None,
        branch_ids=None,
        voltage_level_ids=None,
        three_windings_transformer_ids=None,
    ) -> list[str]:
        errors = validate_monitored_element(
            ctx_type, contingency_ids, branch_ids,
            voltage_level_ids, three_windings_transformer_ids,
        )
        if errors:
            return errors
        self.monitored.append(make_monitored_element(
            ctx_type,
            contingency_ids=contingency_ids,
            branch_ids=branch_ids,
            voltage_level_ids=voltage_level_ids,
            three_windings_transformer_ids=three_windings_transformer_ids,
        ))
        return []

    def remove_monitored(self, index: int) -> None:
        if 0 <= index < len(self.monitored):
            self.monitored.pop(index)

    # ------------------------------------------------------------------
    # Limit reductions
    # ------------------------------------------------------------------
    def add_reduction(
        self, value: float, permanent: bool, temporary: bool, **kwargs,
    ) -> list[str]:
        errors = validate_limit_reduction(value, permanent, temporary)
        if errors:
            return errors
        self.reductions.append(
            make_limit_reduction(value, permanent, temporary, **kwargs)
        )
        return []

    def remove_reduction(self, index: int) -> None:
        if 0 <= index < len(self.reductions):
            self.reductions.pop(index)

    # ------------------------------------------------------------------
    # Remedial actions
    # ------------------------------------------------------------------
    def add_action(
        self, action_type: str, action_id: str, fields: dict,
    ) -> list[str]:
        existing = [a.get("action_id", "") for a in self.actions]
        errors = validate_action(action_type, action_id, fields, existing)
        if errors:
            return errors
        self.actions.append(make_action(action_type, action_id, fields))
        return []

    def remove_action(self, index: int) -> None:
        if 0 <= index < len(self.actions):
            self.actions.pop(index)

    def action_ids(self) -> list[str]:
        return [a.get("action_id", "") for a in self.actions]

    # ------------------------------------------------------------------
    # Operator strategies
    # ------------------------------------------------------------------
    def add_strategy(
        self,
        strategy_id: str,
        contingency_id: str,
        action_ids,
        condition_type: str = "TRUE_CONDITION",
        violation_subject_ids=None,
        violation_types=None,
    ) -> list[str]:
        existing = [s.get("operator_strategy_id", "") for s in self.strategies]
        errors = validate_operator_strategy(strategy_id, action_ids, existing)
        if errors:
            return errors
        self.strategies.append(make_operator_strategy(
            strategy_id, contingency_id, action_ids,
            condition_type=condition_type,
            violation_subject_ids=violation_subject_ids,
            violation_types=violation_types,
        ))
        return []

    def remove_strategy(self, index: int) -> None:
        if 0 <= index < len(self.strategies):
            self.strategies.pop(index)

    # ------------------------------------------------------------------
    # Result handling
    # ------------------------------------------------------------------
    def store_results(self, results: dict) -> None:
        """Store the dict returned by :func:`run_security_analysis`."""
        self.results = results

    def has_results(self) -> bool:
        return bool(self.results)

    def results_summary(self) -> pd.DataFrame:
        """Pure reduction over ``self.results`` returning the
        ``contingency_id`` / ``status`` / ``violations`` table every
        host's Results sub-tab renders. Empty when no results."""
        if not self.results:
            return pd.DataFrame(
                columns=["contingency_id", "status", "violations"],
            )
        return summarize_security_results(self.results)


# ---------------------------------------------------------------------------
# Legacy aliases — existing tests + the Streamlit tab consume the
# underscored names. Keep them re-exported so the rename can land
# without breakage.
# ---------------------------------------------------------------------------
_ELEMENT_TYPES = ELEMENT_TYPES
_AUTO_MODES = AUTO_MODES
_MANUAL_TYPES = MANUAL_TYPES
_MANUAL_TYPE_IDS_KEY = MANUAL_TYPE_IDS_KEY
_MANUAL_GROUPINGS = MANUAL_GROUPINGS
_CTX_TYPES = CTX_TYPES
_ACTION_TYPES = ACTION_TYPES
_CONDITION_TYPES = CONDITION_TYPES
_VIOLATION_TYPES = VIOLATION_TYPES
_SIDES = SIDES
_apply_action = apply_action
_action_summary = action_summary
_get_nominal_voltages = get_nominal_voltages
_get_ids = get_element_ids
