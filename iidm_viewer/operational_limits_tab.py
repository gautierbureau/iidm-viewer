"""Streamlit "Operational Limits" tab.

The pypowsybl integration + pure-pandas reductions live in the
framework-agnostic :mod:`iidm_viewer.operational_limits` module so
PySide6 + NiceGUI can compose their own UI on top. This file holds
only the Streamlit rendering glue + per-session caching wrappers
around the shared compute.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from iidm_viewer.cache_backend import LOADING
from iidm_viewer.caches import (
    _cache_key,
    backend as _backend,
    get_enriched_component,
    get_operational_limits_df,
)
from iidm_viewer.filters import (
    FILTERS,
    render_filters,
)
from iidm_viewer.operational_limits import (
    MAX_DOUBLE,
    build_element_chart,
    compute_loading,
    get_branch_losses,
    get_current_flows,
)


# ---------------------------------------------------------------------------
# Streamlit-cached wrappers around the shared compute.
# ---------------------------------------------------------------------------
def _compute_loading_cached(network, limits_reset: pd.DataFrame) -> pd.DataFrame:
    """Streamlit-cached wrapper around :func:`compute_loading`.

    Cached per ``(net_key, lf_gen)`` so a tab switch / threshold-slider
    rerun doesn't re-run the worker. Invalidated automatically when
    ``invalidate_on_load_flow`` bumps ``_lf_gen``.
    """
    key = _cache_key(network)
    cached = _backend.get(LOADING)
    if cached is not None and cached.get("key") == key:
        return cached["df"]
    df = compute_loading(network, limits_reset)
    _backend.set(LOADING, {"key": key, "df": df})
    return df


def _get_filtered_element_ids(network, selected_vl) -> set[str]:
    """Apply the component-level filter widgets + VL narrow checkboxes
    to lines + 2WTs and return the surviving element IDs.

    Streamlit-only — uses ``st.checkbox`` and ``render_filters`` which
    can't be shared with the PySide6 / NiceGUI hosts. Those build their
    own filter UI on top of the shared :func:`compute_loading` output.
    """
    all_ids: set[str] = set()

    for component, method_name in [
        ("Lines", "get_lines"),
        ("2-Winding Transformers", "get_2_windings_transformers"),
    ]:
        df = get_enriched_component(network, method_name)
        if df.empty:
            continue

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
        df = render_filters(df, filter_cols, key_prefix=f"lim_flt_{component}",
                            label=f"Filter {component}")
        all_ids.update(df.index.tolist())

    return all_ids


# ---------------------------------------------------------------------------
# Tab body
# ---------------------------------------------------------------------------
def render_operational_limits(network, selected_vl):
    limits_df = get_operational_limits_df(network)

    if limits_df.empty:
        st.info("No operational limits found in this network.")
        return

    limits = limits_df.reset_index()
    display_df = limits[limits["value"] < MAX_DOUBLE].copy()

    # --- Most loaded elements ---
    st.subheader("Most loaded elements")
    loading = _compute_loading_cached(network, limits)
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

    flows = get_current_flows(network)
    current_flow = flows.get(selected_element)

    elem_losses = get_branch_losses(network).get(selected_element)
    if elem_losses is not None and pd.notna(elem_losses):
        st.metric("Active-power losses", f"{elem_losses:.3f} MW")
    else:
        st.caption("Losses unavailable (run a load flow to compute p1 + p2).")

    fig = build_element_chart(selected_element, elem_limits, current_flow)
    st.plotly_chart(fig, use_container_width=True)

    show_cols = ["side", "name", "acceptable_duration", "value", "element_type"]
    show_cols = [c for c in show_cols if c in elem_limits.columns]
    st.dataframe(
        elem_limits[show_cols].sort_values(["side", "acceptable_duration"]),
        use_container_width=True,
        hide_index=True,
    )
