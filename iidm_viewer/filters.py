"""Whitelist-driven dataframe filters for the Components explorer.

The ``FILTERS`` registry and the structured filter mask helpers live
in :mod:`iidm_viewer.data_view` so the PySide6 and NiceGUI prototypes
share them. This module keeps the Streamlit-specific widget
rendering and re-exports ``FILTERS`` for callers that grep for the
symbol in this file.
"""
import pandas as pd
import streamlit as st

from iidm_viewer.caches import enrich_with_joins, get_vl_lookup  # noqa: F401 (re-exported)
from iidm_viewer.data_view import (  # noqa: F401  (re-exported)
    FILTERS,
    apply_filter_specs,
    compute_filter_widget_spec,
)


def build_vl_lookup(network) -> pd.DataFrame:
    """Thin wrapper around :func:`caches.get_vl_lookup`."""
    return get_vl_lookup(network)


def collect_filter_specs(
    df: pd.DataFrame, columns: list[str], key_prefix: str, label: str = "Filters",
) -> dict:
    """Render the Streamlit filter widgets and return the structured
    spec dict — the same shape :func:`apply_filter_specs` and
    :func:`build_data_explorer_view_model` consume.

    Splitting the spec collection from the filter application lets the
    Streamlit ``render_data_explorer`` build the host-agnostic
    :class:`~iidm_viewer.data_view.DataExplorerViewModel` directly:
    render widgets here, hand the specs to
    ``build_data_explorer_view_model`` and let the view-model run the
    full filter pipeline in one place. The legacy
    :func:`render_filters` keeps working as a one-call wrapper that
    collects specs + applies them.
    """
    available = [c for c in columns if c in df.columns]
    if not available:
        return {}

    specs: dict = {}
    with st.expander(label, expanded=False):
        for col in available:
            shape = compute_filter_widget_spec(df[col])
            widget_key = f"{key_prefix}_{col}"
            kind = shape.get("kind")

            if kind == "bool":
                choice = st.selectbox(
                    col, options=["Any", "True", "False"], key=widget_key,
                )
                if choice in ("True", "False"):
                    specs[col] = choice

            elif kind == "range":
                state = shape.get("state")
                if state == "empty":
                    st.caption(f"{col}: no data")
                    continue
                if state == "constant":
                    st.caption(f"{col}: constant value {shape['min']}")
                    continue
                lo, hi = shape["min"], shape["max"]
                sel = st.slider(
                    col, min_value=lo, max_value=hi, value=(lo, hi), key=widget_key,
                )
                if sel != (lo, hi):
                    specs[col] = sel

            elif kind == "multiselect":
                sel = st.multiselect(
                    col, options=shape["options"], default=[], key=widget_key,
                )
                if sel:
                    specs[col] = sel
            # ``skip`` (high-cardinality) -> no widget.

    return specs


def render_filters(df: pd.DataFrame, columns: list[str], key_prefix: str, label: str = "Filters") -> pd.DataFrame:
    """Render one Streamlit widget per whitelisted column and return the
    narrowed dataframe.

    Thin wrapper around :func:`collect_filter_specs` +
    :func:`~iidm_viewer.data_view.apply_filter_specs`. Kept for
    backward compatibility with extension callers that don't yet
    consume the structured specs.
    """
    specs = collect_filter_specs(df, columns, key_prefix, label)
    return apply_filter_specs(df, specs)
