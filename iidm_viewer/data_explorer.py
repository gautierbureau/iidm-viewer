import pandas as pd
import streamlit as st
from iidm_viewer.network_info import COMPONENT_TYPES
from iidm_viewer.state import (
    CREATABLE_BRANCHES,
    CREATABLE_COMPONENTS,
    CREATABLE_CONTAINERS,
    CREATABLE_EXTENSIONS,
    CREATABLE_HVDC_LINES,
    CREATABLE_TAP_CHANGERS,
    EDITABLE_COMPONENTS,
    LOCATOR_FIELDS,
    OPERATIONAL_LIMITS_TARGETS,
    OPERATIONAL_LIMIT_SIDES,
    OPERATIONAL_LIMIT_TYPES,
    REACTIVE_LIMITS_TARGETS,
    REMOVABLE_COMPONENTS,
    branch_side_locator_fields,
    create_branch_bay,
    create_component_bay,
    create_container,
    create_coupling_device,
    create_extension,
    create_hvdc_line,
    create_operational_limits,
    create_reactive_limits,
    create_secondary_voltage_control,
    create_tap_changer,
    list_busbar_sections,
    list_converter_stations,
    list_extension_candidates,
    list_extensions_for_component,
    list_node_breaker_voltage_levels,
    list_operational_limit_candidates,
    list_reactive_limit_candidates,
    list_substations_df,
    list_two_winding_transformers,
    next_free_node,
    remove_components,
    run_loadflow,
    update_components,
)
from iidm_viewer.lf_report_dialog import show_lf_report_dialog
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


def _add_to_change_log(method_name: str, changes_df: pd.DataFrame, original_df: pd.DataFrame):
    """Accumulate successfully-applied cell changes into a per-component session-state log."""
    key = f"_change_log_{method_name}"
    log: list[dict] = list(st.session_state.get(key, []))

    for element_id in changes_df.index:
        for col in changes_df.columns:
            new_val = changes_df.at[element_id, col]
            try:
                if pd.isna(new_val):
                    continue
            except (TypeError, ValueError):
                pass
            existing = next(
                (e for e in log if e["element_id"] == element_id and e["property"] == col),
                None,
            )
            if existing is None:
                before_val = original_df.at[element_id, col] if col in original_df.columns else None
                log.append({
                    "element_id": element_id,
                    "property": col,
                    "before": before_val,
                    "after": new_val,
                })
            else:
                existing["after"] = new_val
                try:
                    if existing["before"] == existing["after"]:
                        log.remove(existing)
                except Exception:
                    pass

    st.session_state[key] = log


def _render_change_log(network, component: str, method_name: str):
    """Display applied changes with individual Revert buttons below the data editor."""
    key = f"_change_log_{method_name}"
    log: list[dict] = st.session_state.get(key, [])
    if not log:
        return

    n = len(log)
    st.markdown(f"**Applied changes ({n})**")
    hdr = st.columns([3, 2, 2, 2, 1])
    for widget, label in zip(hdr, ["Element", "Property", "Before", "After", ""]):
        widget.caption(label)

    for i, entry in enumerate(list(log)):
        row = st.columns([3, 2, 2, 2, 1])
        row[0].text(str(entry["element_id"]))
        row[1].text(entry["property"])
        row[2].text(str(entry["before"]))
        row[3].text(str(entry["after"]))
        if row[4].button("Revert", key=f"revert_{method_name}_{i}"):
            before = entry["before"]
            if before is None or (isinstance(before, float) and pd.isna(before)):
                st.error(
                    f"Cannot revert {entry['property']} on {entry['element_id']}: "
                    "original value is unavailable."
                )
            else:
                revert_df = pd.DataFrame(
                    {entry["property"]: [before]},
                    index=pd.Index([entry["element_id"]]),
                )
                try:
                    update_components(network, component, revert_df)
                    log.pop(i)
                    st.session_state[key] = log
                    st.rerun()
                except Exception as e:
                    st.error(f"Revert failed: {e}")


def _add_to_removal_log(component: str, ids: list[str], snapshot_df: pd.DataFrame):
    """Record removed element ids (with full snapshot) in a per-component session-state log."""
    key = f"_removal_log_{component}"
    log: list[dict] = list(st.session_state.get(key, []))
    existing_ids = {e["element_id"] for e in log}
    for eid in ids:
        if eid not in existing_ids:
            snapshot = snapshot_df.loc[eid].to_dict() if eid in snapshot_df.index else {}
            log.append({"element_id": eid, "snapshot": snapshot})
    st.session_state[key] = log


def _render_removal_log(component: str):
    """Display removed element ids in a visually distinct section (no revert for now)."""
    key = f"_removal_log_{component}"
    log: list[dict] = st.session_state.get(key, [])
    if not log:
        return
    n = len(log)
    st.markdown(f"**:red[Removed {component.lower()} ({n})]**")
    for entry in log:
        st.caption(f"• {entry['element_id']}")


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


def _render_create_container_form(network, component: str):
    """Collapsible form to create a new substation, voltage level, or busbar section.

    Unlike injections/branches these don't use a ``_bay`` helper: they call
    the plain ``create_<type>s`` method. For Voltage Levels the user can
    optionally attach the VL to an existing substation; for Busbar Sections
    the user must pick a target node-breaker VL and the form suggests the
    next free node index.
    """
    spec = CREATABLE_CONTAINERS[component]
    singular = {
        "Substations": "substation",
        "Voltage Levels": "voltage level",
        "Busbar Sections": "busbar section",
    }[component]
    prefix = f"new_{spec['create_function']}"

    with st.expander(f"Create a new {singular}", expanded=False):
        context: dict = {}

        if component == "Voltage Levels":
            subs = list_substations_df(network)
            if subs.empty:
                st.info(
                    "No existing substations — the voltage level will be created "
                    "without a substation. Create a substation first if you want "
                    "to attach it."
                )
                context["substation_id"] = None
            else:
                options = ["(none — no substation)"] + subs["id"].tolist()
                labels = {r["id"]: r["display"] for _, r in subs.iterrows()}
                chosen = st.selectbox(
                    "Substation (optional)",
                    options,
                    format_func=lambda i: labels.get(i, i),
                    key=f"{prefix}_sub",
                )
                context["substation_id"] = None if chosen == options[0] else chosen

        if component == "Busbar Sections":
            nb_vls = list_node_breaker_voltage_levels(network)
            if nb_vls.empty:
                st.info(
                    "Busbar sections can only be created in node-breaker voltage "
                    "levels; none were found in this network."
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
            context["voltage_level_id"] = vl_id
            # Dynamic default for the node field based on the chosen VL.
            suggested = next_free_node(network, vl_id)
            fields_spec = []
            for f in spec["fields"]:
                if f["name"] == "node":
                    fields_spec.append({**f, "default": suggested})
                else:
                    fields_spec.append(f)
        else:
            fields_spec = spec["fields"]

        with st.form(key=f"{prefix}_form", clear_on_submit=False):
            raw_fields = _render_generic_field_grid(fields_spec, prefix)
            submit = st.form_submit_button(f"Create {singular}")

        if not submit:
            return

        fields = _coerce_field_values(fields_spec, raw_fields)
        fields.update(context)

        try:
            create_container(network, component, fields)
        except Exception as e:
            st.error(f"Create failed: {e}")
            return

        st.success(f"Created {singular} {fields['id']}.")
        st.rerun()


def _render_create_tap_changer_form(network):
    """Collapsible form to add a ratio or phase tap changer to an existing 2WT.

    Renders a 2WT picker, a kind picker (Ratio / Phase), the main fields from
    :data:`CREATABLE_TAP_CHANGERS`, and a variable-row data-editor for the
    per-tap steps. Steps are seeded with the spec's ``step_defaults``.
    """
    twts = list_two_winding_transformers(network)
    if not twts:
        return

    with st.expander("Create a new tap changer", expanded=False):
        col_twt, col_kind = st.columns([3, 1])
        with col_twt:
            twt_id = st.selectbox(
                "Target 2-winding transformer", twts,
                key="new_tc_twt",
            )
        with col_kind:
            kind = st.selectbox(
                "Tap changer kind", list(CREATABLE_TAP_CHANGERS.keys()),
                key="new_tc_kind",
            )

        spec = CREATABLE_TAP_CHANGERS[kind]
        prefix = f"new_tc_{kind.lower()}"

        with st.form(key=f"{prefix}_form", clear_on_submit=False):
            raw_main = _render_generic_field_grid(spec["main_fields"], prefix)

            st.markdown("**Tap steps**")
            st.caption(
                "One row per tap position (order matters). The current tap is "
                "the index `low_tap + N` where `N` is the 0-based row number."
            )
            initial_steps = pd.DataFrame(
                [spec["step_defaults"].copy() for _ in range(3)],
                columns=spec["step_columns"],
            )
            steps_edited = st.data_editor(
                initial_steps,
                num_rows="dynamic",
                use_container_width=True,
                key=f"{prefix}_steps",
            )
            submit = st.form_submit_button(f"Create {kind.lower()} tap changer")

        if not submit:
            return

        main_fields = _coerce_field_values(spec["main_fields"], raw_main)
        steps_df = pd.DataFrame(steps_edited).dropna(how="all")
        steps = [
            {col: row[col] for col in spec["step_columns"] if col in row}
            for _, row in steps_df.iterrows()
        ]

        try:
            create_tap_changer(network, kind, twt_id, main_fields, steps)
        except Exception as e:
            st.error(f"Create failed: {e}")
            return

        st.success(
            f"Created {kind.lower()} tap changer on {twt_id} ({len(steps)} steps)."
        )
        st.rerun()


def _render_create_coupling_device_form(network):
    """Collapsible form to create a coupling device (switches tying two BBS).

    Picks a node-breaker VL, then two distinct busbar sections inside it.
    pypowsybl inserts the breaker + disconnectors automatically.
    """
    with st.expander("Create a coupling device", expanded=False):
        nb_vls = list_node_breaker_voltage_levels(network)
        if nb_vls.empty:
            st.info(
                "Coupling device creation is currently limited to node-breaker "
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
            key="new_cpl_vl",
        )

        bbs_options = list_busbar_sections(network, vl_id)
        if len(bbs_options) < 2:
            st.warning(
                f"At least two busbar sections are needed in {vl_id} to add a "
                "coupling device (found "
                f"{len(bbs_options)})."
            )
            return

        with st.form(key="new_cpl_form", clear_on_submit=False):
            col1, col2 = st.columns(2)
            with col1:
                bbs1 = st.selectbox(
                    "Busbar section 1", bbs_options, index=0, key="new_cpl_bbs1"
                )
            with col2:
                bbs2 = st.selectbox(
                    "Busbar section 2", bbs_options, index=1, key="new_cpl_bbs2"
                )
            switch_prefix = st.text_input(
                "Switch prefix (optional)", value="",
                key="new_cpl_prefix",
                help="Prefix applied to the created breaker + disconnector ids.",
            )
            submit = st.form_submit_button("Create coupling device")

        if not submit:
            return

        try:
            create_coupling_device(
                network, bbs1, bbs2, switch_prefix.strip() or None
            )
        except Exception as e:
            st.error(f"Create failed: {e}")
            return

        st.success(f"Created coupling device between {bbs1} and {bbs2}.")
        st.rerun()


def _render_create_hvdc_line_form(network):
    """Collapsible form to create an HVDC line between two converter stations.

    The two endpoints are picked from the existing VSC + LCC stations.
    pypowsybl rejects stations already connected to another HVDC line; that
    error surfaces unchanged as an st.error. Matches other creation forms'
    validate-on-main-thread-then-dispatch pattern.
    """
    stations = list_converter_stations(network)
    spec = CREATABLE_HVDC_LINES
    prefix = "new_hvdc"

    with st.expander("Create a new HVDC line", expanded=False):
        if len(stations) < 2:
            st.info(
                "HVDC line creation needs at least two existing converter "
                "stations (VSC or LCC). Create stations first via their "
                "respective component views."
            )
            return

        station_ids = [sid for sid, _ in stations]
        station_labels = {sid: f"{sid} ({kind})" for sid, kind in stations}

        col1, col2 = st.columns(2)
        with col1:
            cs1 = st.selectbox(
                "Converter station 1", station_ids,
                format_func=lambda i: station_labels.get(i, i),
                key=f"{prefix}_cs1",
            )
        with col2:
            default_idx = 1 if len(station_ids) > 1 else 0
            cs2 = st.selectbox(
                "Converter station 2", station_ids, index=default_idx,
                format_func=lambda i: station_labels.get(i, i),
                key=f"{prefix}_cs2",
            )

        with st.form(key=f"{prefix}_form", clear_on_submit=False):
            raw_fields = _render_generic_field_grid(spec["fields"], prefix)
            submit = st.form_submit_button("Create HVDC line")

        if not submit:
            return

        fields = _coerce_field_values(spec["fields"], raw_fields)
        fields["converter_station1_id"] = cs1
        fields["converter_station2_id"] = cs2

        try:
            create_hvdc_line(network, fields)
        except Exception as e:
            st.error(f"Create failed: {e}")
            return

        st.success(f"Created HVDC line {fields['id']} between {cs1} and {cs2}.")
        st.rerun()


def _render_create_reactive_limits_form(network, component: str):
    """Collapsible form to attach reactive limits to a generator/VSC/battery.

    Two modes share one form (``st.radio``):

    - **min/max**: single ``min_q``/``max_q`` pair.
    - **curve**: variable-row editor of ``(p, min_q, max_q)`` points; at
      least two distinct ``p`` values are required.

    pypowsybl replaces any existing reactive limits on the target.
    """
    targets = list_reactive_limit_candidates(network, component)
    prefix = f"new_rl_{component.replace(' ', '_').lower()}"

    with st.expander("Attach reactive limits", expanded=False):
        if not targets:
            st.info(
                f"No {component.lower()} found — create one first to attach "
                "reactive limits."
            )
            return

        col_target, col_mode = st.columns([3, 2])
        with col_target:
            target_id = st.selectbox(
                "Target", targets, key=f"{prefix}_target"
            )
        with col_mode:
            mode = st.radio(
                "Kind", ["min/max", "curve"], horizontal=True,
                key=f"{prefix}_mode",
            )

        with st.form(key=f"{prefix}_form", clear_on_submit=False):
            if mode == "min/max":
                col_min, col_max = st.columns(2)
                with col_min:
                    min_q = st.number_input(
                        "min_q (MVar)", value=-100.0, key=f"{prefix}_min_q"
                    )
                with col_max:
                    max_q = st.number_input(
                        "max_q (MVar)", value=100.0, key=f"{prefix}_max_q"
                    )
                payload_preview = [{"min_q": min_q, "max_q": max_q}]
            else:
                initial_points = pd.DataFrame(
                    [
                        {"p": 0.0, "min_q": -100.0, "max_q": 100.0},
                        {"p": 100.0, "min_q": -80.0, "max_q": 80.0},
                    ]
                )
                st.caption(
                    "One row per active-power point (need at least two "
                    "distinct p values)."
                )
                points = st.data_editor(
                    initial_points,
                    num_rows="dynamic",
                    use_container_width=True,
                    key=f"{prefix}_points",
                )
                payload_preview = pd.DataFrame(points).dropna(how="all").to_dict(
                    orient="records"
                )

            submit = st.form_submit_button("Save reactive limits")

        if not submit:
            return

        try:
            create_reactive_limits(
                network,
                target_id,
                "minmax" if mode == "min/max" else "curve",
                payload_preview,
            )
        except Exception as e:
            st.error(f"Save failed: {e}")
            return

        st.success(
            f"Saved {mode} reactive limits on {target_id} ({component.lower()})."
        )
        st.rerun()


def _render_create_operational_limits_form(network, component: str):
    """Collapsible form to attach operational limits to a line/2WT/dangling line.

    Creates a whole group of limits at once (pypowsybl replaces the group on
    write). The form takes a target element, a side, a limit type, a group
    name, and a dynamic-row editor for the rows. Each row needs a
    ``value`` and an ``acceptable_duration`` (use ``-1`` for the permanent
    limit — exactly one of those is required).
    """
    targets = list_operational_limit_candidates(network, component)
    prefix = f"new_ol_{component.replace(' ', '_').replace('-', '_').lower()}"

    with st.expander("Attach operational limits", expanded=False):
        if not targets:
            st.info(
                f"No {component.lower()} found — create one first to attach "
                "operational limits."
            )
            return

        col_t, col_side, col_type = st.columns([3, 1, 2])
        with col_t:
            target_id = st.selectbox(
                "Target", targets, key=f"{prefix}_target"
            )
        with col_side:
            side = st.selectbox(
                "Side", OPERATIONAL_LIMIT_SIDES, key=f"{prefix}_side"
            )
        with col_type:
            limit_type = st.selectbox(
                "Type", OPERATIONAL_LIMIT_TYPES, key=f"{prefix}_type"
            )

        with st.form(key=f"{prefix}_form", clear_on_submit=False):
            group_name = st.text_input(
                "Group name", value="DEFAULT", key=f"{prefix}_group"
            )
            st.caption(
                "Use `acceptable_duration = -1` for the permanent limit "
                "(exactly one per group). Any positive value is a TATL "
                "(temporary admissible transmission loading), in seconds."
            )
            initial = pd.DataFrame(
                [
                    {"name": "permanent", "value": 1000.0,
                     "acceptable_duration": -1, "fictitious": False},
                    {"name": "TATL_60", "value": 1200.0,
                     "acceptable_duration": 60, "fictitious": False},
                ]
            )
            edited = st.data_editor(
                initial,
                num_rows="dynamic",
                use_container_width=True,
                key=f"{prefix}_rows",
            )
            submit = st.form_submit_button("Save operational limits")

        if not submit:
            return

        rows = pd.DataFrame(edited).dropna(subset=["value"]).to_dict(
            orient="records"
        )

        try:
            create_operational_limits(
                network,
                target_id,
                side,
                limit_type,
                rows,
                group_name=group_name or "DEFAULT",
            )
        except Exception as e:
            st.error(f"Save failed: {e}")
            return

        st.success(
            f"Saved {len(rows)} {limit_type.lower()} limit(s) on "
            f"{target_id} (side {side}, group {group_name or 'DEFAULT'})."
        )
        st.rerun()


def _render_secondary_voltage_control_form(network):
    """Collapsible two-dataframe form for the secondaryVoltageControl extension.

    Unlike the per-element extensions, secondaryVoltageControl is a
    network-level definition: a list of zones (name, target_v, pilot
    bus ids) plus a list of control units (unit_id, zone_name,
    participate). pypowsybl replaces the whole SVC definition on each
    write, so the form submits both lists together.
    """
    prefix = "new_svc_ext"
    with st.expander("Configure secondary voltage control", expanded=False):
        st.caption(
            "Define control zones and the units that participate in each. "
            "Pypowsybl replaces the whole secondaryVoltageControl "
            "extension on submit. `bus_ids` is space-separated if a zone "
            "has several pilot points."
        )

        zones_initial = pd.DataFrame(
            [{"name": "ZONE_1", "target_v": 400.0, "bus_ids": ""}]
        )
        units_initial = pd.DataFrame(
            [{"unit_id": "", "zone_name": "ZONE_1", "participate": True}]
        )

        with st.form(key=f"{prefix}_form", clear_on_submit=False):
            st.markdown("**Zones**")
            zones_edit = st.data_editor(
                zones_initial,
                num_rows="dynamic",
                use_container_width=True,
                key=f"{prefix}_zones",
            )
            st.markdown("**Control units**")
            units_edit = st.data_editor(
                units_initial,
                num_rows="dynamic",
                use_container_width=True,
                key=f"{prefix}_units",
            )
            submit = st.form_submit_button("Save secondary voltage control")

        if not submit:
            return

        zones = pd.DataFrame(zones_edit).dropna(
            subset=["name", "target_v"]
        ).to_dict(orient="records")
        units = pd.DataFrame(units_edit).dropna(
            subset=["unit_id", "zone_name"]
        ).to_dict(orient="records")

        try:
            create_secondary_voltage_control(network, zones, units)
        except Exception as e:
            st.error(f"Save failed: {e}")
            return

        st.success(
            f"Saved {len(zones)} zone(s) and {len(units)} unit(s). "
            "Note: pypowsybl 1.14 has no read-back for secondaryVoltageControl; "
            "the data persists in the XIIDM export."
        )
        st.rerun()


def _render_create_extension_form(network, component: str):
    """Collapsible form to attach an extension row to an existing element.

    The extension dropdown is filtered to the ones whose ``targets`` include
    the current component. Each extension's registered fields are rendered
    dynamically — text, number, bool, or choice.
    """
    ext_names = list_extensions_for_component(component)
    if not ext_names:
        return

    prefix = f"new_ext_{component.replace(' ', '_').replace('-', '_').lower()}"

    with st.expander("Attach extension", expanded=False):
        labels = {
            name: CREATABLE_EXTENSIONS[name]["label"] for name in ext_names
        }
        ext_name = st.selectbox(
            "Extension",
            options=ext_names,
            format_func=lambda n: f"{n} — {labels[n]}",
            key=f"{prefix}_ext",
        )
        schema = CREATABLE_EXTENSIONS[ext_name]
        detail = schema.get("detail")
        if detail:
            st.caption(detail)

        candidates = list_extension_candidates(network, ext_name, component)
        if not candidates:
            st.info(
                f"No {component.lower()} in the network — create one first."
            )
            return

        target_id = st.selectbox(
            "Target", candidates, key=f"{prefix}_{ext_name}_target"
        )

        with st.form(key=f"{prefix}_{ext_name}_form", clear_on_submit=False):
            values: dict = {}
            cols = st.columns(min(len(schema["fields"]), 3) or 1)
            for i, fdef in enumerate(schema["fields"]):
                col = cols[i % len(cols)]
                with col:
                    k = f"{prefix}_{ext_name}_{fdef['name']}"
                    label = fdef["name"] + (" *" if fdef.get("required") else "")
                    help_txt = fdef.get("help")
                    kind = fdef["kind"]
                    default = fdef.get("default")
                    if kind == "bool":
                        values[fdef["name"]] = st.checkbox(
                            label, value=bool(default), key=k, help=help_txt,
                        )
                    elif kind == "choice":
                        opts = fdef.get("options", [])
                        idx = opts.index(default) if default in opts else 0
                        values[fdef["name"]] = st.selectbox(
                            label, options=opts, index=idx, key=k, help=help_txt,
                        )
                    elif kind in ("float", "int"):
                        step = 1 if kind == "int" else 0.01
                        raw = st.number_input(
                            label,
                            value=(default if default is not None else 0.0)
                            if kind == "float"
                            else int(default if default is not None else 0),
                            step=step,
                            key=k,
                            help=help_txt,
                        )
                        values[fdef["name"]] = raw
                    else:
                        values[fdef["name"]] = st.text_input(
                            label,
                            value=str(default) if default else "",
                            key=k,
                            help=help_txt,
                        )
            submit = st.form_submit_button(f"Attach {ext_name}")

        if not submit:
            return

        try:
            create_extension(network, ext_name, target_id, values)
        except Exception as e:
            st.error(f"Attach failed: {e}")
            return

        st.success(f"Attached {ext_name} to {target_id}.")
        st.rerun()


def render_data_explorer(network, selected_vl):
    lf_status = st.session_state.pop("_lf_status_message", None)
    if lf_status:
        status_text, is_success = lf_status
        col_status, col_logs = st.columns([4, 1], gap="small")
        with col_status:
            if is_success:
                st.success(status_text)
            else:
                st.warning(status_text)
        with col_logs:
            if st.session_state.get("_lf_report_json"):
                if st.button("View Logs", key="de_lf_logs_btn", help="Load Flow Logs"):
                    show_lf_report_dialog()

    component_options = sorted(COMPONENT_TYPES.keys())
    component = st.selectbox(
        "Component type",
        options=component_options,
        index=component_options.index("Generators"),
        key="component_type_select",
    )

    method_name = COMPONENT_TYPES[component]

    _has_creation = (
        component in CREATABLE_COMPONENTS
        or component in CREATABLE_BRANCHES
        or component in CREATABLE_CONTAINERS
        or component in ("Switches", "HVDC Lines", "Voltage Levels")
        or component in REACTIVE_LIMITS_TARGETS
        or component in OPERATIONAL_LIMITS_TARGETS
        or bool(list_extensions_for_component(component))
    )

    if _has_creation:
        with st.expander("Create component and/or attach extensions", expanded=False):

            if component in CREATABLE_COMPONENTS:
                _render_create_component_form(network, component)
            elif component in CREATABLE_BRANCHES:
                _render_create_branch_form(network, component)
                if component == "2-Winding Transformers":
                    _render_create_tap_changer_form(network)
            elif component in CREATABLE_CONTAINERS:
                _render_create_container_form(network, component)

            if component == "Switches":
                _render_create_coupling_device_form(network)

            if component == "HVDC Lines":
                _render_create_hvdc_line_form(network)

            if component in REACTIVE_LIMITS_TARGETS:
                _render_create_reactive_limits_form(network, component)

            if component in OPERATIONAL_LIMITS_TARGETS:
                _render_create_operational_limits_form(network, component)

            _render_create_extension_form(network, component)

            if component == "Voltage Levels":
                _render_secondary_voltage_control_form(network)

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

            try:
                df = getattr(network, method_name)(all_attributes=True, **kwargs)
            except Exception as e:
                if filter_by_vl and "No data provided for index" in str(e):
                    st.info(f"No {component.lower()} in this voltage level.")
                    return
                raise

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

            is_removable = component in REMOVABLE_COMPONENTS

            if editable_cols:
                st.info(f"Editable properties: {', '.join(editable_cols)}")
            elif is_removable:
                st.info("No properties are editable for this component, but rows can be removed.")

            if editable_cols or is_removable:
                # Prepend a _remove checkbox column for removable components
                if is_removable:
                    df_display = df.copy()
                    df_display.insert(0, "_remove", False)
                else:
                    df_display = df

                col_config = _column_config(df_display, set(editable_cols))
                if is_removable:
                    col_config["_remove"] = st.column_config.CheckboxColumn(
                        "Remove", default=False
                    )

                edited_df = st.data_editor(
                    df_display,
                    use_container_width=True,
                    column_config=col_config,
                    key=f"editor_{method_name}",
                )

                # Separate removal selection from property edits
                if is_removable:
                    ids_to_remove = edited_df[edited_df["_remove"] == True].index.tolist()
                    edited_df_clean = edited_df.drop(columns=["_remove"])
                else:
                    ids_to_remove = []
                    edited_df_clean = edited_df

                # Exclude rows marked for removal from change detection
                if editable_cols:
                    df_for_changes = df.drop(index=ids_to_remove, errors="ignore")
                    edited_for_changes = edited_df_clean.loc[df_for_changes.index]
                    changes = _compute_changes(df_for_changes, edited_for_changes, editable_cols)
                else:
                    changes = pd.DataFrame()
                n_changes = len(changes)
                n_remove = len(ids_to_remove)

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
                            _add_to_change_log(method_name, changes, df)
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

                if n_remove:
                    label = f"element{'s' if n_remove > 1 else ''}"
                    if st.button(
                        f"Remove {n_remove} {label}",
                        key=f"remove_{method_name}",
                        type="primary",
                    ):
                        try:
                            actually_removed = remove_components(network, component, ids_to_remove)
                            _add_to_removal_log(component, actually_removed, df)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Remove failed: {e}")

                _render_change_log(network, component, method_name)
                _render_removal_log(component)
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
