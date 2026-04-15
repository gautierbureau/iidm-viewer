import streamlit as st
from iidm_viewer.state import init_state, load_network, get_network
from iidm_viewer.components import vl_selector
from iidm_viewer.network_info import render_overview
from iidm_viewer.diagrams import render_nad_tab, render_sld_tab
from iidm_viewer.data_explorer import render_data_explorer


st.set_page_config(page_title="IIDM Viewer", layout="wide", page_icon="⚡")
init_state()

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

# -- Main area --
if network is None:
    st.header("IIDM Viewer")
    st.info("Upload an XIIDM file in the sidebar to get started.")
    st.stop()

tab_overview, tab_nad, tab_sld, tab_data = st.tabs(
    ["Overview", "Network Area Diagram", "Single Line Diagram", "Data Explorer"]
)

with tab_overview:
    render_overview(network)

with tab_nad:
    render_nad_tab(network, selected_vl)

with tab_sld:
    render_sld_tab(network, selected_vl)

with tab_data:
    render_data_explorer(network, selected_vl)
