"""Session-state op log for the HMI-mirror "Session Script" feature.

The recorder is intentionally tiny: every state-mutating call in
``state.py`` appends one record to ``st.session_state["_op_log"]``.
``script_generator.generate_script`` later turns that list into a
runnable Python script.

Design notes:

- The log is reset whenever a new network is loaded or an empty network
  is created — those are the only two valid entry points for a session,
  so the log always starts with one of them.
- Reverts do not delete entries; they set ``reverted=True`` on the
  matching op. The generator filters when emitting so the user can
  choose between "net state only" and "full transcript" at download
  time.
- Phase 1 only records ``load_network``, ``create_empty`` and
  ``run_loadflow``. Later phases append more op kinds without changing
  this module's public surface.
"""
from __future__ import annotations

from typing import Any

import streamlit as st


_OP_LOG_KEY = "_op_log"
_SOURCE_FILENAME_KEY = "_op_log_source_filename"


def get_log() -> list[dict[str, Any]]:
    """Return the current op log (never ``None``)."""
    return list(st.session_state.get(_OP_LOG_KEY, []))


def get_source_filename() -> str | None:
    """Original filename of the loaded network, or ``None`` for empty starts."""
    return st.session_state.get(_SOURCE_FILENAME_KEY)


def clear_log() -> None:
    """Drop every recorded op. The next ``record_load_network`` /
    ``record_create_empty`` reseeds the log."""
    st.session_state[_OP_LOG_KEY] = []
    st.session_state.pop(_SOURCE_FILENAME_KEY, None)


def _reset_with(op: dict[str, Any], source_filename: str | None) -> None:
    st.session_state[_OP_LOG_KEY] = [op]
    if source_filename is None:
        st.session_state.pop(_SOURCE_FILENAME_KEY, None)
    else:
        st.session_state[_SOURCE_FILENAME_KEY] = source_filename


def _append(op: dict[str, Any]) -> None:
    log = list(st.session_state.get(_OP_LOG_KEY, []))
    log.append(op)
    st.session_state[_OP_LOG_KEY] = log


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
