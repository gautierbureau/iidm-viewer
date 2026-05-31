"""Streamlit "Overview" tab.

The framework-agnostic numeric work lives in
:mod:`iidm_viewer.network_info_core`; this module keeps the
per-session ``_overview_cache`` and the Streamlit rendering glue.
"""
from __future__ import annotations

import pandas as pd

# Source of truth lives in the framework-agnostic registry so the Qt and
# NiceGUI prototypes can reuse the same component map without dragging
# streamlit into their dependency graph.
from iidm_viewer.component_registry import COMPONENT_TYPES  # noqa: F401
from iidm_viewer.network_info_core import (
    branch_losses_totals,
    build_component_counts,
    build_country_totals_display,
    build_losses_by_country_display,
    build_metadata,
    build_vl_country_map,
    country_totals,
    country_totals_has_lf,
    losses_by_country,
)


# Back-compat aliases — the existing test suite imports these
# leading-underscore names. The implementations live in the shared core
# now so PySide6 + NiceGUI hosts can reuse them.
_branch_losses_totals = branch_losses_totals
_build_vl_country_map = build_vl_country_map
_losses_by_country = losses_by_country
_country_totals = country_totals


def _net_key(network) -> int:
    return id(object.__getattribute__(network, "_obj"))


def _get_overview_data(network) -> tuple:
    """Compute and cache Overview tab data per (net_key, lf_gen).

    Returns ``(country_df, losses, by_country, counts)`` — same shape
    the previous Streamlit implementation exposed, used by
    :func:`render_overview` below.
    """
    import streamlit as st

    net_key = _net_key(network)
    lf_gen = st.session_state.get("_lf_gen", 0)

    cached = st.session_state.get("_overview_cache")
    if (
        cached is not None
        and cached.get("net_key") == net_key
        and cached.get("lf_gen") == lf_gen
    ):
        return cached["data"]

    country_df = country_totals(network)
    losses = branch_losses_totals(network)
    by_country = losses_by_country(network)
    counts = build_component_counts(network)

    data = (country_df, losses, by_country, counts)
    st.session_state["_overview_cache"] = {
        "net_key": net_key,
        "lf_gen": lf_gen,
        "data": data,
    }
    return data


def render_overview(network):
    import streamlit as st

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Network ID", network.id)
    col2.metric("Name", network.name or "-")
    col3.metric("Format", network.source_format)
    col4.metric(
        "Case Date",
        str(network.case_date.date()) if network.case_date else "-",
    )

    country_df, losses, by_country, counts = _get_overview_data(network)

    st.subheader("Generation and Consumption by Country")
    if country_df.empty:
        st.info("No generation or consumption data available.")
    else:
        display = build_country_totals_display(country_df)
        if not country_totals_has_lf(country_df):
            st.caption("Actual values populate once a load flow has run.")
        st.dataframe(display, use_container_width=True, hide_index=True)

    st.subheader("Network Losses")
    if not losses.get("has_data"):
        st.info("No loss data available (run a load flow first).")
    else:
        lc1, lc2, lc3 = st.columns(3)
        lc1.metric("Total losses", f"{losses['total']:.2f} MW")
        lc2.metric("Line losses", f"{losses['lines']:.2f} MW")
        lc3.metric("Transformer losses", f"{losses['transformers']:.2f} MW")

        if not by_country.empty:
            losses_df = build_losses_by_country_display(by_country)
            st.caption("Losses by country — cross-border branches split 50/50.")
            st.dataframe(losses_df, use_container_width=True, hide_index=True)

    st.subheader("Component Statistics")
    with st.expander("Component statistics", expanded=False):
        if counts:
            cols = st.columns(4)
            for i, (label, count) in enumerate(counts.items()):
                cols[i % 4].metric(label, count)
        else:
            st.info("No components found in this network.")
