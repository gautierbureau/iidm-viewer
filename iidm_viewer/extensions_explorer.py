import streamlit as st

from iidm_viewer.powsybl_worker import run


@st.cache_data(show_spinner=False)
def _extensions_names():
    def _call():
        import pypowsybl.network as pn
        return list(pn.get_extensions_names())

    return run(_call)


@st.cache_data(show_spinner=False)
def _extensions_information():
    def _call():
        import pypowsybl.network as pn
        return pn.get_extensions_information()

    return run(_call)


def render_extensions_explorer(network):
    names = _extensions_names()
    if not names:
        st.info("No extensions are available in this pypowsybl build.")
        return

    extension = st.selectbox(
        "Extension",
        options=names,
        key="extension_type_select",
    )

    info_df = _extensions_information()
    if extension in info_df.index:
        detail = info_df.loc[extension].get("detail")
        if detail:
            st.caption(detail)

    id_filter = st.text_input(
        "Filter by ID (substring, case-insensitive)",
        key=f"id_filter_ext_{extension}",
    )

    with st.spinner(f"Loading {extension} extensions..."):
        try:
            df = network.get_extensions(extension)

            if df is None or df.empty:
                st.info(f"No {extension!r} extensions found in this network.")
                return

            total = len(df)
            if id_filter:
                mask = df.index.astype(str).str.contains(
                    id_filter, case=False, na=False, regex=False
                )
                df = df[mask]

            if df.empty:
                st.info(f"No {extension!r} extensions match ID filter {id_filter!r}.")
                return

            if id_filter:
                st.caption(f"{len(df)} of {total} {extension!r} extensions")
            else:
                st.caption(f"{len(df)} {extension!r} extensions")
            st.dataframe(df, use_container_width=True)

            csv = df.to_csv()
            st.download_button(
                label=f"Download {extension} extensions as CSV",
                data=csv,
                file_name=f"extension_{extension}.csv",
                mime="text/csv",
            )
        except Exception as e:
            st.error(f"Error loading {extension} extensions: {e}")
