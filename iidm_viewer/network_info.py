import streamlit as st
import pandas as pd


COMPONENT_TYPES = {
    "Substations": "get_substations",
    "Voltage Levels": "get_voltage_levels",
    "Buses": "get_buses",
    "Busbar Sections": "get_busbar_sections",
    "Generators": "get_generators",
    "Loads": "get_loads",
    "Lines": "get_lines",
    "2-Winding Transformers": "get_2_windings_transformers",
    "3-Winding Transformers": "get_3_windings_transformers",
    "Switches": "get_switches",
    "Shunt Compensators": "get_shunt_compensators",
    "Static VAR Compensators": "get_static_var_compensators",
    "HVDC Lines": "get_hvdc_lines",
    "VSC Converter Stations": "get_vsc_converter_stations",
    "LCC Converter Stations": "get_lcc_converter_stations",
    "Batteries": "get_batteries",
    "Dangling Lines": "get_dangling_lines",
    "Tie Lines": "get_tie_lines",
}


def render_overview(network):
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Network ID", network.id)
    col2.metric("Name", network.name or "-")
    col3.metric("Format", network.source_format)
    col4.metric("Case Date", str(network.case_date.date()) if network.case_date else "-")

    st.subheader("Element Counts")
    counts = {}
    for label, method in COMPONENT_TYPES.items():
        try:
            df = getattr(network, method)()
            count = len(df)
            if count > 0:
                counts[label] = count
        except Exception:
            pass

    if counts:
        counts_df = pd.DataFrame(
            {"Component": counts.keys(), "Count": counts.values()}
        )
        cols = st.columns(4)
        for i, (label, count) in enumerate(counts.items()):
            cols[i % 4].metric(label, count)
