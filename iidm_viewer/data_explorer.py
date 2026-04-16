import pandas as pd
import streamlit as st
from iidm_viewer.network_info import COMPONENT_TYPES
from iidm_viewer.state import (
    CREATABLE_BRANCHES,
    CREATABLE_COMPONENTS,
    EDITABLE_COMPONENTS,
    LOCATOR_FIELDS,
    branch_side_locator_fields,
    create_branch_bay,
    create_component_bay,
    list_busbar_sections,
    list_node_breaker_voltage_levels,
    run_loadflow,
    update_components,
)
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


def _render_field(field: dict, key: str):
    """Render one form widget from a field spec and return its value."""
    kind = field["kind"]
    label = field["label"] + (" *" if field["required"] else "")
    help_text = field.get("help")
    default = field.get("default")
    if kind == "text":
        return st.text_input(label, value=default or "", key=key, help=help_text)
    if kind == "float":
        kw = {"value": float(default), "key": key, "help": help_text}
        if "min_value" in field:
            kw["min_value"] = float(field["min_value"])
        return st.number_input(label, **kw)
    if kind == "int":
        kw = {
            "value": int(default),
            "step": int(field.get("step", 1)),
            "key": key,
            "help": help_text,
        }
        if "min_value" in field:
            kw["min_value"] = int(field["min_value"])
        return st.number_input(label, **kw)
    if kind == "bool":
        return st.checkbox(label, value=bool(default), key=key, help=help_text)
    if kind == "select":
        options = field["options"]
        idx = options.index(default) if default in options else 0
        return st.selectbox(label, options, index=idx, key=key, help=help_text)
    raise ValueError(f"Unknown field kind {kind!r}")


def _render_generic_field_grid(fields_spec: list[dict], key_prefix: str) -> dict:
    """Render all fields in a responsive 3-column grid."""
    values: dict = {}
    for chunk_start in range(0, len(fields_spec), 3):
        chunk = fields_spec[chunk_start:chunk_start + 3]
        cols = st.columns(len(chunk))
        for col, spec in zip(cols, chunk):
            with col:
                values[spec["name"]] = _render_field(
                    spec, key=f"{key_prefix}_{spec['name']}"
                )
    return values


def _coerce_field_values(fields_spec: list[dict], raw_values: dict) -> dict:
    """Strip text fields and cast int fields; pass float/bool/select through."""
    coerced = {}
    for spec in fields_spec:
        v = raw_values.get(spec["name"])
        if spec["kind"] == "text":
            v = (v or "").strip()
        elif spec["kind"] == "int" and v is not None:
            v = int(v)
        coerced[spec["name"]] = v
    return coerced


def _render_create_component_form(network, component: str):
    """Collapsible form to create a new injection via a feeder bay.

    Registry-driven: fields come from :data:`CREATABLE_COMPONENTS` plus the
    shared locator fields. Restricted to node-breaker voltage levels, where
    pypowsybl's ``create_*_bay`` helper inserts the disconnector + breaker
    so the user never deals with node numbers.
    """
    spec = CREATABLE_COMPONENTS[component]
    singular = component.lower().rstrip("s")
    prefix = f"new_{spec['bay_function']}"

    with st.expander(f"Create a new {singular}", expanded=False):
        nb_vls = list_node_breaker_voltage_levels(network)
        if nb_vls.empty:
            st.info(
                f"{component} creation is currently limited to node-breaker "
                "voltage levels; none were found in this network."
            )
            return

        vl_labels = {
            r["id"]: f"{r['display']} ({r['nominal_v']:.0f} kV)"
            for _, r in nb_vls.iterrows()
        }
        vl_id = st.selectbox(
            "Voltage level",
            nb_vls["id"].tolist(),
            format_func=lambda i: vl_labels.get(i, i),
            key=f"{prefix}_vl",
        )

        bbs_options = list_busbar_sections(network, vl_id)
        if not bbs_options:
            st.warning(f"No busbar sections found in {vl_id}.")
            return
        bbs_id = st.selectbox(
            "Busbar section", bbs_options, key=f"{prefix}_bbs"
        )

        with st.form(key=f"{prefix}_form", clear_on_submit=False):
            raw_fields = _render_generic_field_grid(spec["fields"], prefix)
            st.markdown("---")
            raw_locator = _render_generic_field_grid(LOCATOR_FIELDS, prefix)
            submit = st.form_submit_button(f"Create {singular}")

        if not submit:
            return

        fields = _coerce_field_values(spec["fields"], raw_fields)
        fields.update(_coerce_field_values(LOCATOR_FIELDS, raw_locator))
        fields["bus_or_busbar_section_id"] = bbs_id

        # rated_s=0 is the form's "unset" sentinel; pypowsybl treats missing
        # columns as unset, so drop it rather than sending zero.
        if component == "Generators" and fields.get("rated_s") == 0.0:
            fields.pop("rated_s", None)

        try:
            create_component_bay(network, component, fields)
        except Exception as e:
            st.error(f"Create failed: {e}")
            return

        st.success(f"Created {singular} {fields['id']} on {bbs_id}.")
        st.rerun()


def _render_side_picker(network, nb_vls, vl_labels, prefix: str, side: int):
    """Render VL + busbar-section selectbox pair for one side of a branch."""
    vl_id = st.selectbox(
        f"Voltage level {side}",
        nb_vls["id"].tolist(),
        format_func=lambda i: vl_labels.get(i, i),
        key=f"{prefix}_vl_{side}",
    )
    bbs_options = list_busbar_sections(network, vl_id)
    if not bbs_options:
        st.warning(f"No busbar sections found in {vl_id}.")
        return None
    bbs_id = st.selectbox(
        f"Busbar section {side}", bbs_options, key=f"{prefix}_bbs_{side}"
    )
    return bbs_id


def _render_create_branch_form(network, component: str):
    """Collapsible form to create a new line or 2-winding transformer.

    Branches need two feeder bays, so the form shows two VL + busbar pickers
    side by side plus per-side position/direction fields. 2WTs additionally
    require that both voltage levels belong to the same substation — that's
    validated in :func:`validate_create_branch_fields`.
    """
    spec = CREATABLE_BRANCHES[component]
    singular = {
        "Lines": "line",
        "2-Winding Transformers": "2-winding transformer",
    }.get(component, component.lower())
    prefix = f"new_{spec['bay_function']}"

    with st.expander(f"Create a new {singular}", expanded=False):
        nb_vls = list_node_breaker_voltage_levels(network)
        if nb_vls.empty:
            st.info(
                f"{component} creation is currently limited to node-breaker "
                "voltage levels; none were found in this network."
            )
            return

        vl_labels = {
            r["id"]: f"{r['display']} ({r['nominal_v']:.0f} kV, sub {r['substation_id']})"
            for _, r in nb_vls.iterrows()
        }

        col1, col2 = st.columns(2)
        with col1:
            bbs1 = _render_side_picker(network, nb_vls, vl_labels, prefix, 1)
        with col2:
            bbs2 = _render_side_picker(network, nb_vls, vl_labels, prefix, 2)
        if not bbs1 or not bbs2:
            return

        with st.form(key=f"{prefix}_form", clear_on_submit=False):
            raw_fields = _render_generic_field_grid(spec["fields"], prefix)
            st.markdown("**Side 1**")
            raw_loc1 = _render_generic_field_grid(
                branch_side_locator_fields(1), prefix
            )
            st.markdown("**Side 2**")
            raw_loc2 = _render_generic_field_grid(
                branch_side_locator_fields(2), prefix
            )
            submit = st.form_submit_button(f"Create {singular}")

        if not submit:
            return

        fields = _coerce_field_values(spec["fields"], raw_fields)
        fields.update(_coerce_field_values(branch_side_locator_fields(1), raw_loc1))
        fields.update(_coerce_field_values(branch_side_locator_fields(2), raw_loc2))
        fields["bus_or_busbar_section_id_1"] = bbs1
        fields["bus_or_busbar_section_id_2"] = bbs2

        # rated_s=0 is the form's "unset" sentinel for 2WTs.
        if component == "2-Winding Transformers" and fields.get("rated_s") == 0.0:
            fields.pop("rated_s", None)

        try:
            create_branch_bay(network, component, fields)
        except Exception as e:
            st.error(f"Create failed: {e}")
            return

        st.success(
            f"Created {singular} {fields['id']} between {bbs1} and {bbs2}."
        )
        st.rerun()


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

    if component in CREATABLE_COMPONENTS:
        _render_create_component_form(network, component)
    elif component in CREATABLE_BRANCHES:
        _render_create_branch_form(network, component)

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
