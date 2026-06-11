"""Streamlit "Security Analysis" tab.

The pypowsybl integration + contingency / action builders live in the
framework-agnostic :mod:`iidm_viewer.security_analysis` module so the
PySide6 and NiceGUI prototypes can build their own UI on top. This
file holds only the Streamlit rendering glue + per-session caching
wrappers around the shared core.
"""
from __future__ import annotations

import os
import tempfile

import pandas as pd
import streamlit as st

from iidm_viewer.cache_backend import SA_ID, SA_MANUAL_DF
from iidm_viewer.caches import (
    _cache_key as _caches_cache_key,
    _net_key as _caches_net_key,
    backend as _backend,
    get_2wt_all,
    get_3wt_all,
    get_generators_all,
    get_lines_all,
    get_vl_nominal_v,
)
from iidm_viewer.filters import (
    FILTERS,
    build_vl_lookup,
    enrich_with_joins,
    render_filters,
)
from iidm_viewer.security_analysis import (
    ACTION_TYPES as _ACTION_TYPES,
    AUTO_MODES as _AUTO_MODES,
    CONDITION_TYPES as _CONDITION_TYPES,
    CTX_TYPES as _CTX_TYPES,
    ELEMENT_TYPES as _ELEMENT_TYPES,
    MANUAL_GROUPINGS as _MANUAL_GROUPINGS,
    MANUAL_TYPE_IDS_KEY as _MANUAL_TYPE_IDS_KEY,
    MANUAL_TYPES as _MANUAL_TYPES,
    SIDES as _SIDES,
    SecurityAnalysisViewModel,
    VIOLATION_TYPES as _VIOLATION_TYPES,
    action_summary as _action_summary,
    build_n1_contingencies,
    build_n2_contingencies,
    get_element_ids,
    normalize_manual_contingency,
    run_security_analysis,
    MANUAL_GROUPING_TOKENS as _MANUAL_GROUPING_TOKENS,
)


def _get_sa_vm() -> SecurityAnalysisViewModel:
    """Return the per-session :class:`SecurityAnalysisViewModel` singleton.

    Created lazily on first access and stashed in
    ``st.session_state["_sa_vm"]`` so the five configuration lists
    and the last results survive across Streamlit reruns the same
    way the legacy ``_sa_*`` session keys used to. PySide6 + NiceGUI
    hold the view-model instance as a tab field; Streamlit threads
    it through session state so every rerun sees the same lists.
    """
    vm = st.session_state.get("_sa_vm")
    if vm is None:
        vm = SecurityAnalysisViewModel()
        st.session_state["_sa_vm"] = vm
    return vm


# ---------------------------------------------------------------------------
# Streamlit-cached wrappers around the shared compute.
# ---------------------------------------------------------------------------
def _get_nominal_voltages(network) -> list[float]:
    """Streamlit-cached helper: pulls the cached VL frame from
    ``iidm_viewer.caches`` and reduces it to the sorted set of unique
    nominal voltages. The shared module exposes a non-cached version
    via ``get_nominal_voltages`` for the PySide6 / NiceGUI hosts."""
    try:
        df = get_vl_nominal_v(network)
        return sorted(df["nominal_v"].dropna().unique().tolist())
    except Exception:
        return []


def _get_ids(network) -> dict[str, list[str]]:
    """Streamlit-cached wrapper around the shared ``get_element_ids``.

    Cached per ``net_key`` in the :data:`cache_backend.SA_ID` slot;
    invalidated by :func:`cache_backend.invalidate_topology` on every
    topology edit. The PySide6 / NiceGUI hosts call ``get_element_ids``
    directly with their own caching layer.
    """
    net_key = _caches_net_key(network)
    cached = _backend.get(SA_ID)
    if cached is not None and cached.get("key") == net_key:
        return cached["data"]
    data = get_element_ids(network)
    _backend.set(SA_ID, {"key": net_key, "data": data})
    return data


_FILTERABLE_DF_GETTERS = {
    "Lines": get_lines_all,
    "2-Winding Transformers": get_2wt_all,
    "3-Winding Transformers": get_3wt_all,
    "Generators": get_generators_all,
}


def _get_filterable_df(network, manual_type: str) -> pd.DataFrame:
    """Return the enriched component DataFrame for *manual_type*.

    Raw data comes from ``caches.get_*_all`` (shared, 0 RT when warm). The
    enriched result (VL/substation join applied) is cached per
    ``(net_key, lf_gen, manual_type)`` so repeated visits to the same type
    within a rerun are free. Invalidated via ``caches._TOPOLOGY_CACHE_KEYS``
    on topology edits, and self-invalidating after a load flow (lf_gen bumps).
    """
    cache = _backend.setdefault(SA_MANUAL_DF, {})
    key = _caches_cache_key(network) + (manual_type,)
    if key in cache:
        return cache[key]

    getter_fn = _FILTERABLE_DF_GETTERS.get(manual_type)
    if not getter_fn:
        cache[key] = pd.DataFrame()
        return cache[key]

    df = getter_fn(network)
    if df.empty:
        cache[key] = pd.DataFrame()
        return cache[key]

    cache[key] = enrich_with_joins(df, build_vl_lookup(network))
    return cache[key]


def _contingencies_list() -> list[dict]:
    return _get_sa_vm().contingencies


# --- JSON file upload helpers ---

def _persist_uploaded_json(files, state_key: str) -> int:
    """Write each UploadedFile to a tempfile, record ``(name, path)`` in state.

    Returns the number of newly-persisted files. Skips files whose name is
    already present.
    """
    entries: list[dict] = st.session_state.setdefault(state_key, [])
    existing = {e["name"] for e in entries}
    added = 0
    for f in files or []:
        if f.name in existing:
            continue
        fd, path = tempfile.mkstemp(suffix=".json", prefix="sa_upload_")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(f.getvalue())
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        entries.append({"name": f.name, "path": path})
        existing.add(f.name)
        added += 1
    return added


def _json_paths(state_key: str) -> list[str]:
    return [e["path"] for e in st.session_state.get(state_key, [])]


def _render_json_upload_section(
    label: str,
    state_key: str,
    help_text: str,
    uploader_gen_key: str,
) -> None:
    """Render a collapsible section to upload and manage JSON files.

    Uploaded files are persisted to tempfiles and their paths are stored in
    ``st.session_state[state_key]`` as ``[{"name": str, "path": str}, ...]``.
    The uploader widget is keyed with an incrementing generation counter so
    it resets after a successful load.
    """
    entries: list[dict] = st.session_state.setdefault(state_key, [])
    if uploader_gen_key not in st.session_state:
        st.session_state[uploader_gen_key] = 0

    with st.expander(label, expanded=False):
        st.caption(help_text)
        gen = st.session_state[uploader_gen_key]
        uploaded = st.file_uploader(
            "JSON file(s)",
            type=["json"],
            accept_multiple_files=True,
            key=f"{state_key}_uploader_{gen}",
        )
        if st.button("Load", key=f"{state_key}_load_btn"):
            if not uploaded:
                st.warning("No file selected.")
            else:
                try:
                    added = _persist_uploaded_json(uploaded, state_key)
                except Exception as exc:
                    st.error(f"Failed to persist upload: {exc}")
                else:
                    st.session_state[uploader_gen_key] += 1
                    if added:
                        st.success(f"Loaded {added} file(s).")
                        st.rerun()
                    else:
                        st.warning("All selected files were already loaded.")

        if entries:
            st.caption(f"{len(entries)} JSON file(s) loaded")
            for i, e in enumerate(list(entries)):
                col1, col2 = st.columns([5, 1])
                with col1:
                    st.markdown(f"`{e['name']}`")
                with col2:
                    if st.button("Remove", key=f"{state_key}_rm_{i}"):
                        try:
                            os.unlink(e["path"])
                        except OSError:
                            pass
                        entries.pop(i)
                        st.rerun()


# --- Configuration: Contingencies sub-tab ---

def _render_contingencies_subtab(network):
    st.subheader("Contingency configuration")

    # --- Section A: automatic builder (N-1 / N-2) -----------------------
    st.markdown("**Automatic builder**")
    mode = st.radio(
        "Generation mode",
        options=_AUTO_MODES,
        horizontal=True,
        key="sa_auto_mode",
    )
    element_type = st.selectbox(
        "Element type",
        options=_ELEMENT_TYPES,
        key="sa_element_type",
    )

    nom_voltages = _get_nominal_voltages(network)
    if nom_voltages:
        default_v = [v for v in nom_voltages if v >= 380.0]
        selected_voltages = st.multiselect(
            "Filter by nominal voltage (kV) — leave empty to include all",
            options=nom_voltages,
            default=default_v,
            key="sa_nominal_v_filter",
            format_func=lambda v: f"{v:.0f} kV",
        )
    else:
        selected_voltages = []
        st.info("No voltage levels found in the network.")

    nominal_v_set = set(selected_voltages) if selected_voltages else None

    if mode == "N-1":
        auto = build_n1_contingencies(network, element_type, nominal_v_set)
    else:
        auto = build_n2_contingencies(network, element_type, nominal_v_set)

    # --- Section B: manual contingencies --------------------------------
    st.markdown("**Manual contingencies**")
    st.caption(
        "Pick any subset of elements of a given type and add them as one "
        "or several contingencies alongside the automatic ones."
    )
    manual: list[dict] = st.session_state.setdefault("_sa_manual_contingencies", [])
    ids = _get_ids(network)

    manual_type = st.selectbox(
        "Element type",
        options=_MANUAL_TYPES,
        key="sa_manual_type",
    )

    # Filters are rendered outside the form so changing them re-renders the
    # multiselect options immediately.
    type_df = _get_filterable_df(network, manual_type)
    if type_df.empty:
        type_ids = ids.get(_MANUAL_TYPE_IDS_KEY[manual_type], [])
    else:
        filter_cols = FILTERS.get(manual_type, [])
        filtered_df = render_filters(
            type_df,
            filter_cols,
            key_prefix=f"sa_manual_flt_{manual_type}",
            label=f"Filter {manual_type.lower()}",
        )
        type_ids = list(filtered_df.index.astype(str))

    with st.form("sa_manual_contingency_form", clear_on_submit=True):
        st.caption(f"{len(type_ids)} {manual_type.lower()} available after filtering")
        selected_ids = st.multiselect(
            f"Pick {manual_type.lower()} to include",
            options=type_ids,
            key="sa_manual_ids",
        )
        grouping = st.radio(
            "Grouping",
            options=_MANUAL_GROUPINGS,
            index=0,
            key="sa_manual_grouping",
        )
        group_id = st.text_input(
            "Contingency id (for single grouped mode)",
            key="sa_manual_group_id",
            placeholder="e.g. N2_outage_southwest",
        )
        submitted = st.form_submit_button("Add manual contingencies")

    if submitted:
        grouping_token = _MANUAL_GROUPING_TOKENS.get(grouping, grouping)
        try:
            new_contingencies = normalize_manual_contingency(
                manual_type, selected_ids, grouping_token, group_id,
            )
        except ValueError as exc:
            st.warning(str(exc))
        else:
            existing = {c["id"] for c in manual}
            added = 0
            for c in new_contingencies:
                if c["id"] in existing:
                    continue
                manual.append(c)
                existing.add(c["id"])
                added += 1
            if added:
                st.rerun()
            elif grouping_token == "per_element":
                st.warning("All selected elements were already added.")
            else:
                st.warning(f"Contingency id '{new_contingencies[0]['id']}' already exists.")

    if manual:
        st.caption(f"{len(manual)} manual contingency(ies) defined")
        for i, c in enumerate(list(manual)):
            with st.container(border=True):
                col1, col2 = st.columns([5, 1])
                with col1:
                    eids = c.get("element_ids") or ([c["element_id"]] if "element_id" in c else [])
                    st.markdown(
                        f"`{c['id']}` — "
                        f"{', '.join(f'`{x}`' for x in eids)}"
                    )
                with col2:
                    if st.button("Remove", key=f"sa_manual_rm_{i}"):
                        manual.pop(i)
                        st.rerun()

    # --- Section C: JSON import -----------------------------------------
    st.markdown("**Import contingencies from JSON**")
    _render_json_upload_section(
        label="Upload contingency JSON file(s)",
        state_key="_sa_contingencies_json_files",
        help_text=(
            "Files are passed to `add_contingencies_from_json_file` at run "
            "time. Use pypowsybl's native JSON contingency format."
        ),
        uploader_gen_key="_sa_contingencies_json_uploader_gen",
    )
    json_files = st.session_state.get("_sa_contingencies_json_files", [])

    # --- Section D: composed list --------------------------------------
    contingencies = list(auto) + list(manual)
    _get_sa_vm().set_contingencies(contingencies)

    auto_label = "N-1" if mode == "N-1" else "N-2"
    if contingencies or json_files:
        bits = [f"{len(auto)} auto ({auto_label})", f"{len(manual)} manual"]
        if json_files:
            bits.append(f"{len(json_files)} JSON file(s)")
        st.caption(
            " + ".join(bits)
            + f" — {len(contingencies)} dict contingencies (JSON entries "
            "added on run)"
        )
        with st.expander("Preview contingencies", expanded=False):
            preview = [
                {
                    "id": c["id"],
                    "element_ids": ", ".join(
                        c.get("element_ids") or ([c["element_id"]] if "element_id" in c else [])
                    ),
                }
                for c in contingencies
            ]
            st.dataframe(
                pd.DataFrame(preview),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info(
            "No contingencies yet — adjust the automatic filter above or add "
            "a manual contingency."
        )


# --- Configuration: Monitored elements sub-tab ---

def _render_monitored_subtab(network):
    st.subheader("Monitored elements")
    st.caption(
        "Define extra network elements for which the analysis should return "
        "power, current and voltage results. Each row below becomes a single "
        "call to `add_monitored_elements`."
    )

    vm = _get_sa_vm()
    entries = vm.monitored
    ids = _get_ids(network)
    contingency_ids = [c["id"] for c in _contingencies_list()]

    with st.form("sa_monitored_form", clear_on_submit=True):
        ctx_type = st.selectbox(
            "Contingency context",
            options=_CTX_TYPES,
            index=0,
            key="sa_mon_ctx",
            help=(
                "ALL: monitor in pre- and post-contingency states. "
                "NONE: pre-contingency only. "
                "SPECIFIC: only for the selected contingencies."
            ),
        )
        specific_cids: list[str] = []
        if ctx_type == "SPECIFIC":
            specific_cids = st.multiselect(
                "Contingencies",
                options=contingency_ids,
                key="sa_mon_cids",
            )
        branch_ids = st.multiselect(
            "Branches (lines and 2-winding transformers)",
            options=ids["branches"],
            key="sa_mon_branches",
        )
        vl_ids = st.multiselect(
            "Voltage levels",
            options=ids["voltage_levels"],
            key="sa_mon_vls",
        )
        t3w_ids = st.multiselect(
            "3-winding transformers",
            options=ids["three_windings_transformers"],
            key="sa_mon_3wt",
        )
        submitted = st.form_submit_button("Add monitored elements")

    if submitted:
        errors = vm.add_monitored(
            ctx_type,
            contingency_ids=specific_cids,
            branch_ids=branch_ids,
            voltage_level_ids=vl_ids,
            three_windings_transformer_ids=t3w_ids,
        )
        if errors:
            for msg in errors:
                st.warning(msg)
        else:
            st.rerun()

    if not entries:
        st.info("No monitored-element rules defined.")
        return

    st.caption(f"{len(entries)} rule(s) defined")
    for i, e in enumerate(entries):
        with st.container(border=True):
            col1, col2 = st.columns([5, 1])
            with col1:
                lines = [f"**Context:** {e['contingency_context_type']}"]
                if e["contingency_context_type"] == "SPECIFIC":
                    lines.append(f"**Contingencies:** {', '.join(e['contingency_ids'] or [])}")
                if e.get("branch_ids"):
                    lines.append(f"**Branches ({len(e['branch_ids'])}):** {', '.join(e['branch_ids'])}")
                if e.get("voltage_level_ids"):
                    lines.append(f"**Voltage levels ({len(e['voltage_level_ids'])}):** {', '.join(e['voltage_level_ids'])}")
                if e.get("three_windings_transformer_ids"):
                    lines.append(f"**3WTs ({len(e['three_windings_transformer_ids'])}):** {', '.join(e['three_windings_transformer_ids'])}")
                st.markdown("  \n".join(lines))
            with col2:
                if st.button("Remove", key=f"sa_mon_rm_{i}"):
                    vm.remove_monitored(i)
                    st.rerun()


# --- Configuration: Limit reductions sub-tab ---

def _render_limit_reductions_subtab():
    st.subheader("Limit reductions")
    st.caption(
        "Apply a reduction factor (in [0, 1]) to current limits. OpenLoadFlow "
        "currently supports `limit_type=CURRENT` and `contingency_context=ALL`."
    )

    vm = _get_sa_vm()
    entries = vm.reductions

    with st.form("sa_lr_form", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            value = st.number_input(
                "Value (0 – 1)",
                min_value=0.0,
                max_value=1.0,
                value=0.9,
                step=0.05,
                key="sa_lr_value",
            )
        with col2:
            permanent = st.checkbox("Permanent limits", value=True, key="sa_lr_perm")
        with col3:
            temporary = st.checkbox("Temporary limits", value=True, key="sa_lr_temp")

        col4, col5 = st.columns(2)
        with col4:
            min_dur = st.number_input(
                "Min temp. duration (s, optional)",
                min_value=0,
                value=0,
                step=60,
                key="sa_lr_min_dur",
                help="0 = no minimum",
            )
        with col5:
            max_dur = st.number_input(
                "Max temp. duration (s, optional)",
                min_value=0,
                value=0,
                step=60,
                key="sa_lr_max_dur",
                help="0 = no maximum",
            )

        col6, col7, col8 = st.columns(3)
        with col6:
            country = st.text_input("Country code (optional)", key="sa_lr_country")
        with col7:
            min_v = st.number_input(
                "Min voltage (kV, optional)",
                min_value=0.0,
                value=0.0,
                step=1.0,
                key="sa_lr_min_v",
            )
        with col8:
            max_v = st.number_input(
                "Max voltage (kV, optional)",
                min_value=0.0,
                value=0.0,
                step=1.0,
                key="sa_lr_max_v",
            )

        submitted = st.form_submit_button("Add limit reduction")

    if submitted:
        errors = vm.add_reduction(
            float(value),
            permanent=bool(permanent),
            temporary=bool(temporary),
            min_temporary_duration=int(min_dur),
            max_temporary_duration=int(max_dur),
            country=country,
            min_voltage=float(min_v),
            max_voltage=float(max_v),
        )
        if errors:
            for msg in errors:
                st.warning(msg)
        else:
            st.rerun()

    if not entries:
        st.info("No limit reductions defined.")
        return

    st.caption(f"{len(entries)} reduction(s) defined")
    df = pd.DataFrame(entries)
    remove_idx = None
    for i, e in enumerate(entries):
        with st.container(border=True):
            col1, col2 = st.columns([5, 1])
            with col1:
                scope = []
                if e["permanent"]:
                    scope.append("permanent")
                if e["temporary"]:
                    scope.append("temporary")
                parts = [f"**value={e['value']}** on {' + '.join(scope)} {e['limit_type']}"]
                extras = []
                for k in ("min_temporary_duration", "max_temporary_duration",
                          "country", "min_voltage", "max_voltage"):
                    if k in e:
                        extras.append(f"{k}={e[k]}")
                if extras:
                    parts.append("  \n" + " · ".join(extras))
                st.markdown("".join(parts))
            with col2:
                if st.button("Remove", key=f"sa_lr_rm_{i}"):
                    remove_idx = i

    if remove_idx is not None:
        vm.remove_reduction(remove_idx)
        st.rerun()

    with st.expander("Preview DataFrame passed to pypowsybl", expanded=False):
        st.dataframe(df, use_container_width=True, hide_index=True)


# --- Configuration: Actions sub-tab ---
#
# ``_action_summary`` is imported from the shared module so the Streamlit /
# Qt / NiceGUI hosts all render the same one-line action description.


def _render_action_form_fields(atype: str, ids: dict) -> dict | None:
    """Render type-specific fields; return the extra dict or None on error.

    Returns None when the selected element list is empty so the caller can
    surface a clear message rather than letting the selectbox raise.
    """
    if atype == "SWITCH":
        if not ids["switches"]:
            st.info("No switches in this network.")
            return None
        switch_id = st.selectbox("Switch", ids["switches"], key="sa_act_switch_id")
        open_ = st.checkbox("Open switch", value=True, key="sa_act_switch_open")
        return {"switch_id": switch_id, "open": bool(open_)}
    if atype == "TERMINALS_CONNECTION":
        if not ids["connectables"]:
            st.info("No connectable elements in this network.")
            return None
        element_id = st.selectbox(
            "Element (line / 2WT / generator)",
            ids["connectables"],
            key="sa_act_term_id",
        )
        side = st.selectbox("Side", _SIDES, index=0, key="sa_act_term_side")
        opening = st.checkbox("Open (disconnect)", value=True, key="sa_act_term_open")
        return {"element_id": element_id, "side": side, "opening": bool(opening)}
    if atype == "GENERATOR_ACTIVE_POWER":
        if not ids["generators"]:
            st.info("No generators in this network.")
            return None
        gen_id = st.selectbox("Generator", ids["generators"], key="sa_act_gen_id")
        is_relative = st.checkbox(
            "Relative change (tick) vs. absolute (untick)",
            value=True,
            key="sa_act_gen_rel",
        )
        active_power = st.number_input(
            "Active power (MW)",
            value=-10.0,
            step=10.0,
            key="sa_act_gen_p",
        )
        return {
            "generator_id": gen_id,
            "is_relative": bool(is_relative),
            "active_power": float(active_power),
        }
    if atype == "LOAD_ACTIVE_POWER":
        if not ids["loads"]:
            st.info("No loads in this network.")
            return None
        load_id = st.selectbox("Load", ids["loads"], key="sa_act_load_id")
        is_relative = st.checkbox(
            "Relative change (tick) vs. absolute (untick)",
            value=True,
            key="sa_act_load_rel",
        )
        active_power = st.number_input(
            "Active power (MW)",
            value=-10.0,
            step=10.0,
            key="sa_act_load_p",
        )
        return {
            "load_id": load_id,
            "is_relative": bool(is_relative),
            "active_power": float(active_power),
        }
    if atype == "PHASE_TAP_CHANGER_POSITION":
        if not ids["phase_tap_changers"]:
            st.info("No phase tap changers in this network.")
            return None
        tx_id = st.selectbox(
            "Transformer",
            ids["phase_tap_changers"],
            key="sa_act_ptc_id",
        )
        is_relative = st.checkbox(
            "Relative change (tick) vs. absolute (untick)",
            value=False,
            key="sa_act_ptc_rel",
        )
        tap_position = st.number_input(
            "Tap position",
            value=0,
            step=1,
            key="sa_act_ptc_tap",
        )
        side = st.selectbox("Side (3WTs only)", _SIDES, index=0, key="sa_act_ptc_side")
        return {
            "transformer_id": tx_id,
            "is_relative": bool(is_relative),
            "tap_position": int(tap_position),
            "side": side,
        }
    if atype == "RATIO_TAP_CHANGER_POSITION":
        if not ids["ratio_tap_changers"]:
            st.info("No ratio tap changers in this network.")
            return None
        tx_id = st.selectbox(
            "Transformer",
            ids["ratio_tap_changers"],
            key="sa_act_rtc_id",
        )
        is_relative = st.checkbox(
            "Relative change (tick) vs. absolute (untick)",
            value=False,
            key="sa_act_rtc_rel",
        )
        tap_position = st.number_input(
            "Tap position",
            value=0,
            step=1,
            key="sa_act_rtc_tap",
        )
        side = st.selectbox("Side (3WTs only)", _SIDES, index=0, key="sa_act_rtc_side")
        return {
            "transformer_id": tx_id,
            "is_relative": bool(is_relative),
            "tap_position": int(tap_position),
            "side": side,
        }
    if atype == "SHUNT_COMPENSATOR_POSITION":
        if not ids["shunt_compensators"]:
            st.info("No shunt compensators in this network.")
            return None
        shunt_id = st.selectbox(
            "Shunt compensator",
            ids["shunt_compensators"],
            key="sa_act_shunt_id",
        )
        section = st.number_input(
            "Section count",
            value=0,
            step=1,
            min_value=0,
            key="sa_act_shunt_sec",
        )
        return {
            "shunt_id": shunt_id,
            "section": int(section),
        }
    return {}


def _render_actions_subtab(network):
    st.subheader("Remedial actions")
    st.caption(
        "Define atomic actions that can later be grouped into an operator "
        "strategy. Each action gets a unique id."
    )

    vm = _get_sa_vm()
    entries = vm.actions
    ids = _get_ids(network)

    # Action-type selectbox is outside the form so type-specific fields
    # re-render immediately on change.
    atype = st.selectbox(
        "Action type",
        options=_ACTION_TYPES,
        key="sa_act_type",
    )

    with st.form("sa_actions_form", clear_on_submit=True):
        action_id = st.text_input(
            "Action ID (unique)",
            key="sa_act_id",
            placeholder="e.g. open_L1 or gen_down",
        )
        extra = _render_action_form_fields(atype, ids)
        submitted = st.form_submit_button("Add action")

    if submitted:
        if extra is None:
            st.warning("Cannot build this action — no matching element in the network.")
        else:
            errors = vm.add_action(atype, action_id, extra)
            if errors:
                for msg in errors:
                    st.warning(msg)
            else:
                st.rerun()

    _render_json_upload_section(
        label="Upload action JSON file(s)",
        state_key="_sa_actions_json_files",
        help_text=(
            "Files are passed to `add_actions_from_json_file` at run time. "
            "Action IDs in these files can be referenced by operator "
            "strategies defined here or loaded from JSON."
        ),
        uploader_gen_key="_sa_actions_json_uploader_gen",
    )

    if not entries:
        st.info("No form-defined actions. JSON-imported actions will still be used.")
        return

    st.caption(f"{len(entries)} action(s) defined")
    for i, e in enumerate(entries):
        with st.container(border=True):
            col1, col2 = st.columns([5, 1])
            with col1:
                st.markdown(_action_summary(e))
            with col2:
                if st.button("Remove", key=f"sa_act_rm_{i}"):
                    removed_id = e["action_id"]
                    vm.remove_action(i)
                    # Also drop the action from any strategies that
                    # reference it -- same cross-list invariant as
                    # the PySide6 + NiceGUI tabs.
                    for strat in vm.strategies:
                        strat["action_ids"] = [
                            a for a in strat.get("action_ids", [])
                            if a != removed_id
                        ]
                    st.rerun()


# --- Configuration: Operator strategies sub-tab ---

def _render_operator_strategies_subtab(network=None):
    st.subheader("Operator strategies")
    st.caption(
        "Group actions into a post-contingency strategy. Each strategy is "
        "triggered by one contingency and applies the listed actions when "
        "its condition is met."
    )

    vm = _get_sa_vm()
    entries = vm.strategies
    contingencies = _contingencies_list()
    action_ids = vm.action_ids()
    contingency_ids = [c["id"] for c in contingencies]

    if not contingency_ids or not action_ids:
        st.info(
            "Define at least one contingency (in the Contingencies sub-tab) "
            "and one action (in the Actions sub-tab) to build a strategy."
        )
    else:
        # Condition-type selectbox is outside the form so violation fields
        # show/hide immediately on change.
        condition_type = st.selectbox(
            "Condition type",
            options=_CONDITION_TYPES,
            index=0,
            key="sa_strat_condition",
            help=(
                "TRUE_CONDITION: always apply. "
                "ANY/ALL/AT_LEAST_ONE_VIOLATION_CONDITION: apply only if "
                "post-contingency limit violations match the subject/type filters."
            ),
        )
        needs_filters = condition_type != "TRUE_CONDITION"

        with st.form("sa_strat_form", clear_on_submit=True):
            strat_id = st.text_input(
                "Strategy ID (unique)",
                key="sa_strat_id",
                placeholder="e.g. strat_open_line",
            )
            contingency_id = st.selectbox(
                "Triggered by contingency",
                options=contingency_ids,
                key="sa_strat_cid",
            )
            selected_actions = st.multiselect(
                "Actions to apply (in order)",
                options=action_ids,
                key="sa_strat_actions",
            )
            if needs_filters:
                ids = _get_ids(network) if network is not None else {}
                subject_options = sorted(
                    set(ids.get("branches", []))
                    | set(ids.get("three_windings_transformers", []))
                    | set(ids.get("voltage_levels", []))
                )
                violation_subject_ids = st.multiselect(
                    "Violation subject IDs (empty = any element)",
                    options=subject_options,
                    key="sa_strat_vsubj",
                    help="Only violations on these elements count toward the condition.",
                )
                violation_types = st.multiselect(
                    "Violation types (empty = any type)",
                    options=_VIOLATION_TYPES,
                    key="sa_strat_vtypes",
                )
            else:
                violation_subject_ids = []
                violation_types = []
            submitted = st.form_submit_button("Add operator strategy")

        if submitted:
            errors = vm.add_strategy(
                strat_id,
                contingency_id,
                selected_actions,
                condition_type=condition_type,
                violation_subject_ids=list(violation_subject_ids),
                violation_types=list(violation_types),
            )
            if errors:
                for msg in errors:
                    st.warning(msg)
            else:
                st.rerun()

    _render_json_upload_section(
        label="Upload operator-strategy JSON file(s)",
        state_key="_sa_operator_strategies_json_files",
        help_text=(
            "Files are passed to `add_operator_strategies_from_json_file` "
            "at run time. Strategies can reference contingencies and actions "
            "loaded either from the forms above or from JSON."
        ),
        uploader_gen_key="_sa_operator_strategies_json_uploader_gen",
    )

    if not entries:
        st.info("No form-defined strategies. JSON-imported strategies will still be used.")
        return

    st.caption(f"{len(entries)} strategy(ies) defined")
    for i, s in enumerate(entries):
        with st.container(border=True):
            col1, col2 = st.columns([5, 1])
            with col1:
                cond = s.get("condition_type", "TRUE_CONDITION")
                lines = [
                    f"`{s['operator_strategy_id']}` — triggered by "
                    f"**`{s['contingency_id']}`**",
                    f"**Condition:** {cond}",
                    f"**Actions ({len(s['action_ids'])}):** "
                    + ", ".join(f"`{a}`" for a in s["action_ids"]),
                ]
                subj = s.get("violation_subject_ids") or []
                vtypes = s.get("violation_types") or []
                if subj:
                    lines.append(
                        "**Subjects:** " + ", ".join(f"`{x}`" for x in subj)
                    )
                if vtypes:
                    lines.append("**Violation types:** " + ", ".join(vtypes))
                st.markdown("  \n".join(lines))
            with col2:
                if st.button("Remove", key=f"sa_strat_rm_{i}"):
                    vm.remove_strategy(i)
                    st.rerun()


# --- Configuration tab (run button + sub-tabs) ---

def _render_config_tab(network):
    (
        sub_cont,
        sub_mon,
        sub_lr,
        sub_act,
        sub_strat,
    ) = st.tabs(
        [
            "Contingencies",
            "Monitored elements",
            "Limit reductions",
            "Actions",
            "Operator strategies",
        ]
    )

    with sub_cont:
        _render_contingencies_subtab(network)
    with sub_mon:
        _render_monitored_subtab(network)
    with sub_lr:
        _render_limit_reductions_subtab()
    with sub_act:
        _render_actions_subtab(network)
    with sub_strat:
        _render_operator_strategies_subtab(network)

    st.divider()
    vm = _get_sa_vm()
    contingencies = vm.contingencies
    monitored = vm.monitored
    reductions = vm.reductions
    actions = vm.actions
    strategies = vm.strategies
    contingencies_json_paths = _json_paths("_sa_contingencies_json_files")
    actions_json_paths = _json_paths("_sa_actions_json_files")
    strategies_json_paths = _json_paths("_sa_operator_strategies_json_files")

    cols = st.columns(6)
    cols[0].metric(
        "Contingencies",
        len(contingencies),
        delta=f"+{len(contingencies_json_paths)} JSON" if contingencies_json_paths else None,
    )
    cols[1].metric("Monitored", len(monitored))
    cols[2].metric("Reductions", len(reductions))
    cols[3].metric(
        "Actions",
        len(actions),
        delta=f"+{len(actions_json_paths)} JSON" if actions_json_paths else None,
    )
    cols[4].metric(
        "Strategies",
        len(strategies),
        delta=f"+{len(strategies_json_paths)} JSON" if strategies_json_paths else None,
    )

    has_contingency_source = bool(contingencies) or bool(contingencies_json_paths)
    with cols[5]:
        if st.button(
            "Run Security Analysis",
            key="sa_run_btn",
            type="primary",
            disabled=not has_contingency_source,
        ):
            with st.spinner(
                f"Running security analysis ({len(contingencies)} contingencies"
                + (f" + {len(contingencies_json_paths)} JSON file(s)"
                   if contingencies_json_paths else "")
                + ")…"
            ):
                try:
                    results = run_security_analysis(
                        network,
                        contingencies,
                        monitored_elements=monitored,
                        limit_reductions=reductions,
                        actions=actions,
                        operator_strategies=strategies,
                        contingencies_json_paths=contingencies_json_paths,
                        actions_json_paths=actions_json_paths,
                        operator_strategies_json_paths=strategies_json_paths,
                    )
                    vm.store_results(results)
                    st.success("Security analysis complete.")
                except Exception as exc:
                    st.error(f"Security analysis failed: {exc}")


def _style_status(val: str) -> str:
    if val == "CONVERGED":
        return "color: green"
    return "background-color: #ff4b4b; color: white"


def _style_violations(val: int) -> str:
    if val == 0:
        return ""
    if val >= 3:
        return "background-color: #ff4b4b; color: white"
    return "background-color: #ffa500; color: white"


def _render_monitored_pre(results: dict):
    pre_branch = results.get("pre_branch_results", pd.DataFrame())
    pre_bus = results.get("pre_bus_results", pd.DataFrame())
    pre_3wt = results.get("pre_3wt_results", pd.DataFrame())
    if pre_branch.empty and pre_bus.empty and pre_3wt.empty:
        return
    with st.expander("Pre-contingency monitored results", expanded=False):
        if not pre_branch.empty:
            st.caption("Branches (P, Q, I)")
            st.dataframe(pre_branch, use_container_width=True)
        if not pre_bus.empty:
            st.caption("Buses (voltage magnitude & angle)")
            st.dataframe(pre_bus, use_container_width=True)
        if not pre_3wt.empty:
            st.caption("3-winding transformers")
            st.dataframe(pre_3wt, use_container_width=True)


def _render_monitored_post(cr: dict):
    br = cr.get("branch_results", pd.DataFrame())
    bu = cr.get("bus_results", pd.DataFrame())
    t3 = cr.get("three_windings_transformer_results", pd.DataFrame())
    if br.empty and bu.empty and t3.empty:
        return
    st.caption("Monitored results for this contingency")
    if not br.empty:
        st.markdown("**Branches**")
        st.dataframe(br, use_container_width=True)
    if not bu.empty:
        st.markdown("**Buses**")
        st.dataframe(bu, use_container_width=True)
    if not t3.empty:
        st.markdown("**3-winding transformers**")
        st.dataframe(t3, use_container_width=True)


def _render_operator_strategy_block(sid: str, sr: dict):
    """Render one operator-strategy result block (status + violations + monitored)."""
    status = sr.get("status", "UNKNOWN")
    viol = sr.get("limit_violations", pd.DataFrame())
    status_color = "green" if status == "CONVERGED" else "red"
    actions_str = ", ".join(f"`{a}`" for a in sr.get("action_ids", []))
    st.markdown(
        f"`{sid}` — **Status:** :{status_color}[{status}]  \n"
        f"**Actions:** {actions_str or '(none)'}"
    )
    if not viol.empty:
        st.caption(f"{len(viol)} limit violation(s) after the strategy")
        st.dataframe(viol, use_container_width=True, hide_index=True)
    _render_monitored_post(sr)


def _render_results_tab():
    results = _get_sa_vm().results
    if results is None:
        st.info(
            "No results yet. Configure and run a security analysis "
            "in the Configuration tab."
        )
        return

    contingencies = results.get("contingencies", [])
    pre_status = results.get("pre_status", "UNKNOWN")
    pre_violations: pd.DataFrame = results.get("pre_violations", pd.DataFrame())
    post: dict = results.get("post", {})

    json_export = results.get("json_export")
    if json_export:
        st.download_button(
            "Download results (JSON)",
            data=json_export,
            file_name="security_analysis_result.json",
            mime="application/json",
            key="sa_results_json_dl",
            help="Native pypowsybl SecurityAnalysisResult JSON export.",
        )

    # Pre-contingency summary
    st.subheader("Pre-contingency state")
    col1, col2 = st.columns(2)
    col1.metric("Base case status", pre_status)
    col2.metric(
        "Limit violations",
        0 if pre_violations.empty else len(pre_violations),
    )

    if not pre_violations.empty:
        st.caption("Pre-contingency limit violations")
        st.dataframe(pre_violations, use_container_width=True, hide_index=True)

    _render_monitored_pre(results)

    # Post-contingency summary
    st.subheader("Post-contingency results")

    if not post:
        st.info("No post-contingency results available.")
        return

    form_ids = {c["id"] for c in contingencies}
    rows = []
    for c in contingencies:
        cid = c["id"]
        cr = post.get(cid, {})
        viol_df: pd.DataFrame = cr.get("limit_violations", pd.DataFrame())
        eids = c.get("element_ids") or ([c["element_id"]] if "element_id" in c else [])
        rows.append(
            {
                "Contingency": cid,
                "Element": ", ".join(eids),
                "Status": cr.get("status", "UNKNOWN"),
                "Violations": 0 if viol_df.empty else len(viol_df),
            }
        )
    # Contingencies loaded from JSON files are not in the form list, so show
    # them with whatever id came back in the result dict.
    for cid, cr in post.items():
        if cid in form_ids:
            continue
        viol_df = cr.get("limit_violations", pd.DataFrame())
        rows.append(
            {
                "Contingency": cid,
                "Element": "(from JSON)",
                "Status": cr.get("status", "UNKNOWN"),
                "Violations": 0 if viol_df.empty else len(viol_df),
            }
        )

    summary_df = pd.DataFrame(rows)

    n_failed = int((summary_df["Status"] != "CONVERGED").sum())
    n_with_viol = int((summary_df["Violations"] > 0).sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Contingencies", len(contingencies))
    c2.metric("Failed / not converged", n_failed)
    c3.metric("With limit violations", n_with_viol)

    max_viol = int(summary_df["Violations"].max()) if not summary_df.empty else 0
    threshold = st.slider(
        "Show contingencies with violations ≥",
        min_value=0,
        max_value=max(max_viol, 1),
        value=0,
        key="sa_violation_threshold",
    )

    filtered = summary_df[summary_df["Violations"] >= threshold]
    styled = filtered.style.map(_style_status, subset=["Status"]).map(
        _style_violations, subset=["Violations"]
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # Drill-down
    st.subheader("Contingency detail")

    form_ids = [c["id"] for c in contingencies]
    extra_ids = [cid for cid in post.keys() if cid not in set(form_ids)]
    contingency_options = form_ids + sorted(extra_ids)
    id_filter = st.text_input(
        "Filter by contingency ID (substring, case-insensitive)",
        key="sa_contingency_filter",
    )
    if id_filter:
        contingency_options = [
            c for c in contingency_options if id_filter.lower() in c.lower()
        ]

    if not contingency_options:
        st.info("No contingencies match the filter.")
        return

    selected_contingency = st.selectbox(
        "Select contingency",
        options=contingency_options,
        key="sa_selected_contingency",
    )

    cr = post.get(selected_contingency, {})
    status = cr.get("status", "UNKNOWN")
    viol_df = cr.get("limit_violations", pd.DataFrame())

    status_color = "green" if status == "CONVERGED" else "red"
    st.markdown(f"**Status:** :{status_color}[{status}]")

    if not viol_df.empty:
        st.caption(f"{len(viol_df)} limit violation(s)")
        st.dataframe(viol_df, use_container_width=True, hide_index=True)
    else:
        st.success("No limit violations for this contingency.")

    _render_monitored_post(cr)

    # Operator strategies that target this contingency
    os_results: dict = results.get("operator_strategies", {})
    matching = [
        (sid, sr) for sid, sr in os_results.items()
        if sr.get("contingency_id") == selected_contingency
    ]
    if matching:
        st.subheader("Operator strategies for this contingency")
        for sid, sr in matching:
            with st.container(border=True):
                _render_operator_strategy_block(sid, sr)


def render_security_analysis(network):
    tab_config, tab_results = st.tabs(["Configuration", "Results"])

    with tab_config:
        _render_config_tab(network)

    with tab_results:
        _render_results_tab()
