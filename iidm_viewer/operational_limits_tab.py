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
from iidm_viewer.components import render_view_mode_radio
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
from iidm_viewer.variants import INITIAL_VARIANT_ID, NK_VARIANT_ID


# ---------------------------------------------------------------------------
# Streamlit-cached wrappers around the shared compute.
# ---------------------------------------------------------------------------
def _compute_loading_cached(
    network, limits_reset: pd.DataFrame,
    variant_id: str = INITIAL_VARIANT_ID,
) -> pd.DataFrame:
    """Streamlit-cached wrapper around :func:`compute_loading`.

    Cached per ``(net_key, lf_gen[variant_id], variant_id)`` so a tab
    switch / threshold-slider rerun doesn't re-run the worker.
    Invalidated automatically when ``invalidate_on_load_flow``
    bumps the relevant variant's ``_lf_gen`` slot.
    """
    key = _cache_key(network, variant_id)
    cache = _backend.get(LOADING)
    if not isinstance(cache, dict) or any(
        not isinstance(v, dict) or "key" not in v
        for v in (cache or {}).values()
    ):
        # Legacy single-entry shape (pre-N-K) — start fresh.
        cache = {}
        _backend.set(LOADING, cache)
    slot = cache.get(variant_id)
    if slot is not None and slot.get("key") == key:
        return slot["df"]
    df = compute_loading(network, limits_reset, variant_id=variant_id)
    cache[variant_id] = {"key": key, "df": df}
    _backend.set(LOADING, cache)
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
def _render_most_loaded(network, limits, threshold, variant_id, key_prefix):
    """Render the "Most loaded elements" table for ``variant_id``."""
    loading = _compute_loading_cached(network, limits, variant_id=variant_id)
    if loading.empty:
        st.info("No loading data available (run a load flow first).")
        return
    above = loading[loading["loading_pct"] >= threshold].copy()
    if above.empty:
        st.info(f"No elements loaded above {threshold}%.")
        return
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
    st.dataframe(
        styled, use_container_width=True, hide_index=True,
        key=f"{key_prefix}_most_loaded_df",
    )


def _render_element_detail(
    network, display_df, selected_element, variant_id, key_prefix,
):
    """Render the per-element chart + per-side limits table for
    ``variant_id`` against the chosen ``selected_element``."""
    elem_limits = display_df[display_df["element_id"] == selected_element]
    if elem_limits.empty:
        st.info(f"No limits found for element {selected_element!r}.")
        return

    flows = get_current_flows(network, variant_id=variant_id)
    current_flow = flows.get(selected_element)

    losses_map = get_branch_losses(network, variant_id=variant_id)
    elem_losses = losses_map.get(selected_element)
    if elem_losses is not None and pd.notna(elem_losses):
        st.metric("Active-power losses", f"{elem_losses:.3f} MW")
    else:
        st.caption("Losses unavailable (run a load flow to compute p1 + p2).")

    fig = build_element_chart(selected_element, elem_limits, current_flow)
    st.plotly_chart(
        fig, use_container_width=True,
        key=f"{key_prefix}_chart_{selected_element}",
    )

    show_cols = ["side", "name", "acceptable_duration", "value", "element_type"]
    show_cols = [c for c in show_cols if c in elem_limits.columns]
    st.dataframe(
        elem_limits[show_cols].sort_values(["side", "acceptable_duration"]),
        use_container_width=True,
        hide_index=True,
        key=f"{key_prefix}_limits_df_{selected_element}",
    )


def render_operational_limits(network, selected_vl):
    """Streamlit "Operational Limits" tab body.

    Honours the N / N-K / Side-by-side view-mode toggle: the N path
    keeps the existing Streamlit-cached pipeline; the N-K path reads
    flows + losses + loading against the N-K variant via the variant-
    aware backbone helpers. Operational limits themselves are
    variant-invariant and fetched once.
    """
    view_mode = render_view_mode_radio("_oplim_view_mode")

    limits_df = get_operational_limits_df(network)
    if limits_df.empty:
        st.info("No operational limits found in this network.")
        return

    limits = limits_df.reset_index()
    display_df = limits[limits["value"] < MAX_DOUBLE].copy()

    # --- Most loaded elements ---
    st.subheader("Most loaded elements")
    threshold = st.slider(
        "Show elements loaded above (%)",
        min_value=0, max_value=100, value=50,
        key="loading_threshold",
    )

    if view_mode == "Side-by-side":
        col_n, col_nk = st.columns(2)
        with col_n:
            st.markdown("**N (base)**")
            _render_most_loaded(network, limits, threshold,
                                INITIAL_VARIANT_ID, "oplim_n")
        with col_nk:
            st.markdown("**N-K (contingency)**")
            _render_most_loaded(network, limits, threshold,
                                NK_VARIANT_ID, "oplim_nk")
    elif view_mode == "N-K":
        _render_most_loaded(network, limits, threshold,
                            NK_VARIANT_ID, "oplim_nk")
    else:
        _render_most_loaded(network, limits, threshold,
                            INITIAL_VARIANT_ID, "oplim")

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

    if view_mode == "Side-by-side":
        col_n, col_nk = st.columns(2)
        with col_n:
            st.markdown("**N (base)**")
            _render_element_detail(network, display_df, selected_element,
                                   INITIAL_VARIANT_ID, "oplim_n")
        with col_nk:
            st.markdown("**N-K (contingency)**")
            _render_element_detail(network, display_df, selected_element,
                                   NK_VARIANT_ID, "oplim_nk")
    elif view_mode == "N-K":
        _render_element_detail(network, display_df, selected_element,
                               NK_VARIANT_ID, "oplim_nk")
    else:
        _render_element_detail(network, display_df, selected_element,
                               INITIAL_VARIANT_ID, "oplim")
