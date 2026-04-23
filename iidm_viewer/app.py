import streamlit as st
from iidm_viewer.state import (
    create_empty_network,
    export_network,
    get_export_formats,
    get_import_extensions,
    get_network,
    init_state,
    load_network,
    run_loadflow,
)
from iidm_viewer.lf_parameters import show_lf_parameters_dialog
from iidm_viewer.lf_report_dialog import show_lf_report_dialog
from iidm_viewer.network_reduction import show_network_reduction_dialog
from iidm_viewer.components import vl_selector
from iidm_viewer.network_info import render_overview
from iidm_viewer.diagrams import render_nad_tab, render_sld_tab
from iidm_viewer.data_explorer import render_data_explorer
from iidm_viewer.extensions_explorer import render_extensions_explorer
from iidm_viewer.reactive_curves import render_reactive_curves
from iidm_viewer.operational_limits import render_operational_limits
from iidm_viewer.network_map import render_network_map
from iidm_viewer.pmax_visualization import render_pmax_visualization
from iidm_viewer.voltage_analysis import render_voltage_analysis
from iidm_viewer.injection_map import render_injection_map
from iidm_viewer.security_analysis import render_security_analysis
from iidm_viewer.short_circuit_analysis import render_short_circuit_analysis


st.set_page_config(page_title="IIDM Viewer", layout="wide", page_icon="⚡")
init_state()


@st.dialog("Start with empty network")
def _show_blank_network_dialog():
    blank_id = st.text_input("Network ID", value="network", key="blank_network_id")
    if st.button("Create empty network", key="blank_network_btn"):
        create_empty_network(blank_id)
        # Bump the uploader key so Streamlit discards the old file and
        # doesn't re-load it over the blank network on the next rerun.
        st.session_state["file_uploader_gen"] += 1
        st.rerun()


@st.dialog("Save network")
def _show_save_network_dialog():
    network = get_network()
    if network is None:
        st.warning("No network loaded.")
        return
    fmt = st.selectbox("Export format", get_export_formats(), key="export_format_select")
    net_obj_id = id(object.__getattribute__(network, "_obj"))
    cache_key = f"_export_cache_{net_obj_id}_{fmt}"
    if cache_key not in st.session_state:
        with st.spinner(f"Preparing {fmt} export…"):
            try:
                data, ext = export_network(network, fmt)
                st.session_state[cache_key] = (data, ext)
            except Exception as exc:
                st.error(f"Export failed: {exc}")
                return
    data, ext = st.session_state[cache_key]
    if data[:5] == b'<?xml':
        mime = "text/xml; charset=utf-8"
    elif data[:1] == b'{':
        mime = "application/json"
    else:
        mime = "application/octet-stream"
    st.download_button(
        label=f"Download ({fmt})",
        data=data,
        file_name=f"network.{ext}",
        mime=mime,
        key="export_download_btn",
    )

# -- Sidebar --
with st.sidebar:
    st.title("IIDM Viewer")

    if "file_uploader_gen" not in st.session_state:
        st.session_state["file_uploader_gen"] = 0

    uploaded = st.file_uploader(
        "Load a network file",
        type=get_import_extensions(),
        key=f"file_uploader_{st.session_state['file_uploader_gen']}",
    )

    if uploaded is not None:
        # Use file_id (unique per upload event) so re-uploading the same
        # filename with different content still triggers a reload.
        current = get_network()
        if current is None or st.session_state.get("_last_file_id") != uploaded.file_id:
            with st.spinner("Loading network..."):
                load_network(uploaded)
                st.session_state["_last_file_id"] = uploaded.file_id
                st.session_state["_last_file"] = uploaded.name
            st.rerun()

    if st.button("Start with empty network", key="blank_network_open_btn", use_container_width=True):
        _show_blank_network_dialog()

    network = get_network()

    selected_vl = None
    if network is not None:
        if st.button("Network Reduction", key="network_reduction_btn", use_container_width=True):
            show_network_reduction_dialog()
        if st.button("Save network", key="save_network_btn", use_container_width=True):
            _show_save_network_dialog()
        st.markdown('<hr style="margin: 0.4rem 0"/>', unsafe_allow_html=True)
        selected_vl = vl_selector(network)
        st.divider()
        col_lf, col_params = st.columns([2, 1], gap="small")
        with col_lf:
            if st.button("Run AC Load Flow"):
                with st.spinner("Running load flow..."):
                    results = run_loadflow(network)
                status = results[0].status.name if results else "UNKNOWN"
                if status == "CONVERGED":
                    st.success(f"Load flow: {status}")
                else:
                    st.warning(f"Load flow: {status}")
        with col_params:
            if st.button("⚙️", key="lf_params_btn", help="Load Flow Parameters"):
                show_lf_parameters_dialog()
        lf_status = st.session_state.pop("_lf_status_message", None)
        if lf_status:
            status_text, is_success = lf_status
            if is_success:
                st.success(status_text)
            else:
                st.warning(status_text)
        if st.session_state.get("_lf_report_json"):
            if st.button("View Logs", key="lf_logs_btn", help="Load Flow Logs"):
                show_lf_report_dialog()

# -- Main area --
if network is None:
    st.header("IIDM Viewer")
    st.info(
        "Upload a network file in the sidebar to get started, "
        "or click \"Start from empty network\" to build one from scratch."
    )
    st.stop()

(
    tab_overview,
    tab_map,
    tab_nad,
    tab_sld,
    tab_components,
    tab_extensions,
    tab_rcc,
    tab_limits,
    tab_pmax,
    tab_voltage,
    tab_injection,
    tab_sa,
    tab_sc,
) = st.tabs(
    [
        "Overview",
        "Network Map",
        "Network Area Diagram",
        "Single Line Diagram",
        "Data Explorer Components",
        "Data Explorer Extensions",
        "Reactive Capability Curves",
        "Operational Limits",
        "Pmax Visualization",
        "Voltage Analysis",
        "Injection Map",
        "Security Analysis",
        "Short Circuit Analysis",
    ]
)

with tab_overview:
    render_overview(network)

with tab_map:
    render_network_map(network, selected_vl)

with tab_nad:
    render_nad_tab(network, selected_vl)

with tab_sld:
    render_sld_tab(network, selected_vl)

with tab_components:
    render_data_explorer(network, selected_vl)

with tab_extensions:
    render_extensions_explorer(network)

with tab_rcc:
    render_reactive_curves(network, selected_vl)

with tab_limits:
    render_operational_limits(network, selected_vl)

with tab_pmax:
    render_pmax_visualization(network, selected_vl)

with tab_voltage:
    render_voltage_analysis(network)

with tab_injection:
    render_injection_map(network)

with tab_sa:
    render_security_analysis(network)

with tab_sc:
    render_short_circuit_analysis(network)
