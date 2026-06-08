import pandas as pd
import streamlit as st

from iidm_viewer.caches import get_extension_df
from iidm_viewer.data_explorer import _column_config, _compute_changes
from iidm_viewer.extensions_data import (
    READONLY_EXTENSIONS as _READONLY_EXTENSIONS,
    get_extensions_information as _shared_extensions_information,
    list_extension_names as _shared_list_extension_names,
)
from iidm_viewer.state import EDITABLE_EXTENSIONS, remove_extension, update_extension
from iidm_viewer import script_recorder


@st.cache_data(show_spinner=False)
def _extensions_names():
    return _shared_list_extension_names()


@st.cache_data(show_spinner=False)
def _extensions_information():
    return _shared_extensions_information()


_EXT_LABEL_PREFIX = "ext:"


def _ext_component_label(extension_name: str) -> str:
    """Component label used by the shared :class:`ChangeLog` to bucket
    extension edits separately from real-component edits."""
    return f"{_EXT_LABEL_PREFIX}{extension_name}"


def _add_to_ext_change_log(
    extension_name: str, changes_df: pd.DataFrame, original_df: pd.DataFrame
):
    """Record successfully-applied extension cell edits into the shared
    :class:`iidm_viewer.change_log.ChangeLog`.

    Entries are bucketed under ``"ext:<extension_name>"`` so the
    Extensions Explorer reader filters can fetch only the extension's
    own edits without colliding with component edits like ``Generators``.
    The collapse + net-diff invariants are inherited from
    :func:`change_log.merge_entry` — same rules as the component path.
    """
    from iidm_viewer.state import app_state

    log = app_state().change_log
    component = _ext_component_label(extension_name)
    for element_id in changes_df.index:
        for col in changes_df.columns:
            new_val = changes_df.at[element_id, col]
            before_val = (
                original_df.at[element_id, col]
                if col in original_df.columns
                else None
            )
            log.record(component, element_id, col, before_val, new_val)


def _render_ext_change_log(network, extension_name: str):
    """Display applied extension changes with per-row Revert buttons.

    Reads from the shared :class:`ChangeLog`. On revert, drops the
    entry from the shared log directly via :meth:`drop_entry`; the
    network mutation goes through ``update_extension`` so the
    Streamlit cache layer is invalidated.
    """
    from iidm_viewer.state import app_state

    state = app_state()
    component = _ext_component_label(extension_name)
    entries = state.change_log.entries(component=component)
    if not entries:
        return

    n = len(entries)
    st.markdown(f"**Applied changes ({n})**")
    hdr = st.columns([3, 2, 2, 2, 1])
    for widget, label in zip(hdr, ["Element", "Property", "Before", "After", ""]):
        widget.caption(label)

    for i, entry in enumerate(entries):
        row = st.columns([3, 2, 2, 2, 1])
        row[0].text(str(entry["element_id"]))
        row[1].text(entry["property"])
        row[2].text(str(entry["before"]))
        row[3].text(str(entry["after"]))
        if row[4].button("Revert", key=f"ext_revert_{extension_name}_{i}"):
            before = entry["before"]
            if before is None or (isinstance(before, float) and pd.isna(before)):
                st.error(
                    f"Cannot revert {entry['property']} on {entry['element_id']}: "
                    "original value is unavailable."
                )
            else:
                revert_df = pd.DataFrame(
                    {entry["property"]: [before]},
                    index=pd.Index([entry["element_id"]]),
                )
                try:
                    update_extension(network, extension_name, revert_df)
                    script_recorder.record_update_extension(
                        extension_name,
                        revert_df,
                        pd.DataFrame(),
                        is_revert=True,
                    )
                    state.change_log.drop_entry(entry)
                    st.rerun()
                except Exception as e:
                    st.error(f"Revert failed: {e}")


def _add_to_ext_removal_log(
    extension_name: str, ids: list, snapshot_df: pd.DataFrame
):
    """Record removed extension element ids into the shared
    :class:`ChangeLog`. ``record_removal`` already dedupes by
    ``(component, element_id)``, so repeat calls with the same id
    are safe.
    """
    from iidm_viewer.state import app_state

    if not ids:
        return
    component = _ext_component_label(extension_name)
    app_state().change_log.record_removal(component, list(ids), snapshot=snapshot_df)


def _render_ext_removal_log(extension_name: str):
    """Display removed extension element ids in a visually distinct section.

    Reads from the shared :class:`ChangeLog`.
    """
    from iidm_viewer.state import app_state

    component = _ext_component_label(extension_name)
    removals = app_state().change_log.removals(component=component)
    if not removals:
        return
    n = len(removals)
    st.markdown(f"**:red[Removed {extension_name} extensions ({n})]**")
    for entry in removals:
        st.caption(f"• {entry['element_id']}")


# ``_READONLY_EXTENSIONS`` now lives in :mod:`iidm_viewer.extensions_data`
# (imported above) so all three prototypes share the same set.


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
            df = get_extension_df(network, extension)

            if df.empty:
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

            if extension in _READONLY_EXTENSIONS:
                st.caption(
                    f"The **{extension}** extension is read-only "
                    "(geographical positions are managed outside the viewer)."
                )
                st.dataframe(df, use_container_width=True)
            else:
                editable_cols = [
                    c for c in EDITABLE_EXTENSIONS.get(extension, [])
                    if c in df.columns
                ]

                # Always use data_editor so rows can be marked for removal
                df_display = df.copy()
                df_display.insert(0, "_remove", False)
                col_config = _column_config(df_display, set(editable_cols))
                col_config["_remove"] = st.column_config.CheckboxColumn(
                    "Remove", default=False
                )

                edited_df = st.data_editor(
                    df_display,
                    use_container_width=True,
                    column_config=col_config,
                    key=f"ext_editor_{extension}",
                )

                ids_to_remove = edited_df[edited_df["_remove"] == True].index.tolist()
                edited_df_clean = edited_df.drop(columns=["_remove"])

                if editable_cols:
                    df_for_changes = df.drop(index=ids_to_remove, errors="ignore")
                    edited_for_changes = edited_df_clean.loc[df_for_changes.index]
                    changes = _compute_changes(df_for_changes, edited_for_changes, editable_cols)
                    n_changes = len(changes)
                else:
                    changes = pd.DataFrame()
                    n_changes = 0

                n_remove = len(ids_to_remove)

                if n_changes:
                    label = f"change{'s' if n_changes > 1 else ''}"
                    if st.button(
                        f"Apply {n_changes} {label}",
                        key=f"apply_ext_{extension}",
                    ):
                        try:
                            update_extension(network, extension, changes)
                            _add_to_ext_change_log(extension, changes, df)
                            script_recorder.record_update_extension(
                                extension, changes, df
                            )
                            st.success(
                                f"Updated {n_changes} {extension} "
                                f"extension{'s' if n_changes > 1 else ''}: "
                                f"{', '.join(str(i) for i in changes.index.tolist())}"
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(f"Update failed: {e}")

                if n_remove:
                    label = f"extension{'s' if n_remove > 1 else ''}"
                    if st.button(
                        f"Remove {n_remove} {label}",
                        key=f"remove_ext_{extension}",
                        type="primary",
                    ):
                        try:
                            remove_extension(network, extension, ids_to_remove)
                            _add_to_ext_removal_log(extension, ids_to_remove, df)
                            script_recorder.record_remove_extension(
                                extension, ids_to_remove
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(f"Remove failed: {e}")

                if editable_cols:
                    _render_ext_change_log(network, extension)
                _render_ext_removal_log(extension)

            csv = df.to_csv()
            st.download_button(
                label=f"Download {extension} extensions as CSV",
                data=csv,
                file_name=f"extension_{extension}.csv",
                mime="text/csv",
            )
        except Exception as e:
            st.error(f"Error loading {extension} extensions: {e}")
