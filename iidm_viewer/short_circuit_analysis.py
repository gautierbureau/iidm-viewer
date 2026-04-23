import streamlit as st
import pandas as pd

from iidm_viewer.caches import get_vl_nominal_v
from iidm_viewer.state import build_bus_faults, run_short_circuit_analysis


_FAULT_TYPES = ["THREE_PHASE", "SINGLE_PHASE_TO_GROUND"]
_STUDY_TYPES = ["SUB_TRANSIENT", "TRANSIENT"]


def _get_nominal_voltages(network) -> list[float]:
    try:
        df = get_vl_nominal_v(network)
        return sorted(df["nominal_v"].dropna().unique().tolist())
    except Exception:
        return []


def _render_config_tab(network):
    st.subheader("Fault configuration")

    fault_type = st.selectbox(
        "Fault type",
        options=_FAULT_TYPES,
        key="sc_fault_type",
        format_func=lambda v: "3-phase (THREE_PHASE)" if v == "THREE_PHASE" else "Single-phase to ground",
    )

    nom_voltages = _get_nominal_voltages(network)

    if nom_voltages:
        default_v = [v for v in nom_voltages if v >= 380.0]
        selected_voltages = st.multiselect(
            "Filter by nominal voltage (kV) — leave empty to include all",
            options=nom_voltages,
            default=default_v,
            key="sc_nominal_v_filter",
            format_func=lambda v: f"{v:.0f} kV",
        )
    else:
        selected_voltages = []
        st.info("No voltage levels found in the network.")

    nominal_v_set = set(selected_voltages) if selected_voltages else None

    faults = build_bus_faults(network, nominal_v_set, fault_type)

    if faults:
        st.caption(f"{len(faults)} bus faults to be simulated")
        with st.expander("Preview faults", expanded=False):
            st.dataframe(
                pd.DataFrame(faults),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info(
            "No buses match the current filter. "
            "Adjust the nominal voltage selection."
        )

    st.subheader("Analysis parameters")
    col1, col2 = st.columns(2)
    with col1:
        study_type = st.selectbox(
            "Study type",
            options=_STUDY_TYPES,
            key="sc_study_type",
            help="SUB_TRANSIENT uses subtransient reactances (default); TRANSIENT uses transient reactances.",
        )
        with_feeder_result = st.checkbox(
            "Compute feeder contributions",
            value=True,
            key="sc_with_feeder_result",
            help="Break down fault current by contributing feeder.",
        )
    with col2:
        with_limit_violations = st.checkbox(
            "Check limit violations",
            value=True,
            key="sc_with_limit_violations",
            help="Detect currents exceeding operational limits.",
        )
        min_voltage_drop = st.number_input(
            "Min voltage drop threshold (%)",
            min_value=0.0,
            max_value=100.0,
            value=0.0,
            step=1.0,
            key="sc_min_voltage_drop",
            help="Only report buses with a voltage drop above this threshold.",
        )

    if faults:
        if st.button("Run Short Circuit Analysis", key="sc_run_btn", type="primary"):
            sc_params = {
                "study_type": study_type,
                "with_feeder_result": with_feeder_result,
                "with_limit_violations": with_limit_violations,
                "min_voltage_drop_proportional_threshold": min_voltage_drop / 100.0,
            }
            with st.spinner(
                f"Running short circuit analysis ({len(faults)} faults)…"
            ):
                try:
                    results = run_short_circuit_analysis(network, faults, sc_params)
                    st.session_state["_sc_results"] = results
                    st.success(
                        f"Short circuit analysis complete — "
                        f"{len(faults)} faults evaluated."
                    )
                except Exception as exc:
                    st.error(f"Short circuit analysis failed: {exc}")


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
    results = st.session_state.get("_sc_results")
    if results is None:
        st.info(
            "No results yet. Configure and run a short circuit analysis "
            "in the Configuration tab."
        )
        return

    faults: list[dict] = results.get("faults", [])
    fault_results: dict = results.get("fault_results", {})

    if not fault_results:
        st.info("No fault results available.")
        return

    # Build summary table
    rows = []
    for f in faults:
        fid = f["id"]
        fr = fault_results.get(fid, {})
        viol_df: pd.DataFrame = fr.get("limit_violations", pd.DataFrame())
        pwr = fr.get("short_circuit_power_mva")
        cur = fr.get("current_kA")
        rows.append(
            {
                "Fault": fid,
                "Bus": f["element_id"],
                "Status": fr.get("status", "UNKNOWN"),
                "Fault power (MVA)": round(pwr, 1) if pwr is not None else None,
                "Fault current (kA)": round(cur, 3) if cur is not None else None,
                "Violations": 0 if viol_df.empty else len(viol_df),
            }
        )

    summary_df = pd.DataFrame(rows)

    n_failed = int((summary_df["Status"] != "CONVERGED").sum())
    n_with_viol = int((summary_df["Violations"] > 0).sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Faults simulated", len(faults))
    c2.metric("Failed / not converged", n_failed)
    c3.metric("With limit violations", n_with_viol)

    # Power filter slider (only when data is available)
    pwr_col = summary_df["Fault power (MVA)"].dropna()
    if not pwr_col.empty:
        max_pwr = float(pwr_col.max())
        pwr_threshold = st.slider(
            "Show faults with fault power ≥ (MVA)",
            min_value=0.0,
            max_value=max(max_pwr, 1.0),
            value=0.0,
            key="sc_pwr_threshold",
        )
        mask = summary_df["Fault power (MVA)"].isna() | (
            summary_df["Fault power (MVA)"] >= pwr_threshold
        )
        filtered = summary_df[mask]
    else:
        filtered = summary_df

    styled = (
        filtered.style
        .map(_style_status, subset=["Status"])
        .map(_style_violations, subset=["Violations"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # Drill-down
    st.subheader("Fault detail")

    fault_options = [f["id"] for f in faults]
    id_filter = st.text_input(
        "Filter by fault ID (substring, case-insensitive)",
        key="sc_fault_filter",
    )
    if id_filter:
        fault_options = [f for f in fault_options if id_filter.lower() in f.lower()]

    if not fault_options:
        st.info("No faults match the filter.")
        return

    selected_fault = st.selectbox(
        "Select fault",
        options=fault_options,
        key="sc_selected_fault",
    )

    fr = fault_results.get(selected_fault, {})
    status = fr.get("status", "UNKNOWN")
    pwr = fr.get("short_circuit_power_mva")
    cur = fr.get("current_kA")
    feeder_df: pd.DataFrame = fr.get("feeder_results", pd.DataFrame())
    viol_df: pd.DataFrame = fr.get("limit_violations", pd.DataFrame())

    status_color = "green" if status == "CONVERGED" else "red"
    st.markdown(f"**Status:** :{status_color}[{status}]")

    m1, m2 = st.columns(2)
    m1.metric("Fault power", f"{pwr:.1f} MVA" if pwr is not None else "N/A")
    m2.metric("Fault current", f"{cur:.3f} kA" if cur is not None else "N/A")

    if not feeder_df.empty:
        st.caption(f"Feeder contributions ({len(feeder_df)} feeders)")
        st.dataframe(feeder_df, use_container_width=True, hide_index=True)

    if not viol_df.empty:
        st.caption(f"{len(viol_df)} limit violation(s)")
        st.dataframe(viol_df, use_container_width=True, hide_index=True)
    elif status == "CONVERGED":
        st.success("No limit violations for this fault.")


def render_short_circuit_analysis(network):
    tab_config, tab_results = st.tabs(["Configuration", "Results"])

    with tab_config:
        _render_config_tab(network)

    with tab_results:
        _render_results_tab()
