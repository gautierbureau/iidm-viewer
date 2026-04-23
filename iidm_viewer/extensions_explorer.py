import pandas as pd
import streamlit as st

from iidm_viewer.caches import get_extension_df
from iidm_viewer.data_explorer import _column_config, _compute_changes
from iidm_viewer.powsybl_worker import run
from iidm_viewer.state import EDITABLE_EXTENSIONS, remove_extension, update_extension


@st.cache_data(show_spinner=False)
def _extensions_names():
    def _call():
        import pypowsybl.network as pn
        return sorted(pn.get_extensions_names())

    return run(_call)


@st.cache_data(show_spinner=False)
def _extensions_information():
    def _call():
        import pypowsybl.network as pn
        return pn.get_extensions_information()

    return run(_call)


def _add_to_ext_change_log(
    extension_name: str, changes_df: pd.DataFrame, original_df: pd.DataFrame
):
    key = f"_ext_change_log_{extension_name}"
    log: list[dict] = list(st.session_state.get(key, []))

    for element_id in changes_df.index:
        for col in changes_df.columns:
            new_val = changes_df.at[element_id, col]
            try:
                if pd.isna(new_val):
                    continue
            except (TypeError, ValueError):
                pass
            existing = next(
                (e for e in log if e["element_id"] == element_id and e["property"] == col),
                None,
            )
            if existing is None:
                before_val = (
                    original_df.at[element_id, col]
                    if col in original_df.columns
                    else None
                )
                log.append({
                    "element_id": element_id,
                    "property": col,
                    "before": before_val,
                    "after": new_val,
                })
            else:
                existing["after"] = new_val
                try:
                    if existing["before"] == existing["after"]:
                        log.remove(existing)
                except Exception:
                    pass

    st.session_state[key] = log


def _render_ext_change_log(network, extension_name: str):
    key = f"_ext_change_log_{extension_name}"
    log: list[dict] = st.session_state.get(key, [])
    if not log:
        return

    n = len(log)
    st.markdown(f"**Applied changes ({n})**")
    hdr = st.columns([3, 2, 2, 2, 1])
    for widget, label in zip(hdr, ["Element", "Property", "Before", "After", ""]):
        widget.caption(label)

    for i, entry in enumerate(list(log)):
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
                    log.pop(i)
                    st.session_state[key] = log
                    st.rerun()
                except Exception as e:
                    st.error(f"Revert failed: {e}")


def _add_to_ext_removal_log(
    extension_name: str, ids: list, snapshot_df: pd.DataFrame
):
    key = f"_ext_removal_log_{extension_name}"
    log: list[dict] = list(st.session_state.get(key, []))
    existing_ids = {e["element_id"] for e in log}
    for eid in ids:
        if eid not in existing_ids:
            snapshot = snapshot_df.loc[eid].to_dict() if eid in snapshot_df.index else {}
            log.append({"element_id": eid, "snapshot": snapshot})
    st.session_state[key] = log


def _render_ext_removal_log(extension_name: str):
    key = f"_ext_removal_log_{extension_name}"
    log: list[dict] = st.session_state.get(key, [])
    if not log:
        return
    n = len(log)
    st.markdown(f"**:red[Removed {extension_name} extensions ({n})]**")
    for entry in log:
        st.caption(f"• {entry['element_id']}")


_READONLY_EXTENSIONS = frozenset({"substationPosition", "linePosition"})


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
