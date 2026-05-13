"""Framework-agnostic helpers for the "Extensions Explorer" tab.

What lives here:

* :data:`EDITABLE_EXTENSIONS` — extension name → list of columns that
  pypowsybl's ``update_extensions`` accepts. Extensions not listed
  (e.g. ``substationPosition``, ``linePosition``) are read-only on
  the Java side and can only be replaced via
  ``remove_extensions`` + ``create_extensions``.
* :data:`READONLY_EXTENSIONS` — names that should *also* hide the
  Remove affordance (geographical positions managed elsewhere).
* Worker-routed pypowsybl wrappers used by all three prototypes:
  :func:`list_extension_names`, :func:`get_extensions_information`,
  :func:`get_extension_df`, :func:`remove_extension`,
  :func:`update_extension`.
* Pure :func:`filter_by_id_substring` for the ID-substring filter
  the dialogs show above the table.

Streamlit's ``extensions_explorer`` and ``state.py`` delegate to
these; the PySide6 + NiceGUI prototypes import them directly.
"""
from __future__ import annotations

import pandas as pd

from iidm_viewer.powsybl_worker import NetworkProxy, run


# Extension name -> list of columns pypowsybl's ``update_extensions`` accepts.
#
# Extensions not listed here (substationPosition, position, slackTerminal, ...)
# have immutable columns on the Java side and can only be changed by
# ``remove_extensions`` + ``create_extensions``. The dialogs render those
# as read-only.
EDITABLE_EXTENSIONS: dict[str, list[str]] = {
    "activePowerControl": [
        "participate", "droop", "participation_factor",
        "min_target_p", "max_target_p",
    ],
    "busbarSectionPosition": ["busbar_index", "section_index"],
    "entsoeArea": ["code"],
    "entsoeCategory": ["code"],
    "hvdcAngleDroopActivePowerControl": ["droop", "p0", "enabled"],
    "hvdcOperatorActivePowerRange": [
        "opr_from_cs1_to_cs2", "opr_from_cs2_to_cs1",
    ],
    "standbyAutomaton": [
        "standby", "b0",
        "low_voltage_threshold", "low_voltage_setpoint",
        "high_voltage_threshold", "high_voltage_setpoint",
    ],
    "voltagePerReactivePowerControl": ["slope"],
    "voltageRegulation": [
        "voltage_regulator_on", "target_v", "regulated_element_id",
    ],
}


# Extensions hosts should treat as read-only — geographical positions
# are managed outside the viewer, the Streamlit dialog used the same set.
READONLY_EXTENSIONS: frozenset[str] = frozenset({
    "substationPosition", "linePosition",
})


# ---------------------------------------------------------------------------
# Worker-routed pypowsybl probes (no caching — each host adds its own)
# ---------------------------------------------------------------------------
def list_extension_names() -> list[str]:
    """Return every extension name pypowsybl knows about, alphabetical."""
    def _do():
        import pypowsybl.network as pn
        try:
            return sorted(pn.get_extensions_names())
        except Exception:
            return []

    return run(_do)


def get_extensions_information() -> pd.DataFrame:
    """Return the pypowsybl ``get_extensions_information`` DataFrame.

    Used by the dialogs to surface a per-extension description /
    ``detail`` caption above the table. Returns an empty DataFrame
    when the pypowsybl build doesn't expose it.
    """
    def _do():
        import pypowsybl.network as pn
        try:
            return pn.get_extensions_information()
        except Exception:
            return pd.DataFrame()

    return run(_do)


def get_extension_df(network: NetworkProxy, extension_name: str) -> pd.DataFrame:
    """Fetch ``network.get_extensions(extension_name)`` on the worker.

    Returns an empty DataFrame when the extension is absent or
    pypowsybl refuses to enumerate it.
    """
    try:
        df = network.get_extensions(extension_name)
    except Exception:
        return pd.DataFrame()
    if df is None:
        return pd.DataFrame()
    return df


def remove_extension(
    network: NetworkProxy, extension_name: str, ids: list,
) -> None:
    """Remove extension rows from the network on the worker thread."""
    items = [str(i) for i in (ids or []) if i is not None]
    if not items:
        return
    raw = object.__getattribute__(network, "_obj")

    def _do_remove():
        raw.remove_extensions(extension_name, items)

    run(_do_remove)


def update_extension(
    network: NetworkProxy, extension_name: str, changes_df: pd.DataFrame,
) -> None:
    """Apply a DataFrame of changes to an extension via ``update_extensions``.

    ``changes_df`` is indexed by the extension's native index (usually
    the element id) and may contain NaN for cells that didn't change.
    pypowsybl rejects NaN values, so rows are grouped by their non-null
    column set and one update call is issued per group — same shape as
    ``update_components``.
    """
    if changes_df is None or changes_df.empty:
        return
    if extension_name not in EDITABLE_EXTENSIONS:
        raise ValueError(f"Extension {extension_name!r} is not editable.")
    raw = object.__getattribute__(network, "_obj")

    groups: dict[tuple[str, ...], list] = {}
    for idx in changes_df.index:
        row = changes_df.loc[idx]
        cols = tuple(row.dropna().index.tolist())
        groups.setdefault(cols, []).append(idx)

    def _do_update():
        for cols, ids in groups.items():
            subset = changes_df.loc[ids, list(cols)]
            raw.update_extensions(extension_name, subset)

    run(_do_update)


# ---------------------------------------------------------------------------
# Pure filter helper
# ---------------------------------------------------------------------------
def filter_by_id_substring(df: pd.DataFrame, text: str) -> pd.DataFrame:
    """Case-insensitive substring filter on the DataFrame index.

    Used by the dialogs' "Filter by ID" text input. ``text`` empty →
    return ``df`` unchanged.
    """
    if df is None or df.empty or not text:
        return df
    mask = df.index.astype(str).str.contains(
        text, case=False, na=False, regex=False,
    )
    return df[mask]
