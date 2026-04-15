import streamlit as st
from pypowsybl.network import NadParameters, SldParameters
from iidm_viewer.components import render_svg


def render_nad_tab(network, selected_vl):
    depth = st.slider("Depth", min_value=0, max_value=10, value=1, key="nad_depth_slider")

    if not selected_vl:
        st.info("Select a voltage level in the sidebar to display the Network Area Diagram.")
        return

    with st.spinner("Generating Network Area Diagram..."):
        try:
            nad_params = NadParameters(edge_name_displayed=True, power_value_precision=1)
            svg = network.get_network_area_diagram(
                voltage_level_ids=[selected_vl],
                depth=depth,
                nad_parameters=nad_params,
            )
            render_svg(svg.svg, height=700)
        except Exception as e:
            st.error(f"Error generating NAD: {e}")


def render_sld_tab(network, selected_vl):
    if not selected_vl:
        st.info("Select a voltage level in the sidebar to display the Single Line Diagram.")
        return

    with st.spinner("Generating Single Line Diagram..."):
        try:
            sld_params = SldParameters(use_name=True, tooltip_enabled=True)
            svg = network.get_single_line_diagram(
                selected_vl,
                parameters=sld_params,
            )
            render_svg(svg.svg, height=700)
        except Exception as e:
            st.error(f"Error generating SLD: {e}")
