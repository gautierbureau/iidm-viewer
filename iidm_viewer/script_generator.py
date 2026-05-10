"""Pure-Python generator that turns an op log into a runnable script.

Kept deliberately free of Streamlit and pypowsybl imports so it can be
unit-tested against fixture op-logs without bringing the JVM online.

The emitted script:

- Imports the bare minimum (``argparse``, ``pandas`` when needed,
  ``pypowsybl.network``, ``pypowsybl.loadflow``).
- Optionally defines small helper functions (``_remove``, ``_create_*``)
  that mirror the equivalent helpers in :mod:`iidm_viewer.state` so the
  script does not depend on the ``iidm_viewer`` package.
- Defines ``process(network)`` containing the recorded operations in
  chronological order.
- Defines ``main()`` that either loads the network from a CLI-provided
  path (``argparse``) or creates an empty network — depending on the
  first op in the log — and then calls ``process``.

Phases:

- Phase 1: load_network / create_empty / run_loadflow.
- Phase 2: update_components / revert_update_components,
  update_extension / revert_update_extension, remove_components,
  remove_extension.
- Phase 3: create_component_bay, create_branch_bay, create_container,
  create_tap_changer, create_coupling_device, create_hvdc_line,
  create_reactive_limits, create_operational_limits, create_extension,
  create_secondary_voltage_control.

The public API (``generate_script``) does not change between phases.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


_UPDATE_KINDS = frozenset(
    {
        "update_components",
        "revert_update_components",
        "update_extension",
        "revert_update_extension",
    }
)


def generate_script(
    ops: list[dict[str, Any]],
    *,
    include_reverted: bool = False,
    source_filename: str | None = None,
    timestamp: datetime | None = None,
) -> str:
    """Return a runnable Python script that replays the given op log.

    Parameters
    ----------
    ops:
        Op log as written by ``script_recorder``. May be empty, in which
        case the script is a no-op stub with a single ``pass``.
    include_reverted:
        When ``False`` (default), ops marked ``reverted=True`` and every
        ``revert_*`` op are skipped so the script reproduces the *net*
        state. When ``True``, every op is emitted in order so the script
        is a full transcript of the HMI session, including reverts.
    source_filename:
        Original filename shown in the script header. Optional — used
        only for human-readable provenance.
    timestamp:
        Override the header timestamp. Useful for snapshot tests.
    """
    visible = _filter_visible(ops, include_reverted)
    ts = (timestamp or datetime.now()).isoformat(timespec="seconds")

    helpers = _collect_helpers(visible)
    needs_pandas = (
        any(op["kind"] in _UPDATE_KINDS for op in visible)
        or any(name in helpers for name in _HELPERS_NEED_PANDAS)
    )

    header = _emit_header(ts, source_filename, needs_pandas=needs_pandas)
    helper_block = _emit_helpers(helpers)
    body_lines = _emit_body(visible)
    main_lines = _emit_main(visible)

    parts: list[str] = [header, ""]
    if helper_block:
        parts.extend([*helper_block, ""])
    parts.extend([*body_lines, "", *main_lines, ""])
    return "\n".join(parts) + "\n"


def _filter_visible(
    ops: list[dict[str, Any]],
    include_reverted: bool,
) -> list[dict[str, Any]]:
    if include_reverted:
        return list(ops)
    out: list[dict[str, Any]] = []
    for op in ops:
        if op.get("reverted"):
            continue
        if op["kind"].startswith("revert_"):
            continue
        out.append(op)
    return out


# --------------------------------------------------------------------- header


def _emit_header(
    timestamp: str, source_filename: str | None, *, needs_pandas: bool
) -> str:
    src = (
        f"Source network: {source_filename}"
        if source_filename
        else "Source network: <empty start>"
    )
    lines = [
        '#!/usr/bin/env python3',
        f'"""Auto-generated from IIDM Viewer session on {timestamp}.',
        src,
        '"""',
        'import argparse',
    ]
    if needs_pandas:
        lines.append('import pandas as pd')
    lines.extend(
        [
            'import pypowsybl.network as pn',
            'import pypowsybl.loadflow as lf',
        ]
    )
    return "\n".join(lines)


# ------------------------------------------------------------------- helpers
#
# Helper functions emitted at module scope in the generated script.
# Each ``_HELPERS_REGISTRY`` entry is a self-contained block of source
# text. ``_KIND_HELPER_DEPS`` maps each op kind to the names it pulls
# in; ``_collect_helpers`` walks the visible op log and unions the deps
# so the emitted script only carries the helpers it actually uses.


_REMOVE_HELPER = '''\
_FEEDER_BAY_TYPES = {"Loads", "Generators", "Batteries", "Shunt Compensators", "Static VAR Compensators"}
_HVDC_TYPES = {"HVDC Lines", "VSC Converter Stations", "LCC Converter Stations"}


def _remove(network, component, ids):
    """Mirror of iidm_viewer.state.remove_components — kept inline so this
    script does not depend on the iidm_viewer package."""
    if component in _FEEDER_BAY_TYPES:
        pn.remove_feeder_bays(network, ids)
        return
    if component in _HVDC_TYPES:
        hvdc = network.get_hvdc_lines()
        hids = set()
        for eid in ids:
            if component == "HVDC Lines":
                hids.add(eid)
            else:
                mask = (hvdc["converter_station1_id"] == eid) | (hvdc["converter_station2_id"] == eid)
                hids.update(hvdc[mask].index.tolist())
        if hids:
            pn.remove_hvdc_lines(network, list(hids))
        return
    if component == "Voltage Levels":
        pn.remove_voltage_levels(network, ids)
        return
    if component == "Substations":
        vls = network.get_voltage_levels()
        vlids = vls[vls["substation_id"].isin(ids)].index.tolist()
        if vlids:
            pn.remove_voltage_levels(network, vlids)
        return
    network.remove_elements(ids)'''


_BAY_DF_HELPER = '''\
def _bay_df(fields):
    """Build a one-row, id-indexed DataFrame from a flat fields dict."""
    return pd.DataFrame([dict(fields)]).set_index("id")'''


_SHUNT_BAY_HELPER = '''\
_SHUNT_LINEAR_FIELDS = {"g_per_section", "b_per_section", "max_section_count"}


def _create_shunt_bay(network, fields):
    """Mirror of iidm_viewer.state._dispatch_shunt_bay (LINEAR model only)."""
    linear_row = {k: fields[k] for k in _SHUNT_LINEAR_FIELDS if k in fields}
    linear_row["id"] = fields["id"]
    shunt_row = {k: v for k, v in fields.items() if k not in _SHUNT_LINEAR_FIELDS}
    shunt_row["model_type"] = "LINEAR"
    pn.create_shunt_compensator_bay(
        network,
        pd.DataFrame([shunt_row]).set_index("id"),
        linear_model_df=pd.DataFrame([linear_row]).set_index("id"),
    )'''


_CONTAINER_HELPER = '''\
def _create_container(network, create_function, fields):
    """Create a substation / voltage level / busbar section."""
    getattr(network, create_function)(pd.DataFrame([dict(fields)]).set_index("id"))'''


_TAP_CHANGER_HELPER = '''\
def _create_tap_changer(network, method, transformer_id, main_fields, step_columns, step_defaults, steps):
    """Mirror of iidm_viewer.state.create_tap_changer.

    Drops zero-sentinel target_v / target_deadband so pypowsybl sees
    them as unset, then issues the create call with two DataFrames.
    """
    main_row = {
        k: v for k, v in main_fields.items()
        if v is not None and v != "" and not (k in ("target_v", "target_deadband") and v == 0.0)
    }
    main_row["id"] = transformer_id
    main_df = pd.DataFrame([main_row]).set_index("id")
    step_rows = []
    for step in steps:
        row = {"id": transformer_id}
        for col in step_columns:
            row[col] = step.get(col, step_defaults[col])
        step_rows.append(row)
    steps_df = pd.DataFrame(step_rows).set_index("id")
    getattr(network, method)(main_df, steps_df)'''


_REACTIVE_LIMITS_HELPER = '''\
def _create_reactive_limits(network, element_id, mode, payload):
    """Mirror of iidm_viewer.state.create_reactive_limits."""
    if mode == "minmax":
        row = payload[0]
        df = pd.DataFrame(
            [{"id": element_id, "min_q": row["min_q"], "max_q": row["max_q"]}]
        ).set_index("id")
        network.create_minmax_reactive_limits(df)
        return
    rows = [
        {"id": element_id, "p": r["p"], "min_q": r["min_q"], "max_q": r["max_q"]}
        for r in payload
    ]
    network.create_curve_reactive_limits(pd.DataFrame(rows).set_index("id"))'''


_OPERATIONAL_LIMITS_HELPER = '''\
def _create_operational_limits(network, element_id, side, limit_type, limits, group_name="DEFAULT"):
    """Mirror of iidm_viewer.state.create_operational_limits.

    Defaults the row ``name`` the same way the HMI does when the user
    left it blank: ``permanent`` for the permanent limit (-1), or
    ``TATL_<duration>`` for a temporary limit.
    """
    rows = []
    for lim in limits:
        duration = int(lim["acceptable_duration"])
        name = lim.get("name") or ("permanent" if duration == -1 else f"TATL_{duration}")
        rows.append({
            "element_id": element_id,
            "side": side,
            "name": name,
            "type": limit_type,
            "value": float(lim["value"]),
            "acceptable_duration": duration,
            "fictitious": bool(lim.get("fictitious", False)),
            "group_name": group_name,
        })
    network.create_operational_limits(pd.DataFrame(rows).set_index("element_id"))'''


_EXTENSION_HELPER = '''\
def _create_extension(network, extension_name, target_id, row, index_col):
    """Attach a single extension row to an existing element."""
    df = pd.DataFrame(
        {k: [v] for k, v in row.items()},
        index=pd.Index([target_id], name=index_col),
    )
    network.create_extensions(extension_name, df)'''


_SVC_HELPER = '''\
def _create_secondary_voltage_control(network, zones, units):
    """Replace the secondaryVoltageControl extension with zones + units."""
    zones_df = pd.DataFrame(
        {
            "target_v": [float(z["target_v"]) for z in zones],
            "bus_ids": [(z.get("bus_ids") or "").strip() for z in zones],
        },
        index=pd.Index([z["name"].strip() for z in zones], name="name"),
    )
    units_df = pd.DataFrame(
        {
            "zone_name": [u["zone_name"].strip() for u in units],
            "participate": [bool(u.get("participate", True)) for u in units],
        },
        index=pd.Index([u["unit_id"].strip() for u in units], name="unit_id"),
    )
    network.create_extensions("secondaryVoltageControl", [zones_df, units_df])'''


_SECURITY_ANALYSIS_HELPER = '''\
import pypowsybl.security as sa


def _apply_action(analysis, action):
    """Mirror of iidm_viewer.state._apply_action — dispatches one remedial
    action dict to the right add_*_action call."""
    from pypowsybl._pypowsybl import Side
    action_id = action["action_id"]
    atype = action["type"]
    side = Side.__members__.get(action.get("side", "NONE"), Side.NONE)
    if atype == "SWITCH":
        analysis.add_switch_action(action_id, action["switch_id"], bool(action["open"]))
    elif atype == "TERMINALS_CONNECTION":
        analysis.add_terminals_connection_action(
            action_id, action["element_id"], side=side,
            opening=bool(action.get("opening", True)),
        )
    elif atype == "GENERATOR_ACTIVE_POWER":
        analysis.add_generator_active_power_action(
            action_id, action["generator_id"],
            bool(action["is_relative"]), float(action["active_power"]),
        )
    elif atype == "LOAD_ACTIVE_POWER":
        analysis.add_load_active_power_action(
            action_id, action["load_id"],
            bool(action["is_relative"]), float(action["active_power"]),
        )
    elif atype == "PHASE_TAP_CHANGER_POSITION":
        analysis.add_phase_tap_changer_position_action(
            action_id, action["transformer_id"],
            bool(action["is_relative"]), int(action["tap_position"]), side=side,
        )
    elif atype == "RATIO_TAP_CHANGER_POSITION":
        analysis.add_ratio_tap_changer_position_action(
            action_id, action["transformer_id"],
            bool(action["is_relative"]), int(action["tap_position"]), side=side,
        )
    elif atype == "SHUNT_COMPENSATOR_POSITION":
        analysis.add_shunt_compensator_position_action(
            action_id, action["shunt_id"], int(action["section"]),
        )
    else:
        raise ValueError(f"Unsupported action type: {atype!r}")


def _run_security_analysis(
    network,
    contingencies=None,
    monitored_elements=None,
    limit_reductions=None,
    actions=None,
    operator_strategies=None,
    contingencies_json_paths=None,
    actions_json_paths=None,
    operator_strategies_json_paths=None,
    lf_generic=None,
    lf_provider=None,
):
    """Mirror of iidm_viewer.state.run_security_analysis."""
    from pypowsybl.flowdecomposition import ContingencyContextType
    from pypowsybl._pypowsybl import ConditionType, ViolationType

    contingencies = contingencies or []
    monitored_elements = monitored_elements or []
    limit_reductions = limit_reductions or []
    actions = actions or []
    operator_strategies = operator_strategies or []
    contingencies_json_paths = contingencies_json_paths or []
    actions_json_paths = actions_json_paths or []
    operator_strategies_json_paths = operator_strategies_json_paths or []

    analysis = sa.create_analysis()
    for c in contingencies:
        eids = list(c.get("element_ids") or ([c["element_id"]] if "element_id" in c else []))
        if len(eids) == 1:
            analysis.add_single_element_contingency(eids[0], c["id"])
        elif len(eids) > 1:
            analysis.add_multiple_elements_contingency(eids, c["id"])
    for p in contingencies_json_paths:
        analysis.add_contingencies_from_json_file(p)

    for me in monitored_elements:
        ctx_name = me.get("contingency_context_type", "ALL")
        ctx = ContingencyContextType.__members__.get(ctx_name, ContingencyContextType.ALL)
        analysis.add_monitored_elements(
            contingency_context_type=ctx,
            contingency_ids=me.get("contingency_ids") or None,
            branch_ids=me.get("branch_ids") or None,
            voltage_level_ids=me.get("voltage_level_ids") or None,
            three_windings_transformer_ids=me.get("three_windings_transformer_ids") or None,
        )

    if limit_reductions:
        lr_df = pd.DataFrame(limit_reductions).set_index("limit_type")
        analysis.add_limit_reductions(lr_df)

    for action in actions:
        _apply_action(analysis, action)
    for p in actions_json_paths:
        analysis.add_actions_from_json_file(p)

    for strat in operator_strategies:
        cond_name = strat.get("condition_type", "TRUE_CONDITION")
        cond = ConditionType.__members__.get(cond_name, ConditionType.TRUE_CONDITION)
        vtype_names = strat.get("violation_types") or []
        vtypes = [
            ViolationType.__members__[n] for n in vtype_names
            if n in ViolationType.__members__
        ] or None
        vsubjects = list(strat.get("violation_subject_ids") or []) or None
        analysis.add_operator_strategy(
            strat["operator_strategy_id"], strat["contingency_id"],
            list(strat["action_ids"]),
            condition_type=cond,
            violation_subject_ids=vsubjects, violation_types=vtypes,
        )
    for p in operator_strategies_json_paths:
        analysis.add_operator_strategies_from_json_file(p)

    lf_params = lf.Parameters(**(lf_generic or {}))
    if lf_provider:
        lf_params.provider_parameters = {k: str(v) for k, v in lf_provider.items()}
    params = sa.Parameters(load_flow_parameters=lf_params)
    result = analysis.run_ac(network, parameters=params)
    print(f"Security analysis pre-contingency status: {result.pre_contingency_result.status.name}")
    return result'''


_HELPERS_REGISTRY: dict[str, str] = {
    "remove": _REMOVE_HELPER,
    "bay_df": _BAY_DF_HELPER,
    "shunt_bay": _SHUNT_BAY_HELPER,
    "container": _CONTAINER_HELPER,
    "tap_changer": _TAP_CHANGER_HELPER,
    "reactive_limits": _REACTIVE_LIMITS_HELPER,
    "operational_limits": _OPERATIONAL_LIMITS_HELPER,
    "extension": _EXTENSION_HELPER,
    "secondary_voltage_control": _SVC_HELPER,
    "security_analysis": _SECURITY_ANALYSIS_HELPER,
}


# Helpers that need ``import pandas as pd``. ``_remove`` is the only one
# that does not.
_HELPERS_NEED_PANDAS = frozenset(_HELPERS_REGISTRY) - {"remove"}


_KIND_HELPER_DEPS: dict[str, set[str]] = {
    "remove_components": {"remove"},
    "create_component_bay": {"bay_df", "shunt_bay"},
    "create_branch_bay": {"bay_df"},
    "create_container": {"container"},
    "create_tap_changer": {"tap_changer"},
    "create_hvdc_line": {"bay_df"},
    "create_reactive_limits": {"reactive_limits"},
    "create_operational_limits": {"operational_limits"},
    "create_extension": {"extension"},
    "create_secondary_voltage_control": {"secondary_voltage_control"},
    "run_security_analysis": {"security_analysis"},
}


def _collect_helpers(ops: list[dict[str, Any]]) -> set[str]:
    needed: set[str] = set()
    for op in ops:
        needed |= _KIND_HELPER_DEPS.get(op["kind"], set())
    # ``shunt_bay`` is only needed if a Shunt Compensators creation appears.
    if "shunt_bay" in needed and not any(
        op["kind"] == "create_component_bay" and op["component"] == "Shunt Compensators"
        for op in ops
    ):
        needed.discard("shunt_bay")
    # ``bay_df`` is only needed if a non-Shunt component-bay or any
    # branch-bay or HVDC-line op exists.
    if "bay_df" in needed and not any(
        (op["kind"] == "create_component_bay" and op["component"] != "Shunt Compensators")
        or op["kind"] in ("create_branch_bay", "create_hvdc_line")
        for op in ops
    ):
        needed.discard("bay_df")
    return needed


def _emit_helpers(needed: set[str]) -> list[str]:
    if not needed:
        return []
    out: list[str] = []
    # Emit in registry order for stable output.
    for name, src in _HELPERS_REGISTRY.items():
        if name in needed:
            if out:
                out.append("")
            out.extend(src.splitlines())
    return out


# ----------------------------------------------------------------------- body


def _emit_body(ops: list[dict[str, Any]]) -> list[str]:
    """Emit ``def process(network): ...`` from the in-session ops.

    Adjacent ops that target the same update method (or the same
    extension) are merged into a single DataFrame so the emitted script
    issues one pypowsybl call per logical group instead of one per cell.
    """
    lines = ["def process(network):"]
    body: list[str] = []
    i = 0
    while i < len(ops):
        op = ops[i]
        kind = op["kind"]
        if kind == "update_components":
            batch, i = _collect_batch(ops, i, kind, "method_name")
            body.extend(_emit_update_components(batch, revert=False))
        elif kind == "revert_update_components":
            batch, i = _collect_batch(ops, i, kind, "method_name")
            body.extend(_emit_update_components(batch, revert=True))
        elif kind == "update_extension":
            batch, i = _collect_batch(ops, i, kind, "extension_name")
            body.extend(_emit_update_extension(batch, revert=False))
        elif kind == "revert_update_extension":
            batch, i = _collect_batch(ops, i, kind, "extension_name")
            body.extend(_emit_update_extension(batch, revert=True))
        elif kind == "remove_components":
            body.extend(_emit_remove_components(op))
            i += 1
        elif kind == "remove_extension":
            body.extend(_emit_remove_extension(op))
            i += 1
        elif kind == "run_loadflow":
            body.extend(_emit_run_loadflow(op))
            i += 1
        elif kind == "run_security_analysis":
            body.extend(_emit_run_security_analysis(op))
            i += 1
        elif kind in _CREATE_EMITTERS:
            body.extend(_CREATE_EMITTERS[kind](op))
            i += 1
        else:
            # Unknown / non-body kinds (load_network, create_empty) are
            # handled in main() — just skip here.
            i += 1
    if not body:
        body.append("    pass")
    lines.extend(body)
    return lines


def _collect_batch(
    ops: list[dict[str, Any]],
    start: int,
    kind: str,
    target_key: str,
) -> tuple[list[dict[str, Any]], int]:
    """Greedy run of consecutive same-kind, same-target ops."""
    target = ops[start].get(target_key)
    j = start + 1
    while j < len(ops) and ops[j]["kind"] == kind and ops[j].get(target_key) == target:
        j += 1
    return ops[start:j], j


def _merge_cells(
    ops: list[dict[str, Any]], value_key: str
) -> dict[str, dict[str, Any]]:
    """Build ``{element_id: {property: value}}`` over a batch.

    Later ops win for the same cell — matches the HMI's "last write
    wins" semantics inside a single batch.
    """
    rows: dict[str, dict[str, Any]] = {}
    for op in ops:
        rows.setdefault(op["element_id"], {})[op["property"]] = op[value_key]
    return rows


def _emit_update_components(
    batch: list[dict[str, Any]], *, revert: bool
) -> list[str]:
    method = batch[0]["method_name"]
    component = batch[0]["component"]
    value_key = "value" if revert else "after"
    rows = _merge_cells(batch, value_key)
    verb = "Revert" if revert else "Update"
    return [
        f"    # {verb} {component}",
        f"    network.{method}(pd.DataFrame.from_dict({rows!r}, orient='index'))",
    ]


def _emit_update_extension(
    batch: list[dict[str, Any]], *, revert: bool
) -> list[str]:
    extension = batch[0]["extension_name"]
    value_key = "value" if revert else "after"
    rows = _merge_cells(batch, value_key)
    verb = "Revert" if revert else "Update"
    return [
        f"    # {verb} {extension} extension",
        f"    network.update_extensions({extension!r}, pd.DataFrame.from_dict({rows!r}, orient='index'))",
    ]


def _emit_remove_components(op: dict[str, Any]) -> list[str]:
    return [
        f"    # Remove {op['component']}",
        f"    _remove(network, {op['component']!r}, {op['ids']!r})",
    ]


def _emit_remove_extension(op: dict[str, Any]) -> list[str]:
    return [
        f"    # Remove {op['extension_name']} extension",
        f"    network.remove_extensions({op['extension_name']!r}, {op['ids']!r})",
    ]


def _emit_run_loadflow(op: dict[str, Any]) -> list[str]:
    generic = op.get("generic") or {}
    provider = op.get("provider") or {}
    lines = ["    # Run AC load flow"]
    if generic:
        kwargs = ", ".join(f"{k}={v!r}" for k, v in generic.items())
        lines.append(f"    _lf_params = lf.Parameters({kwargs})")
    else:
        lines.append("    _lf_params = lf.Parameters()")
    if provider:
        lines.append(
            f"    _lf_params.provider_parameters = {{k: str(v) for k, v in {provider!r}.items()}}"
        )
    lines.append("    _lf_results = lf.run_ac(network, parameters=_lf_params)")
    lines.append('    print(f"Load flow: {_lf_results[0].status.name}")')
    return lines


# ------------------------------------------------------------------ creates


def _emit_create_component_bay(op: dict[str, Any]) -> list[str]:
    component = op["component"]
    fields = op["fields"]
    if component == "Shunt Compensators":
        return [
            "    # Create Shunt Compensators",
            f"    _create_shunt_bay(network, {fields!r})",
        ]
    return [
        f"    # Create {component}",
        f"    pn.{op['bay_function']}(network, _bay_df({fields!r}))",
    ]


def _emit_create_branch_bay(op: dict[str, Any]) -> list[str]:
    return [
        f"    # Create {op['component']}",
        f"    pn.{op['bay_function']}(network, _bay_df({op['fields']!r}))",
    ]


def _emit_create_container(op: dict[str, Any]) -> list[str]:
    return [
        f"    # Create {op['component']}",
        f"    _create_container(network, {op['create_function']!r}, {op['fields']!r})",
    ]


def _emit_create_tap_changer(op: dict[str, Any]) -> list[str]:
    return [
        f"    # Create {op['tap_changer_kind']} tap changer on {op['transformer_id']}",
        "    _create_tap_changer(",
        f"        network, {op['create_method']!r}, {op['transformer_id']!r},",
        f"        {op['main_fields']!r},",
        f"        {op['step_columns']!r}, {op['step_defaults']!r},",
        f"        {op['steps']!r},",
        "    )",
    ]


def _emit_create_coupling_device(op: dict[str, Any]) -> list[str]:
    bbs1, bbs2 = op["bbs1"], op["bbs2"]
    sw = op.get("switch_prefix")
    args = [
        f"bus_or_busbar_section_id_1={bbs1!r}",
        f"bus_or_busbar_section_id_2={bbs2!r}",
    ]
    if sw:
        args.append(f"switch_prefix_id={sw!r}")
    return [
        f"    # Create coupling device between {bbs1} and {bbs2}",
        f"    pn.create_coupling_device(network, {', '.join(args)})",
    ]


def _emit_create_hvdc_line(op: dict[str, Any]) -> list[str]:
    return [
        f"    # Create HVDC line {op['fields'].get('id', '')}",
        f"    network.create_hvdc_lines(_bay_df({op['fields']!r}))",
    ]


def _emit_create_reactive_limits(op: dict[str, Any]) -> list[str]:
    return [
        f"    # Create {op['mode']} reactive limits on {op['element_id']}",
        f"    _create_reactive_limits(network, {op['element_id']!r}, {op['mode']!r}, {op['payload']!r})",
    ]


def _emit_create_operational_limits(op: dict[str, Any]) -> list[str]:
    return [
        f"    # Create {op['limit_type']} operational limits on {op['element_id']} (side {op['side']})",
        "    _create_operational_limits(",
        f"        network, {op['element_id']!r}, {op['side']!r}, {op['limit_type']!r},",
        f"        {op['limits']!r}, group_name={op['group_name']!r},",
        "    )",
    ]


def _emit_create_extension(op: dict[str, Any]) -> list[str]:
    return [
        f"    # Create {op['extension_name']} extension on {op['target_id']}",
        "    _create_extension(",
        f"        network, {op['extension_name']!r}, {op['target_id']!r},",
        f"        {op['row']!r}, {op['index_col']!r},",
        "    )",
    ]


def _emit_create_secondary_voltage_control(op: dict[str, Any]) -> list[str]:
    return [
        "    # Create secondary voltage control",
        "    _create_secondary_voltage_control(",
        f"        network, {op['zones']!r}, {op['units']!r},",
        "    )",
    ]


def _emit_run_security_analysis(op: dict[str, Any]) -> list[str]:
    """Emit a single ``_run_security_analysis(...)`` call.

    Only includes kwargs that have a non-empty value to keep the script
    readable for the common case of a small contingency list with no
    monitored elements / actions / strategies.
    """
    kwargs: list[str] = []
    for key in (
        "contingencies",
        "monitored_elements",
        "limit_reductions",
        "actions",
        "operator_strategies",
        "contingencies_json_paths",
        "actions_json_paths",
        "operator_strategies_json_paths",
        "lf_generic",
        "lf_provider",
    ):
        value = op.get(key)
        if value:
            kwargs.append(f"        {key}={value!r},")
    if not kwargs:
        return [
            "    # Run AC security analysis",
            "    _run_security_analysis(network)",
        ]
    return [
        "    # Run AC security analysis",
        "    _run_security_analysis(",
        "        network,",
        *kwargs,
        "    )",
    ]


_CREATE_EMITTERS: dict[str, Any] = {
    "create_component_bay": _emit_create_component_bay,
    "create_branch_bay": _emit_create_branch_bay,
    "create_container": _emit_create_container,
    "create_tap_changer": _emit_create_tap_changer,
    "create_coupling_device": _emit_create_coupling_device,
    "create_hvdc_line": _emit_create_hvdc_line,
    "create_reactive_limits": _emit_create_reactive_limits,
    "create_operational_limits": _emit_create_operational_limits,
    "create_extension": _emit_create_extension,
    "create_secondary_voltage_control": _emit_create_secondary_voltage_control,
}


# ----------------------------------------------------------------------- main


def _emit_main(ops: list[dict[str, Any]]) -> list[str]:
    """Emit ``def main(): ...`` — constructs the network and calls ``process``.

    Picks the first ``load_network`` / ``create_empty`` op found. If the
    log has neither (e.g. cleared mid-session), falls back to an
    argparse path-load so the script still parses and runs.
    """
    entry = next(
        (o for o in ops if o["kind"] in ("load_network", "create_empty")),
        None,
    )

    if entry is None or entry["kind"] == "load_network":
        params = (entry or {}).get("parameters") or {}
        pps = (entry or {}).get("post_processors") or []
        extra = []
        if params:
            extra.append(f"parameters={params!r}")
        if pps:
            extra.append(f"post_processors={pps!r}")
        suffix = (", " + ", ".join(extra)) if extra else ""
        return [
            "def main():",
            '    p = argparse.ArgumentParser()',
            '    p.add_argument("network_path", help="Path to the network file (e.g. .xiidm)")',
            '    args = p.parse_args()',
            f"    network = pn.load(args.network_path{suffix})",
            "    process(network)",
            "",
            'if __name__ == "__main__":',
            "    main()",
        ]

    nid = entry["network_id"]
    return [
        "def main():",
        f"    network = pn.create_empty(network_id={nid!r})",
        "    process(network)",
        "",
        'if __name__ == "__main__":',
        "    main()",
    ]
