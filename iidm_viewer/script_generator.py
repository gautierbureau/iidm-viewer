"""Pure-Python generator that turns an op log into a runnable script.

Kept deliberately free of Streamlit and pypowsybl imports so it can be
unit-tested against fixture op-logs without bringing the JVM online.

The emitted script:

- Imports the bare minimum (``argparse``, ``pandas`` when any update op
  exists, ``pypowsybl.network``, ``pypowsybl.loadflow``).
- Optionally defines a ``_remove`` helper mirroring
  :func:`iidm_viewer.state.remove_components` so the script stays
  self-contained.
- Defines ``process(network)`` containing the recorded operations in
  chronological order.
- Defines ``main()`` that either loads the network from a CLI-provided
  path (``argparse``) or creates an empty network — depending on the
  first op in the log — and then calls ``process``.

Phase 1 supported three op kinds (load_network, create_empty,
run_loadflow). Phase 2 adds update_components, revert_update_components,
remove_components, update_extension, revert_update_extension, and
remove_extension. The public API (``generate_script``) does not change.
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

    needs_pandas = any(op["kind"] in _UPDATE_KINDS for op in visible)
    needs_remove_helper = any(op["kind"] == "remove_components" for op in visible)

    header = _emit_header(ts, source_filename, needs_pandas=needs_pandas)
    helpers = _emit_remove_helper() if needs_remove_helper else []
    body_lines = _emit_body(visible)
    main_lines = _emit_main(visible)

    parts: list[str] = [header, ""]
    if helpers:
        parts.extend([*helpers, ""])
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


# -------------------------------------------------------------- remove helper


_REMOVE_HELPER_SRC = '''\
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


def _emit_remove_helper() -> list[str]:
    return _REMOVE_HELPER_SRC.splitlines()


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
