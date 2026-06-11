"""Host-agnostic variant manager primitives.

pypowsybl's :class:`Network` carries an in-memory variant manager.
This module wraps the four operations the viewer needs into
worker-routed primitives so the **switch + work + restore** invariant
is enforced in exactly one place:

* :func:`fetch_for_variant` — atomic switch + ``getattr(raw, fn_name)``
  + restore. Every variant-aware getter delegates to this.
* :func:`build_contingency_variant` — clone the current variant and
  disconnect a user-picked set of element ids on the clone.
* :func:`run_loadflow_on_variant` — run an AC LF on a target variant
  without leaving the working variant changed.
* :func:`drop_variant` — restore InitialState if it's the working
  variant, then remove the variant id from the variant manager.

The one rule (from AGENTS.md §1): ``set_working_variant`` mutates
the network's "current variant" globally. Between worker calls the
working variant is always :data:`INITIAL_VARIANT_ID`. Every
variant-scoped fetch must do switch + work + restore inside a single
:func:`~iidm_viewer.powsybl_worker.run` call.

No streamlit / Qt / NiceGUI imports.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from iidm_viewer.loadflow import LoadFlowResult
from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INITIAL_VARIANT_ID = "InitialState"
NK_VARIANT_ID = "N-K"


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------
def list_variants(network: NetworkProxy) -> list[str]:
    """Return the variant ids known to the network's variant manager.

    One worker hop, no side effects.
    """
    raw = object.__getattribute__(network, "_obj")
    return run(lambda: list(raw.get_variant_ids()))


def get_working_variant_id(network: NetworkProxy) -> str:
    """Return the currently-active variant id.

    Between viewer-driven worker calls this is always
    :data:`INITIAL_VARIANT_ID` — surfaced for tests and assertions.
    """
    raw = object.__getattribute__(network, "_obj")
    return run(lambda: raw.get_working_variant_id())


# ---------------------------------------------------------------------------
# Atomic switch + work + restore
# ---------------------------------------------------------------------------
def fetch_for_variant(
    network: NetworkProxy, fn_name: str, variant_id: Optional[str],
    *args: Any, **kwargs: Any,
) -> Any:
    """Atomic ``set_working_variant(variant_id)`` + ``getattr(raw, fn_name)(...)``
    + restore, all inside a single worker round-trip.

    ``variant_id`` is ``None`` or :data:`INITIAL_VARIANT_ID` → no
    switch happens at all (fast path matching today's behaviour).

    Any pypowsybl object returned is wrapped in :class:`NetworkProxy`
    so chained ``.svg`` access keeps running on the worker thread.
    """
    raw = object.__getattribute__(network, "_obj")

    def _do():
        if variant_id is None or variant_id == INITIAL_VARIANT_ID:
            return getattr(raw, fn_name)(*args, **kwargs)
        prev = raw.get_working_variant_id()
        try:
            raw.set_working_variant(variant_id)
            return getattr(raw, fn_name)(*args, **kwargs)
        finally:
            raw.set_working_variant(prev)

    result = run(_do)
    module = type(result).__module__ or ""
    if module.startswith("pypowsybl"):
        return NetworkProxy(result)
    return result


# ---------------------------------------------------------------------------
# Contingency variant builder
# ---------------------------------------------------------------------------
def _split_ids_by_type(raw, element_ids: list[str]) -> dict[str, list[str]]:
    """Bucket ``element_ids`` into ``lines`` / ``t2w`` / ``t3w`` /
    ``generators``. Order within each bucket follows ``element_ids``."""
    lines = set(raw.get_lines(attributes=[]).index)
    t2w = set(raw.get_2_windings_transformers(attributes=[]).index)
    t3w = set(raw.get_3_windings_transformers(attributes=[]).index)
    gens = set(raw.get_generators(attributes=[]).index)
    buckets: dict[str, list[str]] = {
        "lines": [], "t2w": [], "t3w": [], "generators": [], "unknown": [],
    }
    for eid in element_ids:
        if eid in lines:
            buckets["lines"].append(eid)
        elif eid in t2w:
            buckets["t2w"].append(eid)
        elif eid in t3w:
            buckets["t3w"].append(eid)
        elif eid in gens:
            buckets["generators"].append(eid)
        else:
            buckets["unknown"].append(eid)
    return buckets


def build_contingency_variant(
    network: NetworkProxy,
    contingency: dict,
    target_variant: str = NK_VARIANT_ID,
) -> None:
    """Clone the current variant into ``target_variant`` and disconnect
    every element id in ``contingency["element_ids"]`` on the clone.

    ``contingency`` is a dict with shape::

        {"id": "single_line_outage", "element_ids": ["L1", "L2", ...]}

    A pre-existing ``target_variant`` is replaced. The working variant
    is always restored to its pre-call value (typically
    :data:`INITIAL_VARIANT_ID`) before this function returns.

    Raises ``ValueError`` when ``element_ids`` is empty or when any id
    does not match a line / 2WT / 3WT / generator.
    """
    raw = object.__getattribute__(network, "_obj")
    element_ids = list(contingency.get("element_ids") or [])
    if not element_ids:
        raise ValueError("contingency must carry at least one element id")

    def _do():
        prev = raw.get_working_variant_id()
        try:
            existing = list(raw.get_variant_ids())
            if target_variant in existing:
                raw.remove_variant(target_variant)
            raw.clone_variant(prev, target_variant)
            raw.set_working_variant(target_variant)

            buckets = _split_ids_by_type(raw, element_ids)
            if buckets["unknown"]:
                raise ValueError(
                    f"Unknown element ids: {buckets['unknown']!r}"
                )

            if buckets["lines"]:
                ids = buckets["lines"]
                raw.update_lines(pd.DataFrame(
                    {"connected1": [False] * len(ids),
                     "connected2": [False] * len(ids)},
                    index=ids,
                ))
            if buckets["t2w"]:
                ids = buckets["t2w"]
                raw.update_2_windings_transformers(pd.DataFrame(
                    {"connected1": [False] * len(ids),
                     "connected2": [False] * len(ids)},
                    index=ids,
                ))
            if buckets["t3w"]:
                ids = buckets["t3w"]
                raw.update_3_windings_transformers(pd.DataFrame(
                    {"connected1": [False] * len(ids),
                     "connected2": [False] * len(ids),
                     "connected3": [False] * len(ids)},
                    index=ids,
                ))
            if buckets["generators"]:
                ids = buckets["generators"]
                raw.update_generators(pd.DataFrame(
                    {"connected": [False] * len(ids)},
                    index=ids,
                ))
        except Exception:
            # Roll back a half-built variant so a failure doesn't leave
            # a stray N-K with mixed connection state behind.
            try:
                raw.set_working_variant(prev)
            finally:
                if target_variant in raw.get_variant_ids():
                    raw.remove_variant(target_variant)
            raise
        finally:
            raw.set_working_variant(prev)

    run(_do)


# ---------------------------------------------------------------------------
# Variant-scoped load flow
# ---------------------------------------------------------------------------
def run_loadflow_on_variant(
    network: NetworkProxy,
    variant_id: str,
    *,
    generic_params: Optional[dict[str, Any]] = None,
    provider_params: Optional[dict[str, Any]] = None,
) -> LoadFlowResult:
    """Run an AC load flow on ``variant_id`` without leaving the working
    variant changed.

    Mirrors :func:`iidm_viewer.loadflow.run_ac` but switches working
    variants inside the worker call so the LF results land on the
    target variant and the caller never sees a switched-variant state.
    """
    raw = object.__getattribute__(network, "_obj")
    generic = generic_params or {}
    provider = provider_params or {}

    def _do():
        import pypowsybl.loadflow as lf
        import pypowsybl.report as r
        prev = raw.get_working_variant_id()
        try:
            raw.set_working_variant(variant_id)
            params = lf.Parameters(**generic)
            if provider:
                params.provider_parameters = {
                    k: str(v) for k, v in provider.items()
                }
            rn = r.ReportNode(
                task_key="loadFlowTask",
                default_name=f"Load Flow ({variant_id})",
            )
            results = lf.run_ac(raw, parameters=params, report_node=rn)
            return results, rn.to_json()
        finally:
            raw.set_working_variant(prev)

    results, report_json = run(_do)
    return LoadFlowResult(results, report_json)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def drop_variant(
    network: NetworkProxy, variant_id: str = NK_VARIANT_ID,
) -> None:
    """Remove ``variant_id`` from the network's variant manager.

    No-op when the variant doesn't exist. If it happens to be the
    working variant, restores :data:`INITIAL_VARIANT_ID` first so
    pypowsybl doesn't refuse the removal.
    """
    raw = object.__getattribute__(network, "_obj")

    def _do():
        if variant_id not in raw.get_variant_ids():
            return
        if raw.get_working_variant_id() == variant_id:
            raw.set_working_variant(INITIAL_VARIANT_ID)
        raw.remove_variant(variant_id)

    run(_do)
