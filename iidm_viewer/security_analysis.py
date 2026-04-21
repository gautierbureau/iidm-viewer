import streamlit as st
import pandas as pd

from iidm_viewer.powsybl_worker import run
from iidm_viewer.state import build_n1_contingencies, run_security_analysis


_ELEMENT_TYPES = ["Lines", "2-Winding Transformers"]
_CTX_TYPES = ["ALL", "NONE", "SPECIFIC"]


def _get_nominal_voltages(network) -> list[float]:
    try:
        vls = network.get_voltage_levels(attributes=["nominal_v"])
        return sorted(vls["nominal_v"].dropna().unique().tolist())
    except Exception:
        return []


def _get_ids(network) -> dict[str, list[str]]:
    """Fetch branch/VL/3WT id lists in a single worker call, cached per network."""
    cache = st.session_state.get("_sa_id_cache")
    if cache is not None:
        return cache

    raw = object.__getattribute__(network, "_obj")

    def _gather():
        lines = list(raw.get_lines(attributes=[]).index)
        t2w = list(raw.get_2_windings_transformers(attributes=[]).index)
        t3w = list(raw.get_3_windings_transformers(attributes=[]).index)
        vls = list(raw.get_voltage_levels(attributes=[]).index)
        return {
            "branches": sorted(lines + t2w),
            "voltage_levels": sorted(vls),
            "three_windings_transformers": sorted(t3w),
        }

    cache = run(_gather)
    st.session_state["_sa_id_cache"] = cache
    return cache


def _contingencies_list() -> list[dict]:
    return st.session_state.get("_sa_contingencies", [])


# --- Configuration: Contingencies sub-tab ---

def _render_contingencies_subtab(network):
    st.subheader("Contingency configuration")

    element_type = st.selectbox(
        "Element type",
        options=_ELEMENT_TYPES,
        key="sa_element_type",
    )

    nom_voltages = _get_nominal_voltages(network)

    if nom_voltages:
        default_v = [v for v in nom_voltages if v >= 380.0]
        selected_voltages = st.multiselect(
            "Filter by nominal voltage (kV) — leave empty to include all",
            options=nom_voltages,
            default=default_v,
            key="sa_nominal_v_filter",
            format_func=lambda v: f"{v:.0f} kV",
        )
    else:
        selected_voltages = []
        st.info("No voltage levels found in the network.")

    nominal_v_set = set(selected_voltages) if selected_voltages else None

    contingencies = build_n1_contingencies(network, element_type, nominal_v_set)
    st.session_state["_sa_contingencies"] = contingencies

    if contingencies:
        st.caption(f"{len(contingencies)} N-1 contingencies to be simulated")
        with st.expander("Preview contingencies", expanded=False):
            st.dataframe(
                pd.DataFrame(contingencies),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info(
            "No elements match the current filter. "
            "Adjust the nominal voltage selection or element type."
        )


# --- Configuration: Monitored elements sub-tab ---

def _render_monitored_subtab(network):
    st.subheader("Monitored elements")
    st.caption(
        "Define extra network elements for which the analysis should return "
        "power, current and voltage results. Each row below becomes a single "
        "call to `add_monitored_elements`."
    )

    entries: list[dict] = st.session_state.setdefault("_sa_monitored", [])
    ids = _get_ids(network)
    contingency_ids = [c["id"] for c in _contingencies_list()]

    with st.form("sa_monitored_form", clear_on_submit=True):
        ctx_type = st.selectbox(
            "Contingency context",
            options=_CTX_TYPES,
            index=0,
            key="sa_mon_ctx",
            help=(
                "ALL: monitor in pre- and post-contingency states. "
                "NONE: pre-contingency only. "
                "SPECIFIC: only for the selected contingencies."
            ),
        )
        specific_cids: list[str] = []
        if ctx_type == "SPECIFIC":
            specific_cids = st.multiselect(
                "Contingencies",
                options=contingency_ids,
                key="sa_mon_cids",
            )
        branch_ids = st.multiselect(
            "Branches (lines and 2-winding transformers)",
            options=ids["branches"],
            key="sa_mon_branches",
        )
        vl_ids = st.multiselect(
            "Voltage levels",
            options=ids["voltage_levels"],
            key="sa_mon_vls",
        )
        t3w_ids = st.multiselect(
            "3-winding transformers",
            options=ids["three_windings_transformers"],
            key="sa_mon_3wt",
        )
        submitted = st.form_submit_button("Add monitored elements")

    if submitted:
        if not (branch_ids or vl_ids or t3w_ids):
            st.warning("Pick at least one branch, voltage level or 3WT.")
        elif ctx_type == "SPECIFIC" and not specific_cids:
            st.warning("Pick at least one contingency for SPECIFIC context.")
        else:
            entries.append({
                "contingency_context_type": ctx_type,
                "contingency_ids": specific_cids if ctx_type == "SPECIFIC" else None,
                "branch_ids": branch_ids or None,
                "voltage_level_ids": vl_ids or None,
                "three_windings_transformer_ids": t3w_ids or None,
            })
            st.rerun()

    if not entries:
        st.info("No monitored-element rules defined.")
        return

    st.caption(f"{len(entries)} rule(s) defined")
    for i, e in enumerate(entries):
        with st.container(border=True):
            col1, col2 = st.columns([5, 1])
            with col1:
                lines = [f"**Context:** {e['contingency_context_type']}"]
                if e["contingency_context_type"] == "SPECIFIC":
                    lines.append(f"**Contingencies:** {', '.join(e['contingency_ids'] or [])}")
                if e.get("branch_ids"):
                    lines.append(f"**Branches ({len(e['branch_ids'])}):** {', '.join(e['branch_ids'])}")
                if e.get("voltage_level_ids"):
                    lines.append(f"**Voltage levels ({len(e['voltage_level_ids'])}):** {', '.join(e['voltage_level_ids'])}")
                if e.get("three_windings_transformer_ids"):
                    lines.append(f"**3WTs ({len(e['three_windings_transformer_ids'])}):** {', '.join(e['three_windings_transformer_ids'])}")
                st.markdown("  \n".join(lines))
            with col2:
                if st.button("Remove", key=f"sa_mon_rm_{i}"):
                    entries.pop(i)
                    st.rerun()


# --- Configuration: Limit reductions sub-tab ---

def _render_limit_reductions_subtab():
    st.subheader("Limit reductions")
    st.caption(
        "Apply a reduction factor (in [0, 1]) to current limits. OpenLoadFlow "
        "currently supports `limit_type=CURRENT` and `contingency_context=ALL`."
    )

    entries: list[dict] = st.session_state.setdefault("_sa_limit_reductions", [])

    with st.form("sa_lr_form", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            value = st.number_input(
                "Value (0 – 1)",
                min_value=0.0,
                max_value=1.0,
                value=0.9,
                step=0.05,
                key="sa_lr_value",
            )
        with col2:
            permanent = st.checkbox("Permanent limits", value=True, key="sa_lr_perm")
        with col3:
            temporary = st.checkbox("Temporary limits", value=True, key="sa_lr_temp")

        col4, col5 = st.columns(2)
        with col4:
            min_dur = st.number_input(
                "Min temp. duration (s, optional)",
                min_value=0,
                value=0,
                step=60,
                key="sa_lr_min_dur",
                help="0 = no minimum",
            )
        with col5:
            max_dur = st.number_input(
                "Max temp. duration (s, optional)",
                min_value=0,
                value=0,
                step=60,
                key="sa_lr_max_dur",
                help="0 = no maximum",
            )

        col6, col7, col8 = st.columns(3)
        with col6:
            country = st.text_input("Country code (optional)", key="sa_lr_country")
        with col7:
            min_v = st.number_input(
                "Min voltage (kV, optional)",
                min_value=0.0,
                value=0.0,
                step=1.0,
                key="sa_lr_min_v",
            )
        with col8:
            max_v = st.number_input(
                "Max voltage (kV, optional)",
                min_value=0.0,
                value=0.0,
                step=1.0,
                key="sa_lr_max_v",
            )

        submitted = st.form_submit_button("Add limit reduction")

    if submitted:
        if not (permanent or temporary):
            st.warning("Pick at least one of 'Permanent' or 'Temporary'.")
        else:
            entry: dict = {
                "limit_type": "CURRENT",
                "permanent": bool(permanent),
                "temporary": bool(temporary),
                "value": float(value),
                "contingency_context": "ALL",
            }
            if temporary and min_dur > 0:
                entry["min_temporary_duration"] = int(min_dur)
            if temporary and max_dur > 0:
                entry["max_temporary_duration"] = int(max_dur)
            if country.strip():
                entry["country"] = country.strip().upper()
            if min_v > 0:
                entry["min_voltage"] = float(min_v)
            if max_v > 0:
                entry["max_voltage"] = float(max_v)
            entries.append(entry)
            st.rerun()

    if not entries:
        st.info("No limit reductions defined.")
        return

    st.caption(f"{len(entries)} reduction(s) defined")
    df = pd.DataFrame(entries)
    remove_idx = None
    for i, e in enumerate(entries):
        with st.container(border=True):
            col1, col2 = st.columns([5, 1])
            with col1:
                scope = []
                if e["permanent"]:
                    scope.append("permanent")
                if e["temporary"]:
                    scope.append("temporary")
                parts = [f"**value={e['value']}** on {' + '.join(scope)} {e['limit_type']}"]
                extras = []
                for k in ("min_temporary_duration", "max_temporary_duration",
                          "country", "min_voltage", "max_voltage"):
                    if k in e:
                        extras.append(f"{k}={e[k]}")
                if extras:
                    parts.append("  \n" + " · ".join(extras))
                st.markdown("".join(parts))
            with col2:
                if st.button("Remove", key=f"sa_lr_rm_{i}"):
                    remove_idx = i

    if remove_idx is not None:
        entries.pop(remove_idx)
        st.rerun()

    with st.expander("Preview DataFrame passed to pypowsybl", expanded=False):
        st.dataframe(df, use_container_width=True, hide_index=True)


# --- Configuration tab (run button + sub-tabs) ---

def _render_config_tab(network):
    sub_cont, sub_mon, sub_lr = st.tabs(
        ["Contingencies", "Monitored elements", "Limit reductions"]
    )

    with sub_cont:
        _render_contingencies_subtab(network)
    with sub_mon:
        _render_monitored_subtab(network)
    with sub_lr:
        _render_limit_reductions_subtab()

    st.divider()
    contingencies = _contingencies_list()
    monitored = st.session_state.get("_sa_monitored", [])
    reductions = st.session_state.get("_sa_limit_reductions", [])

    cols = st.columns(4)
    cols[0].metric("Contingencies", len(contingencies))
    cols[1].metric("Monitored rules", len(monitored))
    cols[2].metric("Limit reductions", len(reductions))

    with cols[3]:
        if st.button(
            "Run Security Analysis",
            key="sa_run_btn",
            type="primary",
            disabled=not contingencies,
        ):
            with st.spinner(
                f"Running security analysis ({len(contingencies)} contingencies)…"
            ):
                try:
                    results = run_security_analysis(
                        network,
                        contingencies,
                        monitored_elements=monitored,
                        limit_reductions=reductions,
                    )
                    st.session_state["_sa_results"] = results
                    st.success(
                        f"Security analysis complete — "
                        f"{len(contingencies)} contingencies evaluated."
                    )
                except Exception as exc:
                    st.error(f"Security analysis failed: {exc}")


def _style_status(val: str) -> str:
    if val == "CONVERGED":
        return "color: green"
    return "background-color: #ff4b4b; color: white"


def _style_violations(val: int) -> str:
    if val == 0:
        return ""
    if val >= 3:
        return "background-color: #ff4b4b; color: white"
    return "background-color: #ffa500; color: white"


def _render_monitored_pre(results: dict):
    pre_branch = results.get("pre_branch_results", pd.DataFrame())
    pre_bus = results.get("pre_bus_results", pd.DataFrame())
    pre_3wt = results.get("pre_3wt_results", pd.DataFrame())
    if pre_branch.empty and pre_bus.empty and pre_3wt.empty:
        return
    with st.expander("Pre-contingency monitored results", expanded=False):
        if not pre_branch.empty:
            st.caption("Branches (P, Q, I)")
            st.dataframe(pre_branch, use_container_width=True)
        if not pre_bus.empty:
            st.caption("Buses (voltage magnitude & angle)")
            st.dataframe(pre_bus, use_container_width=True)
        if not pre_3wt.empty:
            st.caption("3-winding transformers")
            st.dataframe(pre_3wt, use_container_width=True)


def _render_monitored_post(cr: dict):
    br = cr.get("branch_results", pd.DataFrame())
    bu = cr.get("bus_results", pd.DataFrame())
    t3 = cr.get("three_windings_transformer_results", pd.DataFrame())
    if br.empty and bu.empty and t3.empty:
        return
    st.caption("Monitored results for this contingency")
    if not br.empty:
        st.markdown("**Branches**")
        st.dataframe(br, use_container_width=True)
    if not bu.empty:
        st.markdown("**Buses**")
        st.dataframe(bu, use_container_width=True)
    if not t3.empty:
        st.markdown("**3-winding transformers**")
        st.dataframe(t3, use_container_width=True)


def _render_results_tab():
    results = st.session_state.get("_sa_results")
    if results is None:
        st.info(
            "No results yet. Configure and run a security analysis "
            "in the Configuration tab."
        )
        return

    contingencies = results.get("contingencies", [])
    pre_status = results.get("pre_status", "UNKNOWN")
    pre_violations: pd.DataFrame = results.get("pre_violations", pd.DataFrame())
    post: dict = results.get("post", {})

    # Pre-contingency summary
    st.subheader("Pre-contingency state")
    col1, col2 = st.columns(2)
    col1.metric("Base case status", pre_status)
    col2.metric(
        "Limit violations",
        0 if pre_violations.empty else len(pre_violations),
    )

    if not pre_violations.empty:
        st.caption("Pre-contingency limit violations")
        st.dataframe(pre_violations, use_container_width=True, hide_index=True)

    _render_monitored_pre(results)

    # Post-contingency summary
    st.subheader("Post-contingency results")

    if not post:
        st.info("No post-contingency results available.")
        return

    rows = []
    for c in contingencies:
        cid = c["id"]
        cr = post.get(cid, {})
        viol_df: pd.DataFrame = cr.get("limit_violations", pd.DataFrame())
        rows.append(
            {
                "Contingency": cid,
                "Element": c["element_id"],
                "Status": cr.get("status", "UNKNOWN"),
                "Violations": 0 if viol_df.empty else len(viol_df),
            }
        )

    summary_df = pd.DataFrame(rows)

    n_failed = int((summary_df["Status"] != "CONVERGED").sum())
    n_with_viol = int((summary_df["Violations"] > 0).sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Contingencies", len(contingencies))
    c2.metric("Failed / not converged", n_failed)
    c3.metric("With limit violations", n_with_viol)

    max_viol = int(summary_df["Violations"].max()) if not summary_df.empty else 0
    threshold = st.slider(
        "Show contingencies with violations ≥",
        min_value=0,
        max_value=max(max_viol, 1),
        value=0,
        key="sa_violation_threshold",
    )

    filtered = summary_df[summary_df["Violations"] >= threshold]
    styled = filtered.style.map(_style_status, subset=["Status"]).map(
        _style_violations, subset=["Violations"]
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # Drill-down
    st.subheader("Contingency detail")

    contingency_options = [c["id"] for c in contingencies]
    id_filter = st.text_input(
        "Filter by contingency ID (substring, case-insensitive)",
        key="sa_contingency_filter",
    )
    if id_filter:
        contingency_options = [
            c for c in contingency_options if id_filter.lower() in c.lower()
        ]

    if not contingency_options:
        st.info("No contingencies match the filter.")
        return

    selected_contingency = st.selectbox(
        "Select contingency",
        options=contingency_options,
        key="sa_selected_contingency",
    )

    cr = post.get(selected_contingency, {})
    status = cr.get("status", "UNKNOWN")
    viol_df = cr.get("limit_violations", pd.DataFrame())

    status_color = "green" if status == "CONVERGED" else "red"
    st.markdown(f"**Status:** :{status_color}[{status}]")

    if not viol_df.empty:
        st.caption(f"{len(viol_df)} limit violation(s)")
        st.dataframe(viol_df, use_container_width=True, hide_index=True)
    else:
        st.success("No limit violations for this contingency.")

    _render_monitored_post(cr)


def render_security_analysis(network):
    tab_config, tab_results = st.tabs(["Configuration", "Results"])

    with tab_config:
        _render_config_tab(network)

    with tab_results:
        _render_results_tab()
