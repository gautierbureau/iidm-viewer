"""Session-state op log for the HMI-mirror "Session Script" feature.

The recorder is intentionally tiny: every state-mutating call in
``state.py`` appends one record to the backing store under
``_op_log``.  ``script_generator.generate_script`` later turns that
list into a runnable Python script.

Design notes:

- The log is reset whenever a new network is loaded or an empty network
  is created — those are the only two valid entry points for a session,
  so the log always starts with one of them.
- Reverts do not delete entries; they set ``reverted=True`` on the
  matching op. The generator filters when emitting so the user can
  choose between "net state only" and "full transcript" at download
  time.
- Phase 1 recorded ``load_network``, ``create_empty`` and
  ``run_loadflow``.  Phase 2 adds component edits, component removals,
  extension edits, extension removals, and their revert counterparts.
- The backing store is a dict-like mapping injected via
  :func:`set_store`. Streamlit calls ``set_store(_store)``
  on every rerun so the log lives per-tab; the PySide6 / NiceGUI
  prototypes use the default module-level dict (single user per
  process). Test fixtures call :func:`reset_store` to start fresh.
"""
from __future__ import annotations

from typing import Any, MutableMapping

import pandas as pd


_OP_LOG_KEY = "_op_log"
_SOURCE_FILENAME_KEY = "_op_log_source_filename"
_PAUSED_KEY = "_op_log_paused"

# Owned by ``session_script.py``: the Recording toggle widget binds to
# this session-state key. The recorder pops it on session reset so the
# next render of the Session Script tab re-initialises the widget from
# its ``value=`` argument (defaulting to recording = ON).
RECORDING_WIDGET_KEY = "_session_script_recording_toggle"


# ---------------------------------------------------------------------------
# Backing store — host-injected
# ---------------------------------------------------------------------------
_store: MutableMapping[str, Any] = {}


def set_store(store: MutableMapping[str, Any]) -> None:
    """Replace the backing store.

    Streamlit calls this with ``_store`` so the recorder
    lives per browser tab. The PySide6 / NiceGUI prototypes leave the
    default module-level dict in place.
    """
    global _store
    _store = store


def reset_store() -> None:
    """Reset the recorder to an empty module-level dict.

    Useful for tests, and for non-Streamlit hosts that want a clean
    starting state.
    """
    global _store
    _store = {}


def get_log() -> list[dict[str, Any]]:
    """Return the current op log (never ``None``)."""
    return list(_store.get(_OP_LOG_KEY, []))


def get_source_filename() -> str | None:
    """Original filename of the loaded network, or ``None`` for empty starts."""
    return _store.get(_SOURCE_FILENAME_KEY)


def is_paused() -> bool:
    """Return True when recording is currently paused.

    Defaults to False (recording) — every new session starts recording,
    and the user must explicitly pause via the Session Script tab.
    """
    return bool(_store.get(_PAUSED_KEY, False))


def set_paused(value: bool) -> None:
    """Pause or resume recording.

    Pausing makes every ``record_*`` call after this point a silent
    no-op. The op log itself is preserved; resuming continues
    appending. Loading a new network always re-enables recording.
    """
    _store[_PAUSED_KEY] = bool(value)


def _reset_recording_state() -> None:
    """Clear the pause flag and the bound toggle widget.

    Called on every new-session boundary (load_network, create_empty,
    clear_log). Popping the widget key forces ``st.toggle`` to
    re-initialise from its ``value=`` argument the next time the
    Session Script tab renders, so the toggle visibly snaps back to
    Recording = ON.
    """
    _store[_PAUSED_KEY] = False
    _store.pop(RECORDING_WIDGET_KEY, None)


def clear_log() -> None:
    """Drop every recorded op. The next ``record_load_network`` /
    ``record_create_empty`` reseeds the log."""
    _store[_OP_LOG_KEY] = []
    _store.pop(_SOURCE_FILENAME_KEY, None)
    _reset_recording_state()


def _reset_with(op: dict[str, Any], source_filename: str | None) -> None:
    _store[_OP_LOG_KEY] = [op]
    if source_filename is None:
        _store.pop(_SOURCE_FILENAME_KEY, None)
    else:
        _store[_SOURCE_FILENAME_KEY] = source_filename
    _reset_recording_state()


def _append(op: dict[str, Any]) -> None:
    if is_paused():
        return
    log = list(_store.get(_OP_LOG_KEY, []))
    log.append(op)
    _store[_OP_LOG_KEY] = log


def record_load_network(
    filename: str,
    parameters: dict[str, str] | None,
    post_processors: list[str] | None,
) -> None:
    """Seed the log with a ``load_network`` op. Clears any prior log."""
    _reset_with(
        {
            "kind": "load_network",
            "parameters": dict(parameters or {}),
            "post_processors": list(post_processors or []),
        },
        source_filename=filename,
    )


def record_create_empty(network_id: str) -> None:
    """Seed the log with a ``create_empty`` op. Clears any prior log."""
    _reset_with(
        {
            "kind": "create_empty",
            "network_id": network_id,
        },
        source_filename=None,
    )


def record_run_loadflow(
    generic: dict[str, Any] | None,
    provider: dict[str, Any] | None,
) -> None:
    """Append a ``run_loadflow`` op carrying the parameter snapshot.

    Both ``generic`` and ``provider`` are stored as plain dicts so the
    generator can ``repr()`` them straight into the emitted script.
    """
    _append(
        {
            "kind": "run_loadflow",
            "generic": dict(generic or {}),
            "provider": dict(provider or {}),
        }
    )


# --------------------------------------------------------- component edits


def _is_na(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _scalar(value: Any) -> Any:
    """Coerce numpy scalars / NaN to plain Python so the generator can ``repr()``."""
    if _is_na(value):
        return None
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            return value
    return value


def _mark_first_matching_reverted(
    log: list[dict[str, Any]],
    kind: str,
    **match: Any,
) -> bool:
    """Mark the latest non-reverted op matching ``kind`` and ``**match``.

    Walks backwards so the most recent edit on a given cell is the one
    that gets cancelled, which is what the HMI revert always means.
    Returns True when a match was found.
    """
    for op in reversed(log):
        if op.get("kind") != kind:
            continue
        if op.get("reverted"):
            continue
        if all(op.get(k) == v for k, v in match.items()):
            op["reverted"] = True
            return True
    return False


def _iter_cells(changes_df: pd.DataFrame):
    """Yield ``(element_id, column, value)`` for each non-null cell."""
    for element_id in changes_df.index:
        for col in changes_df.columns:
            value = changes_df.at[element_id, col]
            if _is_na(value):
                continue
            yield element_id, col, value


def record_update_components(
    component: str,
    method_name: str,
    changes_df: pd.DataFrame,
    original_df: pd.DataFrame,
    *,
    is_revert: bool = False,
) -> None:
    """Record a component update — one op per non-null cell.

    ``is_revert=True`` flips the semantics: the matching prior
    ``update_components`` op for each cell is marked ``reverted=True``
    and a ``revert_update_components`` op is appended so the full
    transcript still shows the revert as a distinct step.
    """
    if is_paused():
        return
    if changes_df.empty:
        return
    log = list(_store.get(_OP_LOG_KEY, []))
    for element_id, col, value in _iter_cells(changes_df):
        after = _scalar(value)
        if is_revert:
            _mark_first_matching_reverted(
                log,
                "update_components",
                component=component,
                element_id=element_id,
                property=col,
            )
            log.append(
                {
                    "kind": "revert_update_components",
                    "component": component,
                    "method_name": method_name,
                    "element_id": element_id,
                    "property": col,
                    "value": after,
                }
            )
        else:
            before_val = None
            if element_id in original_df.index and col in original_df.columns:
                before_val = _scalar(original_df.at[element_id, col])
            log.append(
                {
                    "kind": "update_components",
                    "component": component,
                    "method_name": method_name,
                    "element_id": element_id,
                    "property": col,
                    "before": before_val,
                    "after": after,
                    "reverted": False,
                }
            )
    _store[_OP_LOG_KEY] = log


def record_remove_components(component: str, ids: list[str]) -> None:
    """Record a component removal as a single op.

    The Data Explorer does not currently expose revert for removals, so
    ``reverted`` stays False — the field is kept for schema uniformity.
    """
    if not ids:
        return
    _append(
        {
            "kind": "remove_components",
            "component": component,
            "ids": [str(i) for i in ids],
            "reverted": False,
        }
    )


# --------------------------------------------------------- extension edits


def record_update_extension(
    extension_name: str,
    changes_df: pd.DataFrame,
    original_df: pd.DataFrame,
    *,
    is_revert: bool = False,
) -> None:
    """Record an extension update — one op per non-null cell.

    Same revert semantics as :func:`record_update_components`.
    """
    if is_paused():
        return
    if changes_df.empty:
        return
    log = list(_store.get(_OP_LOG_KEY, []))
    for element_id, col, value in _iter_cells(changes_df):
        after = _scalar(value)
        if is_revert:
            _mark_first_matching_reverted(
                log,
                "update_extension",
                extension_name=extension_name,
                element_id=element_id,
                property=col,
            )
            log.append(
                {
                    "kind": "revert_update_extension",
                    "extension_name": extension_name,
                    "element_id": element_id,
                    "property": col,
                    "value": after,
                }
            )
        else:
            before_val = None
            if element_id in original_df.index and col in original_df.columns:
                before_val = _scalar(original_df.at[element_id, col])
            log.append(
                {
                    "kind": "update_extension",
                    "extension_name": extension_name,
                    "element_id": element_id,
                    "property": col,
                    "before": before_val,
                    "after": after,
                    "reverted": False,
                }
            )
    _store[_OP_LOG_KEY] = log


def record_remove_extension(extension_name: str, ids: list[str]) -> None:
    """Record an extension removal as a single op."""
    if not ids:
        return
    _append(
        {
            "kind": "remove_extension",
            "extension_name": extension_name,
            "ids": [str(i) for i in ids],
            "reverted": False,
        }
    )


# ---------------------------------------------------------------- creations
#
# These mirror the create_* helpers in ``state.py``. They are called
# *after* the worker thread has applied the change, so we know the
# operation succeeded. None / empty-string entries are dropped from the
# payload up front — the generator emits ``repr(dict)`` straight into
# the script, and a literal ``None`` there would fail validation in
# pypowsybl on replay.


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: _scalar(v) for k, v in fields.items() if v is not None and v != ""}


def record_create_component_bay(
    component: str, bay_function: str, fields: dict[str, Any]
) -> None:
    """Record a feeder-bay creation (Generators, Loads, …, Shunt Compensators)."""
    _append(
        {
            "kind": "create_component_bay",
            "component": component,
            "bay_function": bay_function,
            "fields": _clean(fields),
        }
    )


def record_create_branch_bay(
    component: str, bay_function: str, fields: dict[str, Any]
) -> None:
    """Record a line or 2-winding-transformer creation with bays on each side."""
    _append(
        {
            "kind": "create_branch_bay",
            "component": component,
            "bay_function": bay_function,
            "fields": _clean(fields),
        }
    )


def record_create_container(
    component: str, create_function: str, fields: dict[str, Any]
) -> None:
    """Record a substation / voltage level / busbar section creation."""
    _append(
        {
            "kind": "create_container",
            "component": component,
            "create_function": create_function,
            "fields": _clean(fields),
        }
    )


def record_create_tap_changer(
    kind: str,
    create_method: str,
    transformer_id: str,
    main_fields: dict[str, Any],
    step_columns: list[str],
    step_defaults: dict[str, Any],
    steps: list[dict[str, Any]],
) -> None:
    """Record a ratio / phase tap changer creation on an existing 2WT."""
    _append(
        {
            "kind": "create_tap_changer",
            "tap_changer_kind": kind,
            "create_method": create_method,
            "transformer_id": transformer_id,
            "main_fields": _clean(main_fields),
            "step_columns": list(step_columns),
            "step_defaults": dict(step_defaults),
            "steps": [dict(s) for s in steps],
        }
    )


def record_create_coupling_device(
    bbs1: str, bbs2: str, switch_prefix: str | None
) -> None:
    """Record a coupling-device creation tying two busbar sections together."""
    _append(
        {
            "kind": "create_coupling_device",
            "bbs1": bbs1,
            "bbs2": bbs2,
            "switch_prefix": switch_prefix or None,
        }
    )


def record_create_hvdc_line(fields: dict[str, Any]) -> None:
    """Record an HVDC line creation between two existing converter stations."""
    _append(
        {
            "kind": "create_hvdc_line",
            "fields": _clean(fields),
        }
    )


def record_create_reactive_limits(
    element_id: str, mode: str, payload: list[dict[str, Any]]
) -> None:
    """Record reactive limits — ``mode`` is ``"minmax"`` or ``"curve"``."""
    _append(
        {
            "kind": "create_reactive_limits",
            "element_id": element_id,
            "mode": mode,
            "payload": [dict(p) for p in payload],
        }
    )


def record_create_operational_limits(
    element_id: str,
    side: str,
    limit_type: str,
    limits: list[dict[str, Any]],
    group_name: str = "DEFAULT",
) -> None:
    """Record a group of operational limits on one side of an element."""
    _append(
        {
            "kind": "create_operational_limits",
            "element_id": element_id,
            "side": side,
            "limit_type": limit_type,
            "limits": [dict(l) for l in limits],
            "group_name": group_name,
        }
    )


def record_create_extension(
    extension_name: str,
    target_id: str,
    row: dict[str, Any],
    index_col: str,
) -> None:
    """Record a single-element extension creation."""
    _append(
        {
            "kind": "create_extension",
            "extension_name": extension_name,
            "target_id": target_id,
            "row": _clean(row),
            "index_col": index_col,
        }
    )


def record_create_secondary_voltage_control(
    zones: list[dict[str, Any]], units: list[dict[str, Any]]
) -> None:
    """Record a secondary-voltage-control replacement (zones + units)."""
    _append(
        {
            "kind": "create_secondary_voltage_control",
            "zones": [dict(z) for z in zones],
            "units": [dict(u) for u in units],
        }
    )


# ----------------------------------------------------------- security analysis


def record_run_security_analysis(
    contingencies: list[dict[str, Any]],
    monitored_elements: list[dict[str, Any]] | None,
    limit_reductions: list[dict[str, Any]] | None,
    actions: list[dict[str, Any]] | None,
    operator_strategies: list[dict[str, Any]] | None,
    contingencies_json_paths: list[str] | None,
    actions_json_paths: list[str] | None,
    operator_strategies_json_paths: list[str] | None,
    lf_generic: dict[str, Any] | None,
    lf_provider: dict[str, Any] | None,
) -> None:
    """Record an AC security analysis run.

    Captures the full configuration the user assembled in the Security
    Analysis tab — every dict is deep-copied so subsequent edits in
    session state cannot mutate the recorded op. Filesystem JSON paths
    are recorded as-is; replaying the script needs the same files at
    the same paths.
    """
    _append(
        {
            "kind": "run_security_analysis",
            "contingencies": [dict(c) for c in (contingencies or [])],
            "monitored_elements": [dict(m) for m in (monitored_elements or [])],
            "limit_reductions": [dict(r) for r in (limit_reductions or [])],
            "actions": [dict(a) for a in (actions or [])],
            "operator_strategies": [dict(s) for s in (operator_strategies or [])],
            "contingencies_json_paths": list(contingencies_json_paths or []),
            "actions_json_paths": list(actions_json_paths or []),
            "operator_strategies_json_paths": list(operator_strategies_json_paths or []),
            "lf_generic": dict(lf_generic or {}),
            "lf_provider": dict(lf_provider or {}),
        }
    )


# ------------------------------------------------------- short circuit analysis


def record_run_short_circuit_analysis(
    faults: list[dict[str, Any]] | None,
    sc_params: dict[str, Any] | None,
) -> None:
    """Record a short circuit analysis run.

    ``faults`` is the list passed to :func:`state.run_short_circuit_analysis`
    (one dict per fault: ``id``, ``element_id``, ``fault_type``).
    ``sc_params`` is the parameter dict (``study_type``,
    ``with_feeder_result``, ``with_limit_violations``,
    ``min_voltage_drop_proportional_threshold``). Both are deep-copied
    so subsequent session-state edits cannot mutate the recorded op.
    """
    _append(
        {
            "kind": "run_short_circuit_analysis",
            "faults": [dict(f) for f in (faults or [])],
            "sc_params": dict(sc_params or {}),
        }
    )
