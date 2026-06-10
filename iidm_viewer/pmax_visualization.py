"""Framework-agnostic core for the Pmax Visualization tab.

For each transmission line: ``Pmax = V1 × V2 / X`` (MW, with V in kV,
X in Ω). The operating ratio ``P/Pmax = sin(δ)`` reveals how close
the network is to the steady-state stability limit (δ → 90°  ⟹  P → Pmax).

This module is the framework-agnostic core each UI host (Streamlit
``pmax_visualization_tab``, PySide6, NiceGUI) composes into its own
widget tree. No streamlit / Qt / NiceGUI imports here — the
Streamlit rendering + per-session caching live in
:mod:`iidm_viewer.pmax_visualization_tab`.

Public API:

* :func:`compute_pmax_data` — worker-routed fetch + pure math.
  Returns a DataFrame indexed by line id, sorted by ascending margin.
* :func:`build_pangle_chart` — pure Plotly figure builder.
* :func:`build_display_dataframe` — rename + round helper used by
  every host's summary table.
* :func:`filter_by_vl` — pure VL-touch filter for the "Only lines
  connected to VL X" toggle.
* :func:`ratio_color` / :func:`margin_color` — host-agnostic colour
  classifiers (returning ``"safe"`` / ``"caution"`` / ``"warning"``)
  so each host renders the same red / orange / green semantics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from iidm_viewer.data_view import build_vl_lookup, enrich_with_joins
from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Display schema (shared across hosts)
# ---------------------------------------------------------------------------
RAW_COLUMNS: list[str] = [
    "name",
    "voltage_level1_id",
    "voltage_level2_id",
    "pmax_mw",
    "p_actual_mw",
    "p_pmax_ratio",
    "delta_deg",
    "margin_pct",
]
DISPLAY_COLUMNS: list[str] = [
    "Name",
    "VL 1",
    "VL 2",
    "Pmax (MW)",
    "P (MW)",
    "P/Pmax",
    "δ (°)",
    "Margin (%)",
]


# ---------------------------------------------------------------------------
# Fetch + compute
# ---------------------------------------------------------------------------
def _fetch_inputs(network: NetworkProxy) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One worker hop returning ``(lines, buses)`` with only the columns
    Pmax needs."""
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        lines = raw.get_lines(attributes=[
            "name", "x", "bus1_id", "bus2_id", "p1",
            "voltage_level1_id", "voltage_level2_id",
        ])
        buses = raw.get_buses(attributes=["v_mag"])
        return lines, buses

    return run(_gather)


def compute_pmax_data(network: NetworkProxy) -> pd.DataFrame:
    """Return a DataFrame with Pmax analysis for every line that has
    valid LF data.

    Requires a completed load flow (``v_mag > 0`` on both terminal
    buses). Columns: ``name``, ``voltage_level1_id``, ``voltage_level2_id``,
    ``x_ohm``, ``v1_kv``, ``v2_kv``, ``pmax_mw``, ``p_actual_mw``,
    ``p_pmax_ratio``, ``delta_deg``, ``margin_pct``.

    Indexed by ``line_id``, sorted by ascending ``margin_pct`` so the
    "most at risk" lines bubble to the top.
    """
    lines, buses = _fetch_inputs(network)
    if lines.empty or buses.empty:
        return pd.DataFrame()

    vl_lookup = build_vl_lookup(network)
    lines_en = enrich_with_joins(lines, vl_lookup)

    rows = []
    for line_id, r in lines_en.iterrows():
        x = float(r.get("x", 0) or 0)
        if abs(x) < 1e-6:
            continue

        bus1_id = r.get("bus1_id")
        bus2_id = r.get("bus2_id")
        if not bus1_id or not bus2_id:
            continue
        if bus1_id not in buses.index or bus2_id not in buses.index:
            continue

        v1 = float(buses.loc[bus1_id, "v_mag"])
        v2 = float(buses.loc[bus2_id, "v_mag"])
        if not (v1 > 0 and v2 > 0) or pd.isna(v1) or pd.isna(v2):
            continue

        # Pmax = V1_kV × V2_kV / X_Ω  [MW]
        pmax = v1 * v2 / abs(x)
        p1_raw = r.get("p1")
        p_actual = abs(float(p1_raw)) if pd.notna(p1_raw) else 0.0

        ratio = p_actual / pmax if pmax > 1e-6 else float("nan")
        if pd.notna(ratio) and 0.0 <= ratio <= 1.0:
            delta_deg = float(np.degrees(np.arcsin(ratio)))
        else:
            delta_deg = float("nan")

        margin_pct = (1.0 - ratio) * 100.0 if pd.notna(ratio) else float("nan")

        rows.append({
            "line_id": line_id,
            "name": str(r.get("name", "") or ""),
            "x_ohm": x,
            "v1_kv": v1,
            "v2_kv": v2,
            "pmax_mw": pmax,
            "p_actual_mw": p_actual,
            "p_pmax_ratio": ratio,
            "delta_deg": delta_deg,
            "margin_pct": margin_pct,
            "voltage_level1_id": str(r.get("voltage_level1_id", "") or ""),
            "voltage_level2_id": str(r.get("voltage_level2_id", "") or ""),
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).set_index("line_id")
    return df.sort_values("margin_pct", ascending=True)


# ---------------------------------------------------------------------------
# Pure DataFrame helpers
# ---------------------------------------------------------------------------
def filter_by_vl(df: pd.DataFrame, vl_id: Optional[str]) -> pd.DataFrame:
    """Return the rows whose endpoints include *vl_id*.

    Empty *vl_id* or an empty frame returns *df* unchanged. The
    callers gate the "Only lines connected to VL X" toggle on a
    non-empty result first.
    """
    if not vl_id or df.empty:
        return df
    mask = (
        (df["voltage_level1_id"] == vl_id)
        | (df["voltage_level2_id"] == vl_id)
    )
    return df[mask]


def build_display_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Rename + round *df* into the per-host summary table.

    The output preserves the index (line id) and uses
    :data:`DISPLAY_COLUMNS` for headers — same order across hosts.
    """
    if df.empty:
        return pd.DataFrame(columns=DISPLAY_COLUMNS)
    show = df[RAW_COLUMNS].copy()
    show.columns = DISPLAY_COLUMNS
    show["Pmax (MW)"] = show["Pmax (MW)"].round(1)
    show["P (MW)"] = show["P (MW)"].round(1)
    show["P/Pmax"] = show["P/Pmax"].round(3)
    show["δ (°)"] = show["δ (°)"].round(1)
    show["Margin (%)"] = show["Margin (%)"].round(1)
    return show


# ---------------------------------------------------------------------------
# Colour classifiers (host-agnostic semantics)
# ---------------------------------------------------------------------------
def ratio_color(value) -> str:
    """Return one of ``"safe"`` / ``"caution"`` / ``"warning"`` /
    ``"unknown"`` for a P/Pmax ratio. Each host maps these to its own
    theme."""
    if value is None or pd.isna(value):
        return "unknown"
    if value >= 0.8:
        return "warning"
    if value >= 0.6:
        return "caution"
    return "safe"


def margin_color(value) -> str:
    """Return one of ``"safe"`` / ``"caution"`` / ``"warning"`` /
    ``"unknown"`` for a margin %, same semantics as
    :func:`ratio_color` (margin ≤ 20 % is warning, ≤ 40 % is caution)."""
    if value is None or pd.isna(value):
        return "unknown"
    if value <= 20:
        return "warning"
    if value <= 40:
        return "caution"
    return "safe"


# ---------------------------------------------------------------------------
# Plotly chart builder (pure)
# ---------------------------------------------------------------------------
def build_pangle_chart(line_id: str, row: pd.Series) -> go.Figure:
    """Return a Plotly figure showing the P-δ characteristic for one line."""
    pmax = row["pmax_mw"]
    p_actual = row["p_actual_mw"]
    delta_deg = row["delta_deg"]
    ratio = row["p_pmax_ratio"]

    delta_arr = np.linspace(0, 90, 270)
    p_curve = pmax * np.sin(np.radians(delta_arr))

    fig = go.Figure()

    # Background stability zones (by P/Pmax thresholds mapped to angle)
    for lo_r, hi_r, color, label in [
        (0.0, 0.6, "rgba(0,180,0,0.08)", "Safe < 60%"),
        (0.6, 0.8, "rgba(255,165,0,0.12)", "Caution 60–80%"),
        (0.8, 1.0, "rgba(220,0,0,0.12)", "Warning 80–100%"),
    ]:
        a0 = float(np.degrees(np.arcsin(lo_r)))
        a1 = float(np.degrees(np.arcsin(hi_r)))
        fig.add_vrect(
            x0=a0, x1=a1,
            fillcolor=color,
            layer="below",
            line_width=0,
            annotation_text=label,
            annotation_position="top right",
            annotation_font_size=10,
        )

    # P-δ curve
    fig.add_trace(go.Scatter(
        x=delta_arr,
        y=p_curve,
        mode="lines",
        line=dict(color="rgb(99, 110, 250)", width=2),
        name="P = Pmax × sin(δ)",
    ))

    # Pmax reference line
    fig.add_hline(
        y=pmax,
        line_dash="dot",
        line_color="grey",
        annotation_text=f"Pmax = {pmax:.0f} MW",
        annotation_position="top right",
    )

    # Operating point
    if pd.notna(delta_deg) and pd.notna(p_actual) and p_actual > 0:
        kind = ratio_color(ratio)
        op_color = {
            "warning": "red",
            "caution": "orange",
            "safe": "green",
        }.get(kind, "grey")
        fig.add_trace(go.Scatter(
            x=[delta_deg],
            y=[p_actual],
            mode="markers",
            marker=dict(size=14, color=op_color, symbol="circle"),
            name=f"Operating point  δ={delta_deg:.1f}°  P/Pmax={ratio:.1%}",
        ))
        fig.add_vline(
            x=delta_deg,
            line_dash="dash",
            line_color=op_color,
            line_width=1,
        )

    fig.update_layout(
        title=f"Power-Angle Characteristic — {line_id}",
        xaxis_title="Angle δ (degrees)",
        yaxis_title="Active Power (MW)",
        xaxis=dict(range=[0, 90], tickvals=list(range(0, 91, 10))),
        yaxis=dict(range=[0, pmax * 1.15]),
        showlegend=True,
        height=480,
    )
    return fig


# ---------------------------------------------------------------------------
# Back-compat aliases (existing tests + Streamlit module import these
# leading-underscore names — keep them so the rename is silent for
# external callers).
# ---------------------------------------------------------------------------
_compute_pmax_data = compute_pmax_data
_build_pangle_chart = build_pangle_chart


# ---------------------------------------------------------------------------
# View-model — host-agnostic state container for the Pmax Visualization tab
# ---------------------------------------------------------------------------
@dataclass
class PmaxViewModel:
    """Mutable state container for the Pmax Visualization tab.

    PySide6 + NiceGUI both keep two DataFrames (the raw ``compute_pmax_data``
    result and the optional VL-filtered subset) plus the
    ``selected_vl`` / ``only_vl`` toggle pair; this dataclass captures
    them in one shape and exposes the VL-filter pipeline as a method
    so every host renders the same rows.

    Streamlit's tab is rerun-driven and doesn't need a persistent
    view-model (its compute is cheap per rerun); a transient
    ``PmaxViewModel`` instance is still useful for the same display
    selectors when the rendering glue wants to share the helper
    surface.
    """

    unfiltered_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    only_vl: bool = False
    selected_vl: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Reset the computed DataFrame + filter toggle. Call on
        network swap / load-flow change."""
        self.unfiltered_df = pd.DataFrame()
        self.only_vl = False

    def set_data(self, df: Optional[pd.DataFrame]) -> None:
        """Store a freshly computed DataFrame (typically from
        :func:`compute_pmax_data`). Pass ``None`` to clear."""
        self.unfiltered_df = df if df is not None else pd.DataFrame()

    def set_selected_vl(self, vl_id: Optional[str]) -> None:
        """Update the VL the filter would target."""
        self.selected_vl = vl_id or None

    def set_only_vl(self, value: bool) -> None:
        """Toggle the "only lines connected to the selected VL" filter."""
        self.only_vl = bool(value)

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------
    def is_empty(self) -> bool:
        return self.unfiltered_df.empty

    def has_vl_subset(self) -> bool:
        """Whether filtering by ``selected_vl`` would yield any rows.
        Hosts use this to decide the VL-filter checkbox's visibility."""
        if not self.selected_vl or self.unfiltered_df.empty:
            return False
        return not filter_by_vl(self.unfiltered_df, self.selected_vl).empty

    def rows_df(self) -> pd.DataFrame:
        """Filtered DataFrame the host renders. Either the
        VL-narrowed subset (when ``only_vl`` is True and the VL slice
        has rows) or the full ``unfiltered_df``."""
        if (
            self.only_vl
            and self.selected_vl
            and not self.unfiltered_df.empty
        ):
            subset = filter_by_vl(self.unfiltered_df, self.selected_vl)
            if not subset.empty:
                return subset
        return self.unfiltered_df

    def line_ids(self) -> list[str]:
        """Index of :meth:`rows_df` — the per-line picker's source list."""
        return [str(x) for x in self.rows_df().index]

    def display_df(self) -> pd.DataFrame:
        """Pretty / sorted display DataFrame for the summary table."""
        return build_display_dataframe(self.rows_df())
