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
    # widget is instantiated only for the two cases where the browser's
    # stale value would otherwise win:
    #
    # 1. A NAD/SLD click wrote selected_vl externally then called
    #    st.rerun(). The click handler sets _vl_set_by_click so we know
    #    to push that value into the widget (which still shows the old VL).
    #
    # 2. A new network was just loaded (selected_vl is None). Force the
    #    widget to options[0] so the old network's VL id is not restored.
    #
    # We must NOT override when the user directly changed the selectbox —
    # st.session_state.get(selectbox_key) returns the browser's pending
    # NEW value before the widget renders, so comparing it with current
    # (the previous run's value) would falsely fire and revert the
    # user's selection.
    vl_set_by_click = st.session_state.pop("_vl_set_by_click", False)
    if vl_set_by_click and current in options:
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
