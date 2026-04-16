import pandas as pd
import streamlit as st
from iidm_viewer.network_info import COMPONENT_TYPES
from iidm_viewer.state import EDITABLE_COMPONENTS, update_components, run_loadflow
from iidm_viewer.filters import (
    FILTERS,
    build_vl_lookup,
    enrich_with_joins,
    render_filters,
)


# Columns to promote right after 'name' for specific component types.
PRIORITY_COLUMNS: dict[str, list[str]] = {
    "Generators": ["target_p", "target_q", "target_v", "connected", "voltage_regulator_on", "p", "q", "regulated_element_id"],
    "Loads": ["p0", "q0", "connected", "p", "q"],
}


def _reorder_columns(df, component: str):
    """Move priority columns right after 'name', preserving the rest."""
    priority = PRIORITY_COLUMNS.get(component)
    if not priority or "name" not in df.columns:
        return df
    present = [c for c in priority if c in df.columns]
    if not present:
        return df
    cols = list(df.columns)
    for c in present:
        cols.remove(c)
    insert_at = cols.index("name") + 1
    for i, c in enumerate(present):
        cols.insert(insert_at + i, c)
    return df[cols]


# Component types that support voltage_level_id filtering
VL_FILTERABLE = {
    "Generators", "Loads", "Switches", "Shunt Compensators",
    "Batteries", "Busbar Sections", "Static VAR Compensators",
    "VSC Converter Stations", "LCC Converter Stations",
}


def _column_config(df: pd.DataFrame, editable_cols: set[str]) -> dict:
    """Build a column_config that disables editing on non-editable columns."""
    config = {}
    for col in df.columns:
        if col not in editable_cols:
            config[col] = st.column_config.Column(disabled=True)
    return config


def _compute_changes(original: pd.DataFrame, edited: pd.DataFrame,
                     editable_cols: list[str]) -> pd.DataFrame:
    """Return a DataFrame (indexed by element id) with only changed cells.

    Columns that didn't change for a given row are dropped so the update
    call only touches what the user actually modified.
    """
    cols = [c for c in editable_cols if c in original.columns]
    if not cols:
        return pd.DataFrame()

    orig = original[cols]
    edit = edited[cols]

    # Boolean comparison that handles NaN==NaN as equal
    diff = (orig != edit) & ~(orig.isna() & edit.isna())

    changed_rows = diff.any(axis=1)
    if not changed_rows.any():
        return pd.DataFrame()

    # For each changed row, keep only the columns that actually changed
    result = edit.loc[changed_rows].copy()
    for col in cols:
        unchanged = ~diff.loc[changed_rows, col]
        if unchanged.any():
            result.loc[unchanged, col] = None
    # Drop all-None columns then dropna columns per row is tricky;
    # instead build a sparse frame of only changed cells
    rows = []
    for idx in result.index:
        row_changes = {}
        for col in cols:
            if diff.at[idx, col]:
                row_changes[col] = edit.at[idx, col]
        if row_changes:
            rows.append(pd.Series(row_changes, name=idx))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def render_data_explorer(network, selected_vl):
    lf_status = st.session_state.pop("_lf_status_message", None)
    if lf_status:
        status_text, is_success = lf_status
        if is_success:
            st.success(status_text)
        else:
            st.warning(status_text)

    component_options = list(COMPONENT_TYPES.keys())
    component = st.selectbox(
        "Component type",
        options=component_options,
        index=component_options.index("Generators"),
        key="component_type_select",
    )

    method_name = COMPONENT_TYPES[component]

    filter_by_vl = False
    if component in VL_FILTERABLE and selected_vl:
        filter_by_vl = st.checkbox(
            f"Filter by selected VL ({selected_vl})", value=False, key="filter_by_vl"
        )

    id_filter = st.text_input(
        "Filter by ID (substring, case-insensitive)",
        key=f"id_filter_{method_name}",
    )

    with st.spinner(f"Loading {component}..."):
        try:
            kwargs = {}
            if filter_by_vl and selected_vl:
                kwargs["voltage_level_id"] = selected_vl

            df = getattr(network, method_name)(all_attributes=True, **kwargs)

            if df.empty:
                st.info(f"No {component.lower()} found in this network.")
                return

            df = enrich_with_joins(df, build_vl_lookup(network))
            df = _reorder_columns(df, component)
            total = len(df)

            df = render_filters(
                df, FILTERS.get(component, []), key_prefix=f"flt_{method_name}"
            )

            if id_filter:
                mask = df.index.astype(str).str.contains(
                    id_filter, case=False, na=False, regex=False
                )
                df = df[mask]

            if df.empty:
                st.info(f"No {component.lower()} match the current filters.")
                return

            if len(df) < total:
                st.caption(f"{len(df)} of {total} {component.lower()}")
            else:
                st.caption(f"{len(df)} {component.lower()}")

            # Determine editable columns for this component
            editable_cols: list[str] = []
            if component in EDITABLE_COMPONENTS:
                _, editable_cols = EDITABLE_COMPONENTS[component]
                editable_cols = [c for c in editable_cols if c in df.columns]

            if editable_cols:
                col_config = _column_config(df, set(editable_cols))
                edited_df = st.data_editor(
                    df,
                    use_container_width=True,
                    column_config=col_config,
                    key=f"editor_{method_name}",
                )

                changes = _compute_changes(df, edited_df, editable_cols)
                n_changes = len(changes)
                if n_changes:
                    label = f"change{'s' if n_changes > 1 else ''}"
                    col_apply, col_apply_lf, _ = st.columns([1, 2, 5], gap="small")
                    with col_apply:
                        apply_only = st.button(
                            f"Apply {n_changes} {label}",
                            key=f"apply_{method_name}",
                        )
                    with col_apply_lf:
                        apply_and_lf = st.button(
                            f"Apply {n_changes} {label} & Run Load Flow",
                            key=f"apply_lf_{method_name}",
                        )
                    if apply_only or apply_and_lf:
                        try:
                            update_components(network, component, changes)
                            st.success(
                                f"Updated {n_changes} "
                                f"{component.lower().rstrip('s') if n_changes == 1 else component.lower()}: "
                                f"{', '.join(changes.index.tolist())}"
                            )
                            if apply_and_lf:
                                with st.spinner("Running load flow..."):
                                    results = run_loadflow(network)
                                status = results[0].status.name if results else "UNKNOWN"
                                st.session_state["_lf_status_message"] = (
                                    f"Load flow: {status}",
                                    status == "CONVERGED",
                                )
                            st.rerun()
                        except Exception as e:
                            st.error(f"Update failed: {e}")
            else:
                st.dataframe(df, use_container_width=True)

            csv = df.to_csv()
            st.download_button(
                label=f"Download {component} as CSV",
                data=csv,
                file_name=f"{component.lower().replace(' ', '_')}.csv",
                mime="text/csv",
            )
        except Exception as e:
            st.error(f"Error loading {component}: {e}")
