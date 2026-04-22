"""Whitelist-driven dataframe filters for the Components explorer.

Each component type declares the columns it wants filterable in `FILTERS`.
`enrich_with_joins` adds voltage-level- and substation-derived columns
(`nominal_v`, `country`, `nominal_v1`/`nominal_v2`, `country1`/`country2`)
so those can sit in the whitelist alongside the component's own columns.
"""
import pandas as pd
import streamlit as st

from iidm_viewer.caches import enrich_with_joins, get_vl_lookup


FILTERS: dict[str, list[str]] = {
    "Generators": [
        "nominal_v", "country", "energy_source",
        "min_p", "max_p", "target_p",
        "voltage_regulator_on", "connected",
    ],
    "Loads": ["nominal_v", "country", "type", "p0", "connected"],
    "Batteries": ["nominal_v", "country", "min_p", "max_p", "connected"],
    "Voltage Levels": ["nominal_v", "country", "topology_kind"],
    "Substations": ["country", "TSO"],
    "Buses": ["nominal_v", "v_mag", "connected_component"],
    "Busbar Sections": ["nominal_v", "connected"],
    "Lines": ["nominal_v1", "nominal_v2", "p1", "connected1", "connected2"],
    "2-Winding Transformers": ["nominal_v1", "nominal_v2", "rated_s"],
    "Shunt Compensators": ["nominal_v", "model_type", "connected"],
    "Static VAR Compensators": ["nominal_v", "connected"],
    "VSC Converter Stations": ["nominal_v", "connected"],
    "LCC Converter Stations": ["nominal_v", "connected"],
    "Switches": ["nominal_v", "kind", "open"],
    "Dangling Lines": ["nominal_v", "connected"],
}


def build_vl_lookup(network) -> pd.DataFrame:
    """Thin wrapper around :func:`caches.get_vl_lookup`."""
    return get_vl_lookup(network)


def render_filters(df: pd.DataFrame, columns: list[str], key_prefix: str, label: str = "Filters") -> pd.DataFrame:
    """Render a filter widget per whitelisted column and return the narrowed df.

    Numeric → range slider. Bool → Any/True/False. Low-cardinality object →
    multiselect. Columns absent from the dataframe are silently skipped.
    """
    available = [c for c in columns if c in df.columns]
    if not available:
        return df

    mask = pd.Series(True, index=df.index)
    with st.expander(label, expanded=False):
        for col in available:
            series = df[col]
            dtype = series.dtype
            widget_key = f"{key_prefix}_{col}"

            if pd.api.types.is_bool_dtype(dtype):
                choice = st.selectbox(
                    col, options=["Any", "True", "False"], key=widget_key
                )
                if choice == "True":
                    mask &= series.fillna(False) == True  # noqa: E712
                elif choice == "False":
                    mask &= series.fillna(True) == False  # noqa: E712

            elif pd.api.types.is_numeric_dtype(dtype):
                clean = series.dropna()
                if clean.empty:
                    st.caption(f"{col}: no data")
                    continue
                lo, hi = float(clean.min()), float(clean.max())
                if lo == hi:
                    st.caption(f"{col}: constant value {lo}")
                    continue
                sel = st.slider(
                    col, min_value=lo, max_value=hi, value=(lo, hi), key=widget_key
                )
                if sel != (lo, hi):
                    mask &= series.between(sel[0], sel[1])

            else:
                clean = series.dropna().astype(str)
                clean = clean[clean != ""]
                uniq = sorted(clean.unique())
                if not uniq or len(uniq) > 30:
                    continue
                sel = st.multiselect(col, options=uniq, default=[], key=widget_key)
                if sel:
                    mask &= series.astype(str).isin(sel)

    return df[mask]
