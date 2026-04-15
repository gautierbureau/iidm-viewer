import streamlit as st
import pypowsybl.network as pn


def init_state():
    defaults = {
        "network": None,
        "selected_vl": None,
        "nad_depth": 1,
        "component_type": "Voltage Levels",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def load_network(uploaded_file):
    from io import BytesIO
    if uploaded_file.name.lower().endswith(".zip"):
        buf = BytesIO(uploaded_file.getbuffer())
    else:
        # pypowsybl 1.14 load_from_string / load(path) produce networks that
        # segfault across Streamlit reruns; zipping the payload and going
        # through load_from_binary_buffer is the only stable path.
        import zipfile
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(uploaded_file.name, uploaded_file.getvalue())
        buf.seek(0)
    network = pn.load_from_binary_buffer(buf)
    st.session_state.network = network
    st.session_state.selected_vl = None
    return network


def get_network():
    return st.session_state.get("network")


def get_voltage_levels_df(network):
    vls = network.get_voltage_levels(attributes=["name", "substation_id", "nominal_v"])
    vls = vls.reset_index()
    vls["display"] = vls.apply(
        lambda r: r["name"] if r["name"] else r["id"], axis=1
    )
    return vls.sort_values("display")


def filter_voltage_levels(vls_df, text):
    if not text:
        return vls_df
    mask = vls_df["display"].str.contains(text, case=False, na=False, regex=False)
    return vls_df[mask]
