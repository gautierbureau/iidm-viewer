"""Framework-agnostic helpers for the Operational Limits tab.

This module owns the pypowsybl integration + pure-pandas reductions
each UI host (Streamlit ``operational_limits_tab``, PySide6, NiceGUI)
composes into its own widget tree. No streamlit / Qt / NiceGUI imports
here — the Streamlit-only rendering + per-session caching live in
:mod:`iidm_viewer.operational_limits_tab`.

Public API:

* :data:`MAX_DOUBLE` — pypowsybl sentinel for "no limit".
* :func:`side_label` / :func:`duration_label` — pure formatters used
  by every host's chart labels.
* :func:`get_current_flows` / :func:`get_branch_losses` —
  worker-routed pypowsybl fetchers. No caching; hosts wrap with their
  own (Streamlit uses :mod:`iidm_viewer.caches`, the prototypes use
  module-level dicts).
* :func:`compute_loading` — worst-side ``loading_pct`` table.
* :func:`build_element_chart` — a Plotly ``Figure`` rendering the
  per-side limit bars + current flow lines for one element.
* :func:`build_operational_limits_view_model` — the composer that
  drives the whole tab. Returns ``None`` when the network carries no
  operational limits, otherwise an :class:`OperationalLimitsViewModel`
  with everything the host needs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import plotly.graph_objects as go

from iidm_viewer.powsybl_worker import NetworkProxy


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_DOUBLE: float = 1.7e308  # pypowsybl sentinel for "no limit"


# ---------------------------------------------------------------------------
# Pure formatters
# ---------------------------------------------------------------------------
def side_label(side: str) -> str:
    """Human-friendly label for the pypowsybl side enum."""
    return "Side 1" if side == "ONE" else "Side 2"


def duration_label(d: int) -> str:
    """Human-friendly label for an ``acceptable_duration`` value.

    ``-1`` is the pypowsybl sentinel for "permanent"; positive values
    are seconds.
    """
    if d == -1:
        return "Permanent"
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}min"
    return f"{d // 3600}h"


# ---------------------------------------------------------------------------
# Worker-routed pypowsybl fetchers
# ---------------------------------------------------------------------------
def _branch_dataframes(network):
    """Yield ``(lines_df, 2wt_df)`` directly from pypowsybl.

    Each fetch is wrapped in a try/except so that a single failure (no
    transformers in this network, attribute change in pypowsybl, etc.)
    doesn't take the whole helper down — the caller can still see
    whichever frame did come back.
    """
    for method_name in ("get_lines", "get_2_windings_transformers"):
        try:
            df = getattr(network, method_name)(all_attributes=True)
        except Exception:
            df = pd.DataFrame()
        yield df


def get_current_flows(network: NetworkProxy) -> dict[str, dict[str, float]]:
    """Return ``{element_id: {'i1': …, 'i2': …}}`` for lines + 2WTs."""
    flows: dict[str, dict[str, float]] = {}
    for df in _branch_dataframes(network):
        if df.empty or "i1" not in df.columns or "i2" not in df.columns:
            continue
        for idx, row in df[["i1", "i2"]].iterrows():
            flows[idx] = {"i1": row["i1"], "i2": row["i2"]}
    return flows


def get_branch_losses(network: NetworkProxy) -> dict[str, float]:
    """Return ``{element_id: losses_MW}`` for lines + 2-winding transformers.

    Active-power losses = ``p1 + p2`` (pypowsybl sign convention: both
    flows positive when entering the branch). Returns NaN where ``p1``
    or ``p2`` is NaN (typically before any load flow has run).
    """
    losses: dict[str, float] = {}
    for df in _branch_dataframes(network):
        if df.empty or "p1" not in df.columns or "p2" not in df.columns:
            continue
        for idx, row in df[["p1", "p2"]].iterrows():
            p1, p2 = row["p1"], row["p2"]
            if pd.notna(p1) and pd.notna(p2):
                losses[idx] = float(p1) + float(p2)
            else:
                losses[idx] = float("nan")
    return losses


# ---------------------------------------------------------------------------
# Loading compute
# ---------------------------------------------------------------------------
def compute_loading(
    network: NetworkProxy,
    limits_reset: pd.DataFrame,
) -> pd.DataFrame:
    """Worst-side ``loading_pct = I_actual / I_permanent_limit * 100``.

    Returns a DataFrame sorted by descending loading with columns:
    ``element_id``, ``element_name``, ``element_type``, ``side``,
    ``permanent_limit``, ``current``, ``loading_pct``, ``losses``. One
    row per element (the worst of its two sides). Empty when the
    network has no lines / 2WTs or no load flow has been run.

    No caching here — hosts that need it wrap this call with their own
    (Streamlit's tab keeps a per-``(net_key, lf_gen)`` cache).
    """
    # Permanent limits only, drop the "no limit" sentinel.
    perm = limits_reset[
        (limits_reset["acceptable_duration"] == -1)
        & (limits_reset["value"] < MAX_DOUBLE)
    ][["element_id", "side", "value", "element_type"]].copy()
    perm = perm.rename(columns={"value": "permanent_limit"})

    rows = []
    for df in _branch_dataframes(network):
        if df.empty or "i1" not in df.columns or "i2" not in df.columns:
            continue
        sub = df[["i1", "i2", "name"]] if "name" in df.columns else df[["i1", "i2"]]
        for idx, r in sub.iterrows():
            name = r["name"] if "name" in sub.columns else idx
            rows.append({"element_id": idx, "side": "ONE",
                         "current": r["i1"], "element_name": name})
            rows.append({"element_id": idx, "side": "TWO",
                         "current": r["i2"], "element_name": name})

    if not rows:
        return pd.DataFrame()

    currents = pd.DataFrame(rows)
    merged = perm.merge(currents, on=["element_id", "side"], how="inner")
    merged = merged.dropna(subset=["current"])
    merged = merged[merged["current"] > 0]
    if merged.empty:
        return pd.DataFrame()
    merged["loading_pct"] = (merged["current"] / merged["permanent_limit"]) * 100

    # Keep the worst side per element.
    idx_max = merged.groupby("element_id")["loading_pct"].idxmax()
    worst = merged.loc[idx_max].sort_values("loading_pct", ascending=False)

    # Attach per-element losses (p1 + p2).
    losses = get_branch_losses(network)
    worst["losses"] = worst["element_id"].map(losses)
    return worst.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Per-element plot
# ---------------------------------------------------------------------------
def build_element_chart(
    element_id: str,
    elem_df: pd.DataFrame,
    current_flow: Optional[dict],
) -> go.Figure:
    """Bar chart of limits by ``acceptable_duration`` for one element.

    One trace per side (Side 1 / Side 2) — the bars are grouped by
    duration label. When ``current_flow`` is provided, the actual
    ``i1`` / ``i2`` are overlaid as horizontal dashed lines so the
    user can spot which limit the current is approaching.
    """
    fig = go.Figure()

    sides = elem_df["side"].unique()
    for side in sorted(sides):
        side_df = elem_df[elem_df["side"] == side].copy()
        side_df = side_df[side_df["value"] < MAX_DOUBLE]
        side_df = side_df.sort_values("acceptable_duration")

        durations = side_df["acceptable_duration"].values
        values = side_df["value"].values
        labels = [duration_label(int(d)) for d in durations]
        names = side_df["name"].values

        hover = [f"{n}<br>{duration_label(int(d))}<br>{v:.0f} A"
                 for n, d, v in zip(names, durations, values)]

        fig.add_trace(go.Bar(
            x=labels,
            y=values,
            name=side_label(side),
            hovertext=hover,
            hoverinfo="text",
        ))

        if current_flow:
            i_key = "i1" if side == "ONE" else "i2"
            i_val = current_flow.get(i_key)
            if i_val is not None and pd.notna(i_val) and i_val > 0:
                fig.add_hline(
                    y=i_val,
                    line_dash="dash",
                    line_color="red" if side == "ONE" else "orange",
                    annotation_text=f"I {side_label(side)}: {i_val:.0f} A",
                    annotation_position="top left" if side == "ONE" else "top right",
                )

    fig.update_layout(
        title=f"Current limits — {element_id}",
        xaxis_title="Acceptable duration",
        yaxis_title="Current limit (A)",
        barmode="group",
        height=450,
    )
    return fig


# ---------------------------------------------------------------------------
# View-model composer
# ---------------------------------------------------------------------------
@dataclass
class OperationalLimitsViewModel:
    """Everything an Operational Limits tab needs in one shape.

    The composer fetches + reduces; the host renders. ``loading_df``
    is the worst-side-per-element table sorted by descending loading.
    ``display_limits_df`` is the same as ``limits_df`` minus the
    pypowsybl ``MAX_DOUBLE`` sentinel rows.
    """
    limits_df: "pd.DataFrame"                # raw, with sentinel rows
    display_limits_df: "pd.DataFrame"        # MAX_DOUBLE rows dropped
    loading_df: "pd.DataFrame"               # worst-side loading per element
    element_ids: list                         # IDs that have ≥1 displayable limit
    flows: dict                               # {id: {'i1', 'i2'}}
    losses: dict                              # {id: loss_MW}


def build_operational_limits_view_model(
    network: NetworkProxy,
    *,
    limits_df: Optional["pd.DataFrame"] = None,
    loading_df: Optional["pd.DataFrame"] = None,
    flows: Optional[dict] = None,
    losses: Optional[dict] = None,
) -> Optional[OperationalLimitsViewModel]:
    """Build the view model for the Operational Limits tab.

    Pipeline:

    1. Fetch ``get_operational_limits()`` (or use the caller-supplied
       ``limits_df`` so Streamlit can pass its session-state cache).
    2. Return ``None`` when there are no limits at all.
    3. Drop ``MAX_DOUBLE`` sentinel rows for the display frame.
    4. Compute worst-side loading per element.
    5. Surface the list of element IDs that have at least one
       displayable limit.
    6. Pre-fetch current flows + losses so hosts can render the
       per-element chart without an extra worker hop.
    """
    if limits_df is None:
        try:
            limits_df = network.get_operational_limits()
        except Exception:
            limits_df = pd.DataFrame()
    if limits_df is None or limits_df.empty:
        return None

    limits_reset = limits_df.reset_index()
    display = limits_reset[limits_reset["value"] < MAX_DOUBLE].copy()
    element_ids = list(display["element_id"].unique())

    if loading_df is None:
        loading_df = compute_loading(network, limits_reset)
    if flows is None:
        flows = get_current_flows(network)
    if losses is None:
        losses = get_branch_losses(network)

    return OperationalLimitsViewModel(
        limits_df=limits_reset,
        display_limits_df=display,
        loading_df=loading_df,
        element_ids=element_ids,
        flows=flows,
        losses=losses,
    )


# ---------------------------------------------------------------------------
# Legacy aliases — existing tests + the Streamlit tab consume the
# underscored names. Keep them re-exported so the rename can land
# without breakage.
# ---------------------------------------------------------------------------
_MAX_DOUBLE = MAX_DOUBLE
_side_label = side_label
_duration_label = duration_label
_get_current_flows = get_current_flows
_get_branch_losses = get_branch_losses
_compute_loading = compute_loading
_build_element_chart = build_element_chart
