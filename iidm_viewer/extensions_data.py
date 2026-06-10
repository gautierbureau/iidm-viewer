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

from dataclasses import dataclass, field

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


# ---------------------------------------------------------------------------
# View-model — host-agnostic state container for the Extensions Explorer tab
# ---------------------------------------------------------------------------
@dataclass
class ExtensionsExplorerViewModel:
    """Mutable state container for the Extensions Explorer tab.

    Both the PySide6 and NiceGUI hosts carry the same four state items:

    * ``info_df`` — pypowsybl's per-extension description DataFrame
      (used for the picker's caption).
    * ``current_extension`` — the extension currently displayed.
    * ``current_df`` — that extension's full unfiltered DataFrame.
    * ``pending_edits`` / ``pending_removals`` — uncommitted user
      edits + remove ticks. Both are scoped to ``current_extension``;
      switching to another extension drops them.

    Streamlit's host is rerun-driven and rebuilds these on every run,
    so it does not consume the view-model — the prototype hosts do.
    """

    info_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    current_extension: str = ""
    current_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    pending_edits: dict[str, dict[str, object]] = field(default_factory=dict)
    pending_removals: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Reset everything. Call on network swap."""
        self.info_df = pd.DataFrame()
        self.current_extension = ""
        self.current_df = pd.DataFrame()
        self.pending_edits = {}
        self.pending_removals = set()

    def reset_pending(self) -> None:
        """Drop pending edits + removals. Call when switching to
        another extension or after a successful apply / remove."""
        self.pending_edits = {}
        self.pending_removals = set()

    def set_info(self, info_df: pd.DataFrame) -> None:
        """Cache pypowsybl's per-extension description DataFrame."""
        self.info_df = info_df if info_df is not None else pd.DataFrame()

    def set_data(self, extension: str, df: pd.DataFrame) -> None:
        """Store the source DataFrame for ``extension``. Does **not**
        touch ``pending_edits`` / ``pending_removals`` — same-extension
        refreshes (post-apply, filter change) keep the in-progress ticks."""
        self.current_extension = extension or ""
        self.current_df = df if df is not None else pd.DataFrame()

    # ------------------------------------------------------------------
    # Derived read accessors
    # ------------------------------------------------------------------
    def detail(self) -> str:
        """Per-extension description from :func:`get_extensions_information`,
        or an empty string when unavailable."""
        if (
            self.info_df is None
            or self.info_df.empty
            or not self.current_extension
            or self.current_extension not in self.info_df.index
        ):
            return ""
        try:
            return str(self.info_df.loc[self.current_extension].get("detail") or "")
        except Exception:
            return ""

    def is_readonly(self) -> bool:
        """``True`` when the current extension is in
        :data:`READONLY_EXTENSIONS` and edits / removals must be hidden."""
        return self.current_extension in READONLY_EXTENSIONS

    def editable_cols(self, df: pd.DataFrame | None = None) -> list[str]:
        """Columns of ``df`` that pypowsybl's ``update_extensions`` accepts
        for the current extension. Defaults to ``current_df``."""
        if not self.current_extension:
            return []
        target = self.current_df if df is None else df
        if target is None or target.empty:
            return []
        cols = EDITABLE_EXTENSIONS.get(self.current_extension, [])
        return [c for c in cols if c in target.columns]

    def filtered_view(self, text: str) -> pd.DataFrame:
        """``current_df`` narrowed by the ID-substring filter."""
        return filter_by_id_substring(self.current_df, text or "")

    # ------------------------------------------------------------------
    # Pending edits + removals
    # ------------------------------------------------------------------
    def tick_remove(self, element_id: str, ticked: bool) -> None:
        """Mark / unmark ``element_id`` for removal on the next Apply."""
        if ticked:
            self.pending_removals.add(str(element_id))
        else:
            self.pending_removals.discard(str(element_id))

    def is_ticked(self, element_id: str) -> bool:
        """``True`` if ``element_id`` is awaiting removal."""
        return str(element_id) in self.pending_removals

    def add_edit(self, element_id: str, col: str, value) -> None:
        """Stage an edit to ``(element_id, col)``."""
        self.pending_edits.setdefault(str(element_id), {})[col] = value

    def get_edit(self, element_id: str, col: str):
        """Return the staged edit for ``(element_id, col)`` or ``None``."""
        return self.pending_edits.get(str(element_id), {}).get(col)

    def has_edits(self) -> bool:
        return bool(self.pending_edits)

    def has_removals(self) -> bool:
        return bool(self.pending_removals)

    def edits_changes_df(self) -> pd.DataFrame:
        """Pending edits as a DataFrame ready for :func:`update_extension`.
        Index = element_id, columns = the edited columns."""
        if not self.pending_edits:
            return pd.DataFrame()
        return pd.DataFrame.from_dict(self.pending_edits, orient="index")

    def removals_list(self) -> list[str]:
        """Sorted list of element_ids ticked for removal."""
        return sorted(self.pending_removals)

    def clear_edits(self) -> None:
        """Drop all pending edits (after successful Apply)."""
        self.pending_edits = {}

    def drop_edits_for(self, element_ids) -> None:
        """Drop pending edits for the given ids (after removal)."""
        for eid in element_ids:
            self.pending_edits.pop(str(eid), None)

    def clear_removals(self) -> None:
        """Drop all removal ticks (after successful Remove)."""
        self.pending_removals = set()
