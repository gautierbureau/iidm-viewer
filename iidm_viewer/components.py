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

    # Determine what value the selectbox should show:
    #
    # 1. A NAD/SLD click set selected_vl externally and flagged it with
    #    _vl_set_by_click so we push that value into the widget key,
    #    overriding whatever the browser still shows.
    # 2. First render for this gen (key absent) — seed from current or
    #    fall back to first option.
    # 3. Normal user-driven rerun — Streamlit already updated the key
    #    from the widget interaction; leave it alone.
    #
    # We must never combine "set key via Session State API" with passing
    # index= to st.selectbox — that triggers Streamlit's
    # "created with a default value but also had its value set via the
    # Session State API" warning.
    vl_set_by_click = st.session_state.pop("_vl_set_by_click", False)
    if vl_set_by_click and current in options:
        st.session_state[selectbox_key] = current
    elif selectbox_key not in st.session_state:
        st.session_state[selectbox_key] = current if current in options else options[0]

    selected = st.selectbox(
        "Voltage Level",
        options=options,
        format_func=lambda x: label_map.get(x, x),
        key=selectbox_key,
    )
    st.session_state.selected_vl = selected
    return selected


def render_svg(svg_string, height=600):
    st_components.html(svg_string, height=height, scrolling=True)
