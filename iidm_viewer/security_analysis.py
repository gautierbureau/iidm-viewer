import streamlit as st
import pandas as pd

from iidm_viewer.state import build_n1_contingencies, run_security_analysis


_ELEMENT_TYPES = ["Lines", "2-Winding Transformers"]


def _get_nominal_voltages(network) -> list[float]:
    try:
        vls = network.get_voltage_levels(attributes=["nominal_v"])
        return sorted(vls["nominal_v"].dropna().unique().tolist())
    except Exception:
        return []


def _render_config_tab(network):
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

    if contingencies:
        if st.button("Run Security Analysis", key="sa_run_btn", type="primary"):
            with st.spinner(
                f"Running security analysis ({len(contingencies)} contingencies)…"
            ):
                try:
                    results = run_security_analysis(network, contingencies)
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


def render_security_analysis(network):
    tab_config, tab_results = st.tabs(["Configuration", "Results"])

    with tab_config:
        _render_config_tab(network)

    with tab_results:
        _render_results_tab()
