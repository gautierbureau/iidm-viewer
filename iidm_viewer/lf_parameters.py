"""Streamlit "Load Flow Parameters" dialog.

Pure logic (type coercion, provider-options parsing, "changed vs
default" filter, category grouping) lives in
:mod:`iidm_viewer.lf_parameters_schema` so the PySide6 and NiceGUI
prototypes share it. This file holds only the Streamlit rendering
glue.
"""
from __future__ import annotations

import streamlit as st

from iidm_viewer.lf_parameters_schema import (
    coerce_generic_value,
    coerce_provider_value,
    filter_changed_generic_params,
    filter_changed_provider_params,
    group_provider_params_by_category,
    parse_provider_options,
)
from iidm_viewer.loadflow import GENERIC_PARAMETERS as _GENERIC_PARAMS  # noqa: F401
from iidm_viewer.loadflow import get_provider_parameters_df


def _get_provider_params_info():
    """Return the provider parameters DataFrame (cached).

    Delegates the worker-routed pypowsybl fetch to
    :func:`iidm_viewer.loadflow.get_provider_parameters_df`; the
    session-state caching stays Streamlit-side.
    """
    cache = st.session_state.setdefault("_lf_provider_info", {})
    if "df" not in cache:
        cache["df"] = get_provider_parameters_df()
    return cache["df"]


def get_lf_parameters():
    """Return the current (generic_params_dict, provider_params_dict) from session state."""
    return (
        st.session_state.get("_lf_generic_params", {}),
        st.session_state.get("_lf_provider_params", {}),
    )


def _render_generic_tab():
    """Render widgets for generic LF parameters. Returns dict of values."""
    current = st.session_state.get("_lf_generic_params", {})
    new_values = {}

    for param_def in _GENERIC_PARAMS:
        name = param_def[0]
        ptype = param_def[1]
        default = param_def[2]
        desc = param_def[3]

        val = current.get(name, default)
        key = f"lf_gen_{name}"

        if ptype == "bool":
            new_values[name] = st.checkbox(desc, value=bool(val), key=key)
        elif ptype == "enum":
            options = param_def[4]
            idx = options.index(str(val)) if str(val) in options else 0
            new_values[name] = st.selectbox(desc, options=options, index=idx, key=key)
        elif ptype == "float":
            new_values[name] = st.number_input(desc, value=float(val), format="%g", key=key)

    return new_values


def _render_provider_tab():
    """Render widgets for OpenLoadFlow provider parameters. Returns dict of values."""
    info_df = _get_provider_params_info()
    current = st.session_state.get("_lf_provider_params", {})
    new_values = {}

    for category, cat_params in group_provider_params_by_category(info_df):
        with st.expander(category, expanded=False):
            for name, row in cat_params.iterrows():
                ptype = row["type"]
                default = row["default"]
                desc = row["description"]
                val = current.get(name, default)
                key = f"lf_prov_{name}"

                if ptype == "BOOLEAN":
                    bool_val = coerce_provider_value(ptype, val, default)
                    new_values[name] = st.checkbox(
                        f"{name}", value=bool_val, key=key, help=desc,
                    )
                elif ptype == "INTEGER":
                    int_val = coerce_provider_value(ptype, val, default)
                    new_values[name] = st.number_input(
                        f"{name}", value=int_val, step=1, key=key, help=desc,
                    )
                elif ptype == "DOUBLE":
                    float_val = coerce_provider_value(ptype, val, default)
                    new_values[name] = st.number_input(
                        f"{name}", value=float_val, format="%g", key=key, help=desc,
                    )
                elif ptype == "STRING":
                    options = parse_provider_options(row.get("possible_values"))
                    if options:
                        idx = options.index(str(val)) if str(val) in options else 0
                        new_values[name] = st.selectbox(
                            f"{name}", options=options, index=idx, key=key, help=desc,
                        )
                    else:
                        new_values[name] = st.text_input(
                            f"{name}", value=str(val) if val else "", key=key,
                            help=desc,
                        )
                else:
                    new_values[name] = st.text_input(
                        f"{name}", value=str(val) if val else "", key=key, help=desc,
                    )

    return new_values


@st.dialog("Load Flow Parameters", width="large")
def show_lf_parameters_dialog():
    tab_generic, tab_provider = st.tabs(["Generic Parameters", "OpenLoadFlow Parameters"])

    with tab_generic:
        generic_values = _render_generic_tab()

    with tab_provider:
        provider_values = _render_provider_tab()

    if st.button("Save", key="lf_params_save"):
        st.session_state["_lf_generic_params"] = filter_changed_generic_params(
            generic_values,
        )
        st.session_state["_lf_provider_params"] = filter_changed_provider_params(
            provider_values, _get_provider_params_info(),
        )
        st.rerun()
