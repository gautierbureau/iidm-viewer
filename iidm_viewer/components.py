import streamlit as st
import streamlit.components.v1 as st_components
from iidm_viewer.state import get_voltage_levels_df, filter_voltage_levels


def vl_selector(network):
    vls_df = get_voltage_levels_df(network)

    gen = st.session_state.get("vl_selector_gen", 0)
    filter_key = f"vl_filter_text_{gen}"
    selectbox_key = f"vl_selectbox_{gen}"

    vl_filter = st.text_input("Filter voltage levels", key=filter_key)
    filtered = filter_voltage_levels(vls_df, vl_filter)

    if filtered.empty:
        st.info("No voltage levels match the filter.")
        return None

    options = filtered["id"].tolist()
    labels = filtered["display"].tolist()
    label_map = dict(zip(options, labels))

    current = st.session_state.get("selected_vl")
    index = 0
    if current in options:
        index = options.index(current)

    # Sync the selectbox's widget state with selected_vl *before* the
    # widget is instantiated. Without this, a NAD/SLD click that writes
    # selected_vl from the tab callback is clobbered on the next rerun
    # because the sidebar runs first and the stale widget state wins.
    # When selected_vl is None (network just reloaded), force the widget
    # to the first option so the stale frontend value cannot be restored.
    if current in options and st.session_state.get(selectbox_key) != current:
        st.session_state[selectbox_key] = current
    elif current is None and options:
        st.session_state[selectbox_key] = options[0]

    selected = st.selectbox(
        "Voltage Level",
        options=options,
        index=index,
        format_func=lambda x: label_map.get(x, x),
        key=selectbox_key,
    )
    st.session_state.selected_vl = selected
    return selected


def render_svg(svg_string, height=600):
    st_components.html(svg_string, height=height, scrolling=True)
