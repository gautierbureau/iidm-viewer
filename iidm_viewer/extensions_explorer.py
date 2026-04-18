import streamlit as st

from iidm_viewer.data_explorer import _column_config, _compute_changes
from iidm_viewer.powsybl_worker import run
from iidm_viewer.state import EDITABLE_EXTENSIONS, update_extension


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

            editable_cols = [
                c for c in EDITABLE_EXTENSIONS.get(extension, [])
                if c in df.columns
            ]

            if editable_cols:
                col_config = _column_config(df, set(editable_cols))
                edited_df = st.data_editor(
                    df,
                    use_container_width=True,
                    column_config=col_config,
                    key=f"ext_editor_{extension}",
                )

                changes = _compute_changes(df, edited_df, editable_cols)
                n_changes = len(changes)
                if n_changes:
                    label = f"change{'s' if n_changes > 1 else ''}"
                    if st.button(
                        f"Apply {n_changes} {label}",
                        key=f"apply_ext_{extension}",
                    ):
                        try:
                            update_extension(network, extension, changes)
                            st.success(
                                f"Updated {n_changes} {extension} "
                                f"extension{'s' if n_changes > 1 else ''}: "
                                f"{', '.join(str(i) for i in changes.index.tolist())}"
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(f"Update failed: {e}")
            else:
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
