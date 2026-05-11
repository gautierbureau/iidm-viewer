"""Framework-agnostic edit change log + revert helpers.

The Streamlit data explorer used to own a private collapse-and-revert
implementation in ``state.add_to_change_log`` /
``data_explorer._render_change_log``. This module hosts the
non-trivial piece — the merge/collapse logic — and a
:class:`ChangeLog` class that the PySide6 and NiceGUI prototypes use.
Streamlit's storage layer (per-method ``st.session_state`` lists)
stays as-is, but now delegates to :func:`merge_entry` instead of
re-implementing the rules.

Entry shape — kept identical across all three front-ends so the same
revert UI can render any of them:

    {
        "component":  str,   # e.g. "Generators"
        "element_id": str,   # the pypowsybl id
        "property":   str,   # the edited attribute
        "before":     Any,   # value before the first edit (may be NaN)
        "after":      Any,   # current value
    }

Rules implemented by :func:`merge_entry`:

* ``after`` NaN -> skip (mirrors the Streamlit "incomplete edit"
  contract).
* Re-edit of the same (component, element_id, property) collapses
  into the existing entry.
* If after a collapse the entry's ``after`` equals its ``before``,
  the entry is removed — the log shows only net differences.

Revert flows through :func:`apply_cell_edit` from the shared
``component_registry`` so it inherits worker-thread routing + dtype
coercion + the editable-attribute allow-list automatically.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import pandas as pd

from iidm_viewer.component_registry import apply_cell_edit
from iidm_viewer.powsybl_worker import NetworkProxy


# Public dict shape — exported so tests can document it explicitly.
ChangeLogEntry = dict  # {"component", "element_id", "property", "before", "after"}


def _is_nan(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _matches(entry: ChangeLogEntry, component: str, element_id: str, attribute: str) -> bool:
    return (
        entry.get("component", component) == component
        and entry.get("element_id") == element_id
        and entry.get("property") == attribute
    )


def merge_entry(
    log: list[ChangeLogEntry],
    component: str,
    element_id: str,
    attribute: str,
    before: Any,
    after: Any,
) -> None:
    """Append or collapse one cell change into ``log`` (mutated in place).

    See module docstring for the precise rules.
    """
    if _is_nan(after):
        return
    idx = next(
        (i for i, e in enumerate(log) if _matches(e, component, element_id, attribute)),
        None,
    )
    if idx is None:
        log.append({
            "component": component,
            "element_id": element_id,
            "property": attribute,
            "before": before,
            "after": after,
        })
        return
    existing = log[idx]
    existing["after"] = after
    try:
        if existing["before"] == after:
            log.pop(idx)
    except Exception:
        # Equality comparison can raise for some pandas/np scalar
        # mismatches; treat as "not equal" and leave the entry.
        pass


def revert_via_apply(
    network: NetworkProxy,
    entry: ChangeLogEntry,
) -> None:
    """Apply the inverse edit for ``entry``.

    Raises ``ValueError`` if the entry's ``before`` is unavailable
    (e.g. NaN). The caller is responsible for removing the entry from
    its own log on success.
    """
    if _is_nan(entry.get("before")):
        raise ValueError(
            f"cannot revert {entry.get('property')!r} on "
            f"{entry.get('element_id')!r}: original value unavailable"
        )
    apply_cell_edit(
        network,
        entry["component"],
        entry["element_id"],
        entry["property"],
        entry["before"],
    )


# ---------------------------------------------------------------------------
# In-memory ChangeLog for the PySide6 and NiceGUI prototypes.
# Streamlit doesn't use this directly — its storage is per-method
# ``st.session_state`` lists that already round-trip across reruns.
# ---------------------------------------------------------------------------
class ChangeLog:
    """Per-process change log.

    The Streamlit app keeps its own session-state-backed storage; the
    prototype hosts hold one of these instances and reset it on every
    network reload. Listeners can subscribe via :meth:`on_changed` to
    repaint a UI panel.

    Tracks two parallel timelines:

    * ``entries`` — edits (per cell or in bulk), revertable via the
      shared :func:`revert_via_apply`.
    * ``removals`` — destructive deletions, with an optional pandas
      snapshot of the removed rows kept around so a future host can
      offer "undo" via :func:`network.create_*`. The current
      prototypes only display removals; revert is out of scope.
    """

    def __init__(self) -> None:
        self._entries: list[ChangeLogEntry] = []
        self._removals: list[dict] = []  # {component, element_id, snapshot?}
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def entries(self, component: Optional[str] = None) -> list[ChangeLogEntry]:
        """Snapshot of the log, optionally filtered by component label."""
        if component is None:
            return list(self._entries)
        return [e for e in self._entries if e.get("component") == component]

    def __len__(self) -> int:
        return len(self._entries) + len(self._removals)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def record(
        self,
        component: str,
        element_id: str,
        attribute: str,
        before: Any,
        after: Any,
    ) -> None:
        before_n = len(self._entries)
        merge_entry(self._entries, component, element_id, attribute, before, after)
        # _fire only when something actually changed (append/collapse/remove).
        if len(self._entries) != before_n or self._last_entry_changed(component, element_id, attribute):
            self._fire()

    def record_bulk(
        self,
        component: str,
        attribute: str,
        prev_map: dict[str, Any],
        after: Any,
    ) -> None:
        """Record N entries from an ``apply_bulk_edit`` previous-value map."""
        if not prev_map:
            return
        for element_id, before in prev_map.items():
            merge_entry(
                self._entries,
                component, str(element_id), attribute, before, after,
            )
        self._fire()

    def clear(self) -> None:
        if not self._entries and not self._removals:
            return
        self._entries.clear()
        self._removals.clear()
        self._fire()

    # ------------------------------------------------------------------
    # Removals
    # ------------------------------------------------------------------
    def record_removal(
        self,
        component: str,
        element_ids,
        snapshot=None,
    ) -> None:
        """Append removal records for ``element_ids``.

        ``snapshot`` is an optional pandas DataFrame whose index
        carries those ids; the per-id row is stashed in the entry so
        a future host can offer "recreate" via the pypowsybl
        ``create_*`` APIs. Pass ``None`` to skip the snapshot (the
        cheap path the prototypes take).
        """
        if not element_ids:
            return
        existing = {(e.get("component"), str(e.get("element_id"))) for e in self._removals}
        added = False
        for eid in element_ids:
            key = (component, str(eid))
            if key in existing:
                continue
            entry: dict = {"component": component, "element_id": str(eid)}
            if snapshot is not None and str(eid) in {str(x) for x in getattr(snapshot, "index", [])}:
                try:
                    entry["snapshot"] = snapshot.loc[str(eid)].to_dict()
                except Exception:
                    pass
            self._removals.append(entry)
            existing.add(key)
            added = True
        if added:
            self._fire()

    def removals(self, component: Optional[str] = None) -> list[dict]:
        if component is None:
            return list(self._removals)
        return [r for r in self._removals if r.get("component") == component]

    def clear_removals(self) -> None:
        if not self._removals:
            return
        self._removals.clear()
        self._fire()

    # ------------------------------------------------------------------
    # Revert
    # ------------------------------------------------------------------
    def revert(self, network: NetworkProxy, entry: ChangeLogEntry) -> None:
        """Apply the inverse edit and drop the entry from the log."""
        revert_via_apply(network, entry)
        try:
            self._entries.remove(entry)
        except ValueError:
            pass
        self._fire()

    def revert_all(
        self,
        network: NetworkProxy,
        component: Optional[str] = None,
    ) -> tuple[int, list[ChangeLogEntry]]:
        """Revert every entry (optionally restricted to ``component``).

        Returns ``(reverted_count, skipped_entries)``. Entries whose
        ``before`` is unavailable are skipped silently and left in the
        log; callers can surface the count in a UI status.
        """
        targets = self.entries(component)
        if not targets:
            return 0, []
        reverted = 0
        skipped: list[ChangeLogEntry] = []
        for entry in list(targets):
            try:
                revert_via_apply(network, entry)
            except ValueError:
                skipped.append(entry)
                continue
            try:
                self._entries.remove(entry)
            except ValueError:
                pass
            reverted += 1
        if reverted:
            self._fire()
        return reverted, skipped

    # ------------------------------------------------------------------
    # Listener bus
    # ------------------------------------------------------------------
    def on_changed(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _fire(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                # A misbehaving listener mustn't break a successful edit.
                pass

    # Used by record() to detect "the entry exists and its after just changed".
    def _last_entry_changed(self, component: str, element_id: str, attribute: str) -> bool:
        for e in self._entries:
            if _matches(e, component, element_id, attribute):
                return True
        return False
