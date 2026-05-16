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


def render_filters(df: pd.DataFrame, columns: list[str], key_prefix: str, label: str = "Filters") -> pd.DataFrame:
    """Render one Streamlit widget per whitelisted column and return the
    narrowed dataframe.

    Widget shape per column is decided by
    :func:`iidm_viewer.data_view.compute_filter_widget_spec`; the
    filtering itself runs through
    :func:`iidm_viewer.data_view.apply_filter_specs` so the rules
    stay byte-identical with the PySide6 + NiceGUI prototypes' own
    filter UIs.
    """
    available = [c for c in columns if c in df.columns]
    if not available:
        return df

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

    return apply_filter_specs(df, specs)
