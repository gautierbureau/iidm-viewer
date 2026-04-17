import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

from iidm_viewer.filters import (
    FILTERS,
    build_vl_lookup,
    enrich_with_joins,
    render_filters,
)


_MAX_DOUBLE = 1.7e308  # pypowsybl sentinel for "no limit"


def _get_current_flows(network) -> dict[str, dict[str, float]]:
    """Return {element_id: {'i1': ..., 'i2': ...}} for lines and transformers."""
    flows: dict[str, dict[str, float]] = {}
    for method, cols in [
        ("get_lines", ["i1", "i2"]),
        ("get_2_windings_transformers", ["i1", "i2"]),
    ]:
        try:
            df = getattr(network, method)(attributes=cols)
            for idx, row in df.iterrows():
                flows[idx] = {"i1": row["i1"], "i2": row["i2"]}
        except Exception:
            pass
    return flows


def _get_branch_losses(network) -> dict[str, float]:
    """Return {element_id: losses_MW} for lines and 2-winding transformers.

    Active-power losses = p1 + p2 (pypowsybl sign convention: both flows
    positive when entering the branch). Returns NaN where p1 or p2 is NaN
    (typically before any load flow has run).
    """
    losses: dict[str, float] = {}
    for method in ("get_lines", "get_2_windings_transformers"):
        try:
            df = getattr(network, method)(attributes=["p1", "p2"])
        except Exception:
            continue
        for idx, row in df.iterrows():
            p1, p2 = row["p1"], row["p2"]
            if pd.notna(p1) and pd.notna(p2):
                losses[idx] = float(p1) + float(p2)
            else:
                losses[idx] = float("nan")
    return losses


def _side_label(side: str) -> str:
    return "Side 1" if side == "ONE" else "Side 2"


def _duration_label(d: int) -> str:
    if d == -1:
        return "Permanent"
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}min"
    return f"{d // 3600}h"


def _build_element_chart(element_id: str, elem_df: pd.DataFrame,
                         current_flow: dict[str, float] | None) -> go.Figure:
    """Bar chart of limits by acceptable_duration for one element, per side."""
    fig = go.Figure()

    sides = elem_df["side"].unique()
    for side in sorted(sides):
        side_df = elem_df[elem_df["side"] == side].copy()
        # Filter out sentinel "no limit" values and sort by duration
        side_df = side_df[side_df["value"] < _MAX_DOUBLE]
        side_df = side_df.sort_values("acceptable_duration")

        durations = side_df["acceptable_duration"].values
        values = side_df["value"].values
        labels = [_duration_label(int(d)) for d in durations]
        names = side_df["name"].values

        hover = [f"{n}<br>{_duration_label(int(d))}<br>{v:.0f} A"
                 for n, d, v in zip(names, durations, values)]

        fig.add_trace(go.Bar(
            x=labels,
            y=values,
            name=_side_label(side),
            hovertext=hover,
            hoverinfo="text",
        ))

        # Add current flow as a horizontal line for this side
        if current_flow:
            i_key = "i1" if side == "ONE" else "i2"
            i_val = current_flow.get(i_key)
            if i_val is not None and pd.notna(i_val) and i_val > 0:
                fig.add_hline(
                    y=i_val,
                    line_dash="dash",
                    line_color="red" if side == "ONE" else "orange",
                    annotation_text=f"I {_side_label(side)}: {i_val:.0f} A",
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


def _compute_loading(network, limits_reset: pd.DataFrame) -> pd.DataFrame:
    """Compute loading % = I_actual / I_permanent_limit for every element/side.

    Returns a DataFrame sorted by descending loading with columns:
    element_id, element_type, side, permanent_limit, current, loading_pct,
    losses.
    """
    # Permanent limits only, no sentinel
    perm = limits_reset[
        (limits_reset["acceptable_duration"] == -1)
        & (limits_reset["value"] < _MAX_DOUBLE)
    ][["element_id", "side", "value", "element_type"]].copy()
    perm = perm.rename(columns={"value": "permanent_limit"})

    # Gather actual currents
    rows = []
    for method, cols in [
        ("get_lines", ["i1", "i2", "name"]),
        ("get_2_windings_transformers", ["i1", "i2", "name"]),
    ]:
        try:
            df = getattr(network, method)(attributes=cols).reset_index()
            for _, r in df.iterrows():
                rows.append({"element_id": r["id"], "side": "ONE", "current": r["i1"], "element_name": r["name"]})
                rows.append({"element_id": r["id"], "side": "TWO", "current": r["i2"], "element_name": r["name"]})
        except Exception:
            pass

    if not rows:
        return pd.DataFrame()

    currents = pd.DataFrame(rows)
    merged = perm.merge(currents, on=["element_id", "side"], how="inner")
    merged = merged.dropna(subset=["current"])
    merged = merged[merged["current"] > 0]
    if merged.empty:
        return pd.DataFrame()
    merged["loading_pct"] = (merged["current"] / merged["permanent_limit"]) * 100

    # Keep the worst side per element
    idx_max = merged.groupby("element_id")["loading_pct"].idxmax()
    worst = merged.loc[idx_max].sort_values("loading_pct", ascending=False)

    # Attach per-element losses (p1 + p2)
    losses = _get_branch_losses(network)
    worst["losses"] = worst["element_id"].map(losses)
    return worst.reset_index(drop=True)


def _get_filtered_element_ids(network, selected_vl) -> set[str]:
    """Load lines + transformers, apply filters, return surviving element IDs."""
    vl_lookup = build_vl_lookup(network)
    all_ids: set[str] = set()

    for component, method in [
        ("Lines", "get_lines"),
        ("2-Winding Transformers", "get_2_windings_transformers"),
    ]:
        try:
            df = getattr(network, method)(all_attributes=True)
        except Exception:
            continue
        if df.empty:
            continue
        df = enrich_with_joins(df, vl_lookup)

        # VL filter — show all by default, check to restrict to selected VL
        if selected_vl:
            vl_cols = [c for c in df.columns
                       if c in ("voltage_level1_id", "voltage_level2_id")]
            if vl_cols:
                mask = pd.Series(False, index=df.index)
                for col in vl_cols:
                    mask |= df[col] == selected_vl
                vl_subset = df[mask]
                if not vl_subset.empty:
                    filter_vl = st.checkbox(
                        f"Only {component.lower()} in VL {selected_vl}",
                        value=False,
                        key=f"limits_vl_only_{component}",
                    )
                    if filter_vl:
                        df = vl_subset

        filter_cols = FILTERS.get(component, [])
        df = render_filters(df, filter_cols, key_prefix=f"lim_flt_{component}")
        all_ids.update(df.index.tolist())

    return all_ids


def render_operational_limits(network, selected_vl):
    limits_df = network.get_operational_limits()

    if limits_df.empty:
        st.info("No operational limits found in this network.")
        return

    limits = limits_df.reset_index()
    # Filter out sentinel values for display
    display_df = limits[limits["value"] < _MAX_DOUBLE].copy()

    # --- Most loaded elements ---
    st.subheader("Most loaded elements")
    loading = _compute_loading(network, limits)
    if loading.empty:
        st.info("No loading data available (run a load flow first).")
    else:
        threshold = st.slider(
            "Show elements loaded above (%)",
            min_value=0, max_value=100, value=50,
            key="loading_threshold",
        )
        above = loading[loading["loading_pct"] >= threshold].copy()
        if above.empty:
            st.info(f"No elements loaded above {threshold}%.")
        else:
            st.caption(f"{len(above)} elements above {threshold}%")

            def _color_loading(val):
                if val >= 100:
                    return "background-color: #ff4b4b; color: white"
                if val >= 80:
                    return "background-color: #ffa500; color: white"
                return ""

            show = above[["element_id", "element_name", "element_type", "side",
                          "current", "permanent_limit", "loading_pct",
                          "losses"]].copy()
            show.columns = ["Element", "Name", "Type", "Worst side",
                            "I (A)", "Permanent limit (A)", "Loading (%)",
                            "Losses (MW)"]
            show["Worst side"] = show["Worst side"].map(
                {"ONE": "Side 1", "TWO": "Side 2"})
            show["I (A)"] = show["I (A)"].round(1)
            show["Loading (%)"] = show["Loading (%)"].round(1)
            show["Losses (MW)"] = show["Losses (MW)"].round(3)

            styled = show.style.map(_color_loading, subset=["Loading (%)"])
            st.dataframe(styled, use_container_width=True, hide_index=True)

    # --- Per-element detail ---
    st.subheader("Element detail")

    # Apply component filters to narrow elements for the detail section
    filtered_ids = _get_filtered_element_ids(network, selected_vl)
    if not filtered_ids:
        st.info("No elements match the current filters.")
        return

    element_ids = [e for e in display_df["element_id"].unique()
                   if e in filtered_ids]

    id_filter = st.text_input(
        "Filter by element ID (substring, case-insensitive)",
        key="limits_id_filter",
    )
    if id_filter:
        element_ids = [e for e in element_ids
                       if id_filter.lower() in e.lower()]

    if not element_ids:
        st.info("No elements match the current filters.")
        return

    st.caption(f"{len(element_ids)} elements with limits")

    selected_element = st.selectbox(
        "Element",
        options=element_ids,
        key="limits_element_select",
    )

    elem_limits = display_df[display_df["element_id"] == selected_element]

    # Get current flows for the chart
    flows = _get_current_flows(network)
    current_flow = flows.get(selected_element)

    # Losses for this element (p1 + p2)
    elem_losses = _get_branch_losses(network).get(selected_element)
    if elem_losses is not None and pd.notna(elem_losses):
        st.metric("Active-power losses", f"{elem_losses:.3f} MW")
    else:
        st.caption("Losses unavailable (run a load flow to compute p1 + p2).")

    fig = _build_element_chart(selected_element, elem_limits, current_flow)
    st.plotly_chart(fig, use_container_width=True)

    # Show the raw limits table
    show_cols = ["side", "name", "acceptable_duration", "value", "element_type"]
    show_cols = [c for c in show_cols if c in elem_limits.columns]
    st.dataframe(
        elem_limits[show_cols].sort_values(["side", "acceptable_duration"]),
        use_container_width=True,
        hide_index=True,
    )
