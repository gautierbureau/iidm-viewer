"""Pure-Python generator that turns an op log into a runnable script.

Kept deliberately free of Streamlit and pypowsybl imports so it can be
unit-tested against fixture op-logs without bringing the JVM online.

The emitted script:

- Imports the bare minimum (``argparse``, ``pandas`` if needed,
  ``pypowsybl.network``, ``pypowsybl.loadflow``).
- Defines ``process(network)`` containing the recorded operations in
  chronological order.
- Defines ``main()`` that either loads the network from a CLI-provided
  path (``argparse``) or creates an empty network — depending on the
  first op in the log — and then calls ``process``.

Phase 1 supports three op kinds. Later phases add more emitters; the
public API (``generate_script``) does not change.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable


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
        When ``False`` (default), ops marked ``reverted=True`` are
        skipped so the script reproduces the *net* state. When ``True``,
        every op is emitted in order so the script is a full transcript
        of the HMI session, including reverts.
    source_filename:
        Original filename shown in the script header. Optional — used
        only for human-readable provenance.
    timestamp:
        Override the header timestamp. Useful for snapshot tests.
    """
    visible = [op for op in ops if include_reverted or not op.get("reverted")]
    ts = (timestamp or datetime.now()).isoformat(timespec="seconds")

    header = _emit_header(ts, source_filename)
    body_lines = _emit_body(visible)
    main_lines = _emit_main(visible)

    parts = [header, "", *body_lines, "", *main_lines, ""]
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------- header


def _emit_header(timestamp: str, source_filename: str | None) -> str:
    src = f"Source network: {source_filename}" if source_filename else "Source network: <empty start>"
    return (
        '#!/usr/bin/env python3\n'
        f'"""Auto-generated from IIDM Viewer session on {timestamp}.\n'
        f'{src}\n'
        '"""\n'
        'import argparse\n'
        'import pypowsybl.network as pn\n'
        'import pypowsybl.loadflow as lf'
    )


# ----------------------------------------------------------------------- body


def _emit_body(ops: list[dict[str, Any]]) -> list[str]:
    """Emit ``def process(network): ...`` from the in-session ops.

    Skips the leading ``load_network`` / ``create_empty`` op — those are
    handled in ``main()`` since they construct the network object.
    """
    lines = ["def process(network):"]
    body: list[str] = []
    for op in ops:
        kind = op["kind"]
        emitter = _BODY_EMITTERS.get(kind)
        if emitter is None:
            continue
        body.extend(emitter(op))
    if not body:
        body.append("    pass")
    lines.extend(body)
    return lines


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


_BODY_EMITTERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "run_loadflow": _emit_run_loadflow,
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

    # create_empty entry
    nid = entry["network_id"]
    return [
        "def main():",
        f"    network = pn.create_empty(network_id={nid!r})",
        "    process(network)",
        "",
        'if __name__ == "__main__":',
        "    main()",
    ]
