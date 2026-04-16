import streamlit as st
from iidm_viewer.state import init_state, load_network, get_network, run_loadflow
from iidm_viewer.lf_parameters import show_lf_parameters_dialog
from iidm_viewer.components import vl_selector
from iidm_viewer.network_info import render_overview
from iidm_viewer.diagrams import render_nad_tab, render_sld_tab
from iidm_viewer.data_explorer import render_data_explorer
from iidm_viewer.extensions_explorer import render_extensions_explorer
from iidm_viewer.reactive_curves import render_reactive_curves
from iidm_viewer.operational_limits import render_operational_limits
from iidm_viewer.network_map import render_network_map


st.set_page_config(page_title="IIDM Viewer", layout="wide", page_icon="⚡")
init_state()

# The NAD's click-to-select injection rewrites the top window URL with
# ?selected_vl=VLx. Promote that into session state so the sidebar picks
# it up on the subsequent rerun.
_qp_vl = st.query_params.get("selected_vl")
if _qp_vl and st.session_state.get("selected_vl") != _qp_vl:
    st.session_state["selected_vl"] = _qp_vl

# -- Sidebar --
with st.sidebar:
    st.title("IIDM Viewer")

    uploaded = st.file_uploader(
        "Load a network file",
        type=["xiidm", "iidm"],
        key="file_uploader",
    )

    if uploaded is not None:
        # Only reload if it's a new file
        current = get_network()
        if current is None or st.session_state.get("_last_file") != uploaded.name:
            with st.spinner("Loading network..."):
                load_network(uploaded)
                st.session_state["_last_file"] = uploaded.name
            st.rerun()

    network = get_network()

    selected_vl = None
    if network is not None:
        st.divider()
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
            if st.button("\u2699\ufe0f", key="lf_params_btn", help="Load Flow Parameters"):
                show_lf_parameters_dialog()

# -- Main area --
if network is None:
    st.header("IIDM Viewer")
    st.info("Upload an XIIDM file in the sidebar to get started.")
    st.stop()

tab_overview, tab_map, tab_nad, tab_sld, tab_components, tab_extensions, tab_rcc, tab_limits = st.tabs(
    [
        "Overview",
        "Network Map",
        "Network Area Diagram",
        "Single Line Diagram",
        "Data Explorer Components",
        "Data Explorer Extensions",
        "Reactive Capability Curves",
        "Operational Limits",
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
