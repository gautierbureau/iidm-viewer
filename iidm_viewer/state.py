import pandas as pd
import streamlit as st

from iidm_viewer.powsybl_worker import NetworkProxy, run
from iidm_viewer import network_loader
from iidm_viewer.caches import (
    invalidate_on_load_flow,
    invalidate_on_network_replace,
    invalidate_on_topology_change,
)
from iidm_viewer import script_recorder


def init_state():
    defaults = {
        "network": None,
        "selected_vl": None,
        "nad_depth": 1,
        "component_type": "Voltage Levels",
        "vl_selector_gen": 0,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def get_import_extensions() -> list[str]:
    """Return file extensions accepted by pypowsybl, discovered at runtime.

    Result is cached in session state so the worker is only hit once per
    browser session. ``zip`` is always included for pre-zipped archives.
    Delegates to :mod:`iidm_viewer.network_loader` for the actual
    worker-routed pypowsybl call.
    """
    if "import_extensions" not in st.session_state:
        st.session_state["import_extensions"] = network_loader.get_import_extensions()
    return st.session_state["import_extensions"]


def get_export_formats() -> list[str]:
    """Return export format names supported by pypowsybl, cached per session."""
    if "export_formats" not in st.session_state:
        st.session_state["export_formats"] = network_loader.get_export_formats()
    return st.session_state["export_formats"]


def export_network(
    network,
    format_name: str,
    parameters: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    """Streamlit wrapper around the shared
    :func:`iidm_viewer.network_loader.export_network`.

    Kept for backwards compatibility — callers go through this; the
    actual worker-routed export + ZIP unwrap live in the shared
    module so the PySide6 and NiceGUI prototypes reuse them.
    """
    return network_loader.export_network(network, format_name, parameters)


def load_network(
    uploaded_file,
    parameters: dict[str, str] | None = None,
    post_processors: list[str] | None = None,
):
    """Load a network from an uploaded file into session state.

    *parameters* is forwarded to ``load_from_binary_buffer`` so callers can
    pass format-specific import options discovered via
    :func:`~iidm_viewer.io_options.get_format_parameters`.

    *post_processors* is a list of post-processor names to apply after
    parsing, e.g. ``['loadflowResultsCompletion']``.  Available names are
    returned by :func:`~iidm_viewer.io_options.get_import_post_processors`.

    The raw file bytes are stored in ``_last_file_bytes`` so the UI can offer
    a "Reload with options" flow without requiring a second upload.
    """
    raw_bytes = uploaded_file.getvalue()
    network = network_loader.load_from_bytes(
        uploaded_file.name,
        raw_bytes,
        parameters=parameters,
        post_processors=post_processors,
    )
    st.session_state.network = network
    st.session_state.selected_vl = None
    st.session_state["vl_selector_gen"] = st.session_state.get("vl_selector_gen", 0) + 1
    st.session_state.pop("_vl_set_by_click", None)
    st.session_state.pop("_lf_report_json", None)
    invalidate_on_network_replace()
    st.session_state["_last_file_bytes"] = raw_bytes
    st.session_state.pop("_export_bytes", None)
    st.session_state.pop("_export_fmt", None)
    st.session_state.pop("_export_ext", None)
    st.session_state.pop("va_nom_select", None)
    for k in [k for k in st.session_state if k.startswith("_change_log_") or k.startswith("_removal_log_") or k.startswith("_ext_change_log_") or k.startswith("_ext_removal_log_") or k.startswith("_export_cache_")]:
        del st.session_state[k]
    script_recorder.record_load_network(uploaded_file.name, params, pp_list)
    return network


def create_empty_network(network_id: str = "network"):
    """Create a blank network and install it as the session network.

    Lets users bootstrap a model from scratch without uploading anything —
    they can then build it up via the Data Explorer's "Create a new …"
    forms. Like :func:`load_network`, the resulting object is a
    :class:`NetworkProxy` so every subsequent pypowsybl call runs on the
    worker thread.
    """
    network = network_loader.create_empty(network_id)
    st.session_state.network = network
    st.session_state.selected_vl = None
    st.session_state["vl_selector_gen"] = st.session_state.get("vl_selector_gen", 0) + 1
    st.session_state.pop("_vl_set_by_click", None)
    st.session_state.pop("_lf_report_json", None)
    invalidate_on_network_replace()
    st.session_state.pop("_last_file", None)
    st.session_state.pop("_last_file_id", None)
    st.session_state.pop("_export_bytes", None)
    st.session_state.pop("_export_fmt", None)
    st.session_state.pop("_export_ext", None)
    for k in [k for k in st.session_state if k.startswith("_change_log_") or k.startswith("_removal_log_") or k.startswith("_ext_change_log_") or k.startswith("_ext_removal_log_") or k.startswith("_export_cache_")]:
        del st.session_state[k]
    script_recorder.record_create_empty(nid)
    return network


def get_network():
    return st.session_state.get("network")


def run_loadflow(network):
    """Run AC load flow + invalidate Streamlit caches.

    The actual pypowsybl call lives in
    :func:`iidm_viewer.loadflow.run_ac` so the PySide6 and NiceGUI
    prototypes share the same worker round-trip. This wrapper reads
    the dialog parameters from session_state, stashes the report JSON
    in session_state, and calls :func:`invalidate_on_load_flow`.

    Returns the raw pypowsybl ``LoadFlowResult`` list (legacy
    contract — callers index `[0].status.name`).
    """
    from iidm_viewer.lf_parameters import get_lf_parameters
    from iidm_viewer.loadflow import run_ac

    generic, provider = get_lf_parameters()
    result = run_ac(network, generic, provider)
    st.session_state["_lf_report_json"] = result.report_json
    # Invalidate cached lookups so tabs reload fresh data
    invalidate_on_load_flow()
    script_recorder.record_run_loadflow(generic, provider)
    return result.results


# Source of truth lives in iidm_viewer.component_registry so the Qt /
# NiceGUI prototypes can reuse it without importing streamlit.
from iidm_viewer.component_registry import EDITABLE_COMPONENTS  # noqa: F401


# Removal-related registries live in the framework-agnostic component
# registry; re-export so the existing Streamlit imports keep working.
from iidm_viewer.component_registry import (  # noqa: F401
    REMOVABLE_COMPONENTS,
    _FEEDER_BAY_TYPES,
    _HVDC_TYPES,
    _SHALLOW_REMOVE_TYPES,
)


# Voltage-level resolution for substation removal lives in the shared
# registry; kept as a re-export here for any external caller.
from iidm_viewer.component_registry import _find_vl_ids_for_substations  # noqa: F401, E402


# _resolve_hvdc_removal also lives in the shared registry — re-export
# for any external caller.
from iidm_viewer.component_registry import _resolve_hvdc_removal  # noqa: F401, E402
from iidm_viewer.component_registry import remove_elements as _remove_elements_shared


def remove_components(network, component: str, ids: list[str]) -> list[str]:
    """Remove elements from the network and invalidate session caches.

    The removal logic itself (feeder-bays / HVDC triples / VLs /
    substations / branches) lives in
    :func:`iidm_viewer.component_registry.remove_elements` so the
    PySide6 and NiceGUI prototypes use the same code path. This
    wrapper just adds the Streamlit cache-invalidation step.
    """
    removed = _remove_elements_shared(network, component, ids)
    invalidate_on_topology_change()
    return removed


def update_components(network, component: str, changes_df):
    """Apply a DataFrame of changes via the appropriate update_ method.

    *changes_df* is indexed by element id and may contain NaN for cells
    that didn't change.  pypowsybl rejects NaN values, so we group rows
    by their non-null column set and issue one update call per group.
    """
    if changes_df.empty:
        return
    update_method_name, _ = EDITABLE_COMPONENTS[component]
    raw = object.__getattribute__(network, "_obj")

    # Group rows by which columns are non-null
    groups: dict[tuple[str, ...], list[str]] = {}
    for idx in changes_df.index:
        row = changes_df.loc[idx]
        cols = tuple(row.dropna().index.tolist())
        groups.setdefault(cols, []).append(idx)

    def _do_update():
        method = getattr(raw, update_method_name)
        for cols, ids in groups.items():
            subset = changes_df.loc[ids, list(cols)]
            method(subset)

    run(_do_update)
    invalidate_on_topology_change()


def add_to_change_log(method_name: str, changes_df: pd.DataFrame, original_df: pd.DataFrame):
    """Accumulate successfully-applied cell changes into a per-component session-state log.

    Writes to ``st.session_state[f"_change_log_{method_name}"]``.  The
    collapse + net-diff invariants are delegated to
    :func:`iidm_viewer.change_log.merge_entry` so the PySide6 and
    NiceGUI prototypes' ``ChangeLog`` class apply the same rules; the
    session-state list layout (no ``component`` key — Streamlit looks
    it up by method-name index) stays unchanged.
    """
    from iidm_viewer.change_log import merge_entry

    key = f"_change_log_{method_name}"
    log: list[dict] = list(st.session_state.get(key, []))
    # Streamlit's existing on-disk entries don't carry a ``component``
    # key (the method_name is the implicit grouping). Use the empty
    # string here; ``merge_entry`` will only compare it against the
    # same default and the entries stay shape-compatible.
    component = ""

    for element_id in changes_df.index:
        for col in changes_df.columns:
            new_val = changes_df.at[element_id, col]
            before_val = (
                original_df.at[element_id, col]
                if col in original_df.columns
                else None
            )
            merge_entry(log, component, element_id, col, before_val, new_val)

    # Drop the ``component`` key on freshly-appended entries so the
    # on-disk shape matches what the existing render code expects
    # (``element_id``, ``property``, ``before``, ``after`` only).
    for entry in log:
        entry.pop("component", None)

    st.session_state[key] = log


def toggle_switch(network, switch_id: str, new_open: bool) -> tuple[bool, bool]:
    """Open or close a single switch; return ``(before_open, after_open)``.

    The pypowsybl read + write pair lives in
    :func:`iidm_viewer.component_registry.toggle_switch` so the
    Streamlit, PySide6 and NiceGUI breaker handlers share the same
    code path. This wrapper just adds the Streamlit-side
    topology-cache invalidation.
    """
    from iidm_viewer.component_registry import toggle_switch as _shared
    result = _shared(network, switch_id, new_open)
    invalidate_on_topology_change()
    return result


# Extension name -> list of columns that pypowsybl's update_extensions accepts.
#
# Extensions not listed here (substationPosition, position, slackTerminal, ...)
# have immutable columns on the Java side and can only be changed by
# remove_extensions + create_extensions. We keep those read-only for now; users
# can still remove + recreate them via the Data Explorer Components tab.
EDITABLE_EXTENSIONS: dict[str, list[str]] = {
    "activePowerControl": [
        "participate", "droop", "participation_factor",
        "min_target_p", "max_target_p",
    ],
    "busbarSectionPosition": ["busbar_index", "section_index"],
    "entsoeArea": ["code"],
    "entsoeCategory": ["code"],
    "hvdcAngleDroopActivePowerControl": ["droop", "p0", "enabled"],
    "hvdcOperatorActivePowerRange": [
        "opr_from_cs1_to_cs2", "opr_from_cs2_to_cs1",
    ],
    "standbyAutomaton": [
        "standby", "b0",
        "low_voltage_threshold", "low_voltage_setpoint",
        "high_voltage_threshold", "high_voltage_setpoint",
    ],
    "voltagePerReactivePowerControl": ["slope"],
    "voltageRegulation": [
        "voltage_regulator_on", "target_v", "regulated_element_id",
    ],
}


def remove_extension(network, extension_name: str, ids: list):
    """Remove extension rows from the network on the worker thread."""
    raw = object.__getattribute__(network, "_obj")

    def _do_remove():
        raw.remove_extensions(extension_name, ids)

    run(_do_remove)
    invalidate_on_topology_change()


def update_extension(network, extension_name: str, changes_df):
    """Apply a DataFrame of changes to an extension via ``update_extensions``.

    *changes_df* is indexed by the extension's native index (usually the
    element id) and may contain NaN for cells that didn't change. pypowsybl
    rejects NaN values, so we group rows by their non-null column set and
    issue one update call per group — same shape as :func:`update_components`.
    """
    if changes_df.empty:
        return
    if extension_name not in EDITABLE_EXTENSIONS:
        raise ValueError(f"Extension {extension_name!r} is not editable.")
    raw = object.__getattribute__(network, "_obj")

    groups: dict[tuple[str, ...], list] = {}
    for idx in changes_df.index:
        row = changes_df.loc[idx]
        cols = tuple(row.dropna().index.tolist())
        groups.setdefault(cols, []).append(idx)

    def _do_update():
        for cols, ids in groups.items():
            subset = changes_df.loc[ids, list(cols)]
            raw.update_extensions(extension_name, subset)

    run(_do_update)
    invalidate_on_topology_change()


# ----------------------------------------------------------------------
# CREATABLE_COMPONENTS registry + validators + create_component_bay
# live in iidm_viewer.component_creation so the PySide6 and NiceGUI
# prototypes share them. _VALIDATORS is mutated later in this file
# (line ~870 adds _validate_voltage_level), so import the dict object
# rather than rebinding it locally.
# ----------------------------------------------------------------------
from iidm_viewer.component_creation import (  # noqa: E402, F401  (re-exported)
    CREATABLE_COMPONENTS,
    ENERGY_SOURCES,
    FEEDER_DIRECTIONS,
    LOAD_TYPES,
    LOCATOR_FIELDS,
    SVC_REGULATION_MODES,
    _SHUNT_LINEAR_FIELDS,
    _VALIDATORS,
    _dispatch_bay_create as _shared_dispatch_bay_create,
    _dispatch_shunt_bay as _shared_dispatch_shunt_bay,
    _validate_generator,
    _validate_minmax_p,
    _validate_shunt,
    _validate_svc,
    _validate_voltage_regulator,
    list_busbar_sections,
    list_node_breaker_voltage_levels,
    validate_create_fields,
)


def _dispatch_bay_create(network, bay_fn_name, fields):
    """Streamlit wrapper that also invalidates topology + geography caches."""
    _shared_dispatch_bay_create(network, bay_fn_name, fields)
    invalidate_on_topology_change(affects_geography=True)


def _dispatch_shunt_bay(network, fields):
    """Streamlit wrapper for the shunt-bay create that also invalidates caches."""
    _shared_dispatch_shunt_bay(network, fields)
    invalidate_on_topology_change(affects_geography=True)


def create_component_bay(network, component, fields):
    """Streamlit wrapper around the shared dispatcher; adds cache
    invalidation + Session Script recording."""
    from iidm_viewer.component_creation import create_component_bay as _shared
    # The shared dispatcher already validates + raises ValueError
    # on bad input; the Streamlit-side wrapper just adds the
    # session-state cache flush and the script recorder hook.
    _shared(network, component, fields)
    invalidate_on_topology_change(affects_geography=True)
    bay_fn = (
        "create_shunt_compensator_bay"
        if component == "Shunt Compensators"
        else CREATABLE_COMPONENTS[component]["bay_function"]
    )
    script_recorder.record_create_component_bay(component, bay_fn, fields)


# --- Branches (two-end connectables: lines + 2-winding transformers) ---
#
# All of these — the registry, the per-side locator builder, the
# same-substation check, the worker-routed creator — live in
# ``iidm_viewer.component_creation`` so the PySide6 and NiceGUI
# prototypes share them. Streamlit's wrapper below just adds the
# topology-cache invalidation.
from iidm_viewer.component_creation import (  # noqa: E402, F401  (re-exported)
    CREATABLE_BRANCHES,
    _BRANCH_SIDE_LOCATOR,
    _substations_of_busbars as _substations_of_bbs,  # legacy alias
    branch_side_locator_fields,
    validate_create_branch_fields,
)


def create_branch_bay(network, component: str, fields: dict):
    """Streamlit wrapper around the shared dispatcher; adds cache
    invalidation + Session Script recording."""
    from iidm_viewer.component_creation import create_branch_bay as _shared
    _shared(network, component, fields)
    invalidate_on_topology_change(affects_geography=True)
    bay_fn = CREATABLE_BRANCHES[component]["bay_function"]
    script_recorder.record_create_branch_bay(component, bay_fn, fields)


# --- Containers (substations, voltage levels, busbar sections) ---
#
# The registry, the _validate_voltage_level hook, the validator, and
# the worker-routed creator all live in ``iidm_viewer.component_creation``
# so the PySide6 and NiceGUI prototypes share them. Importing the
# module also runs its
# ``_VALIDATORS["_validate_voltage_level"] = _validate_voltage_level``
# side-effect — needed because validate_create_container_fields looks
# the hook up by name.
from iidm_viewer.component_creation import (  # noqa: E402, F401  (re-exported)
    CREATABLE_CONTAINERS,
    TOPOLOGY_KINDS,
    _validate_voltage_level,
    list_substations_df,
    next_free_node,
    validate_create_container_fields,
)


def create_container(network, component: str, fields: dict):
    """Streamlit wrapper around the shared dispatcher; adds cache invalidation."""
    from iidm_viewer.component_creation import create_container as _shared
    _shared(network, component, fields)
    invalidate_on_topology_change(affects_geography=True)
    script_recorder.record_create_container(
        component, spec["create_function"], clean
    )


def get_voltage_levels_df(network):
    """Cached VL listing for the Streamlit ``vl_selector``.

    The fetch + display-column derivation live in
    :mod:`iidm_viewer.network_loader` so the PySide6 + NiceGUI
    prototypes share them. Streamlit wraps with a per-network
    ``st.session_state`` cache to avoid re-fetching on every rerun.
    """
    cache = st.session_state.setdefault("_vl_lookup_cache", {})
    net_id = id(network)
    if cache.get("vl_df_id") == net_id and "vl_df" in cache:
        return cache["vl_df"]
    from iidm_viewer.network_loader import list_voltage_levels_for_selector
    df = list_voltage_levels_for_selector(network)
    cache["vl_df_id"] = net_id
    cache["vl_df"] = df
    return df


def filter_voltage_levels(vls_df, text):
    from iidm_viewer.network_loader import filter_voltage_levels as _shared
    return _shared(vls_df, text)


# --- Tap changers (ratio + phase) on existing 2-winding transformers ---
#
# Registry + validator + worker-routed dispatcher live in the shared
# ``iidm_viewer.component_creation`` module. The Streamlit wrapper adds the
# cache invalidation that every topology-affecting mutation needs.

from iidm_viewer.component_creation import (
    CREATABLE_TAP_CHANGERS,
    PTC_REGULATION_MODES,
    TRANSFORMER_SIDES,
    list_transformers_without_tap_changer,
    list_two_winding_transformers,
    validate_create_tap_changer_fields,
)


def create_tap_changer(
    network, kind: str, transformer_id: str, main_fields: dict, steps: list[dict]
):
    from iidm_viewer.component_creation import create_tap_changer as _shared
    _shared(network, kind, transformer_id, main_fields, steps)
    invalidate_on_topology_change(affects_geography=True)
    script_recorder.record_create_tap_changer(
        kind,
        method_name,
        transformer_id,
        main_fields,
        spec["step_columns"],
        spec["step_defaults"],
        steps,
    )


# --- Coupling device (switches tying two busbar sections together) ---
#
# Validator + worker-routed dispatcher live in the shared
# ``iidm_viewer.component_creation`` module so the PySide6 and NiceGUI
# prototypes share them. The Streamlit wrapper adds cache invalidation.

from iidm_viewer.component_creation import (  # noqa: E402, F401  (re-exported)
    list_node_breaker_vls_with_multi_bbs,
    validate_create_coupling_device_fields,
)


def create_coupling_device(
    network, bbs1: str, bbs2: str, switch_prefix: str | None = None
):
    from iidm_viewer.component_creation import create_coupling_device as _shared
    _shared(network, bbs1, bbs2, switch_prefix)
    invalidate_on_topology_change(affects_geography=True)
    script_recorder.record_create_coupling_device(bbs1, bbs2, switch_prefix)


# --- HVDC lines (attach to two existing converter stations) ---
#
# The registry, the converter-station lister, the validator and the
# worker-routed creator live in ``iidm_viewer.component_creation`` so
# the PySide6 and NiceGUI prototypes share them.
from iidm_viewer.component_creation import (  # noqa: E402, F401  (re-exported)
    CONVERTERS_MODES,
    CREATABLE_HVDC_LINES,
    list_converter_stations,
    validate_create_hvdc_line_fields,
)


def create_hvdc_line(network, fields: dict):
    """Streamlit wrapper around the shared dispatcher; adds cache invalidation."""
    from iidm_viewer.component_creation import create_hvdc_line as _shared
    _shared(network, fields)
    invalidate_on_topology_change(affects_geography=True)
    script_recorder.record_create_hvdc_line(fields)


# --- Reactive limits (min/max or per-P curve) on generators / VSC / batteries ---
#
# Registry + candidate lister + validator + worker-routed dispatcher live in the
# shared ``iidm_viewer.component_creation`` module. The Streamlit wrapper adds
# the cache invalidation that every topology-affecting mutation needs.

from iidm_viewer.component_creation import (  # noqa: E402, F401  (re-exported)
    REACTIVE_LIMITS_MODES,
    REACTIVE_LIMITS_TARGETS,
    list_reactive_limit_candidates,
    validate_create_reactive_limits_fields,
)


def create_reactive_limits(
    network, element_id: str, mode: str, payload: list[dict]
):
    from iidm_viewer.component_creation import create_reactive_limits as _shared
    _shared(network, element_id, mode, payload)
    invalidate_on_topology_change()
    script_recorder.record_create_reactive_limits(element_id, mode, payload)


# --- Operational limits (CURRENT / APPARENT_POWER / ACTIVE_POWER) ---
#
# Registry + candidate lister + validator + worker-routed dispatcher live in the
# shared ``iidm_viewer.component_creation`` module. The Streamlit wrapper adds
# the cache invalidation that every topology-affecting mutation needs.

from iidm_viewer.component_creation import (  # noqa: E402, F401  (re-exported)
    OPERATIONAL_LIMIT_SIDES,
    OPERATIONAL_LIMIT_TYPES,
    OPERATIONAL_LIMITS_TARGETS,
    PERMANENT_DURATION,
    list_operational_limit_candidates,
    validate_create_operational_limits_fields,
)


def create_operational_limits(
    network,
    element_id: str,
    side: str,
    limit_type: str,
    limits: list[dict],
    group_name: str = "DEFAULT",
):
    from iidm_viewer.component_creation import create_operational_limits as _shared
    _shared(network, element_id, side, limit_type, limits, group_name)
    invalidate_on_topology_change()
    script_recorder.record_create_operational_limits(
        element_id, side, limit_type, limits, group_name=group_name
    )


# --- Extensions (first-phase: attach extension rows to existing elements) ---
#
# pypowsybl exposes 27 network extensions. This registry wires up an initial
# set that pairs directly with equipment the app can already create, so users
# can fill out geographical position / controls / HVDC droop / ENTSO-E tags
# without hand-editing XML. Read-only browsing of every extension already
# lives in `extensions_explorer.py`; this module adds write support.
#
# Each schema entry contains:
#   label        — human-readable name shown in the form
#   detail       — help caption
#   index        — the pypowsybl index column for create_extensions (usually
#                  "id", but slackTerminal is indexed by "voltage_level_id")
#   targets      — dict of component label -> getter method name, used to
#                  populate the target dropdown from existing elements
#   fields       — list of {name, kind, required, default, help, options,
#                  optional_fill}. ``kind`` drives the input widget and type
#                  coercion; ``optional_fill`` marks columns that can be
#                  dropped from the dataframe when blank.

_EXT_KIND_COERCIONS = {
    "float": float,
    "int": int,
    "bool": bool,
    "str": str,
}


CREATABLE_EXTENSIONS: dict[str, dict] = {
    "substationPosition": {
        "label": "Substation position (lat/long)",
        "detail": "Geographical coordinates used by the map tab.",
        "index": "id",
        "targets": {"Substations": "get_substations"},
        "fields": [
            {"name": "latitude", "kind": "float", "required": True, "default": 0.0,
             "help": "Decimal degrees."},
            {"name": "longitude", "kind": "float", "required": True, "default": 0.0,
             "help": "Decimal degrees."},
        ],
    },
    "entsoeArea": {
        "label": "ENTSO-E area code",
        "detail": "Two-letter ENTSO-E country / area code (e.g. FR, DE).",
        "index": "id",
        "targets": {"Substations": "get_substations"},
        "fields": [
            {"name": "code", "kind": "str", "required": True, "default": "",
             "help": "ENTSO-E area code."},
        ],
    },
    "busbarSectionPosition": {
        "label": "Busbar section position (row / section)",
        "detail": "Drives the SLD layout: which busbar row and section the BBS sits on.",
        "index": "id",
        "targets": {"Busbar Sections": "get_busbar_sections"},
        "fields": [
            {"name": "busbar_index", "kind": "int", "required": True, "default": 1,
             "help": "1-based row index."},
            {"name": "section_index", "kind": "int", "required": True, "default": 1,
             "help": "1-based section index along the row."},
        ],
    },
    "position": {
        "label": "Connectable position (order / feeder name)",
        "detail": "Feeder order / direction used by the SLD; injections only in this phase.",
        "index": "id",
        "targets": {
            "Generators": "get_generators",
            "Loads": "get_loads",
            "Batteries": "get_batteries",
            "Shunt Compensators": "get_shunt_compensators",
            "Static VAR Compensators": "get_static_var_compensators",
            "LCC Converter Stations": "get_lcc_converter_stations",
            "VSC Converter Stations": "get_vsc_converter_stations",
        },
        "fields": [
            {"name": "order", "kind": "int", "required": True, "default": 10,
             "help": "Feeder order on the busbar."},
            {"name": "feeder_name", "kind": "str", "required": False, "default": "",
             "optional_fill": True, "help": "Feeder label (defaults to element id)."},
            {"name": "direction", "kind": "choice", "required": True, "default": "TOP",
             "options": ["TOP", "BOTTOM", "UNDEFINED"],
             "help": "Feeder direction on the SLD."},
            {"name": "side", "kind": "str", "required": False, "default": "",
             "optional_fill": True,
             "help": "Leave blank for injections. For branches use ONE or TWO."},
        ],
    },
    "slackTerminal": {
        "label": "Slack terminal",
        "detail": "Overrides the slack bus chosen by the load flow.",
        "index": "voltage_level_id",
        "targets": {"Voltage Levels": "get_voltage_levels"},
        "fields": [
            {"name": "bus_id", "kind": "str", "required": False, "default": "",
             "optional_fill": True,
             "help": "Bus view bus id. Fill either bus_id OR element_id, not both."},
            {"name": "element_id", "kind": "str", "required": False, "default": "",
             "optional_fill": True,
             "help": "Connectable id (e.g. a generator). Fill either this OR bus_id."},
        ],
    },
    "activePowerControl": {
        "label": "Active power control (participation / droop)",
        "detail": "Controls how the element participates in load-flow balancing.",
        "index": "id",
        "targets": {
            "Generators": "get_generators",
            "Batteries": "get_batteries",
        },
        "fields": [
            {"name": "participate", "kind": "bool", "required": True, "default": True,
             "help": "Whether the unit participates in balancing."},
            {"name": "droop", "kind": "float", "required": True, "default": 4.0,
             "help": "Droop coefficient (% or MW/Hz depending on provider)."},
            {"name": "participation_factor", "kind": "float", "required": False,
             "default": 1.0, "optional_fill": True,
             "help": "Proportional participation weight."},
            {"name": "min_target_p", "kind": "float", "required": False,
             "default": None, "optional_fill": True,
             "help": "Lower bound applied during balancing."},
            {"name": "max_target_p", "kind": "float", "required": False,
             "default": None, "optional_fill": True,
             "help": "Upper bound applied during balancing."},
        ],
    },
    "voltageRegulation": {
        "label": "Voltage regulation (battery)",
        "detail": "Enables voltage regulation on a battery (regulated element can be any bus).",
        "index": "id",
        "targets": {"Batteries": "get_batteries"},
        "fields": [
            {"name": "voltage_regulator_on", "kind": "bool", "required": True,
             "default": True, "help": "Whether regulation is active."},
            {"name": "target_v", "kind": "float", "required": True, "default": 0.0,
             "help": "Target voltage (kV)."},
            {"name": "regulated_element_id", "kind": "str", "required": False,
             "default": "", "optional_fill": True,
             "help": "Regulated terminal element (defaults to self)."},
        ],
    },
    "voltagePerReactivePowerControl": {
        "label": "V/Q slope (SVC)",
        "detail": "Slope (kV/MVar) applied by the SVC voltage regulator.",
        "index": "id",
        "targets": {"Static VAR Compensators": "get_static_var_compensators"},
        "fields": [
            {"name": "slope", "kind": "float", "required": True, "default": 0.01,
             "help": "Slope in kV/MVar."},
        ],
    },
    "standbyAutomaton": {
        "label": "Standby automaton (SVC)",
        "detail": "Standby mode and low/high voltage thresholds + setpoints.",
        "index": "id",
        "targets": {"Static VAR Compensators": "get_static_var_compensators"},
        "fields": [
            {"name": "standby", "kind": "bool", "required": True, "default": False,
             "help": "Whether the SVC is in standby."},
            {"name": "b0", "kind": "float", "required": True, "default": 0.0,
             "help": "Susceptance in standby mode (S)."},
            {"name": "low_voltage_threshold", "kind": "float", "required": True,
             "default": 0.0, "help": "Low voltage threshold (kV)."},
            {"name": "low_voltage_setpoint", "kind": "float", "required": True,
             "default": 0.0, "help": "Low voltage setpoint (kV)."},
            {"name": "high_voltage_threshold", "kind": "float", "required": True,
             "default": 0.0, "help": "High voltage threshold (kV)."},
            {"name": "high_voltage_setpoint", "kind": "float", "required": True,
             "default": 0.0, "help": "High voltage setpoint (kV)."},
        ],
    },
    "hvdcAngleDroopActivePowerControl": {
        "label": "HVDC AC emulation (angle droop)",
        "detail": "Active power set from an offset plus droop * (theta1 - theta2).",
        "index": "id",
        "targets": {"HVDC Lines": "get_hvdc_lines"},
        "fields": [
            {"name": "droop", "kind": "float", "required": True, "default": 0.0,
             "help": "Droop (MW/deg)."},
            {"name": "p0", "kind": "float", "required": True, "default": 0.0,
             "help": "Base active power offset (MW)."},
            {"name": "enabled", "kind": "bool", "required": True, "default": True,
             "help": "Whether AC emulation is active."},
        ],
    },
    "hvdcOperatorActivePowerRange": {
        "label": "HVDC operator active power range",
        "detail": "Operator-imposed power-flow bounds in each direction.",
        "index": "id",
        "targets": {"HVDC Lines": "get_hvdc_lines"},
        "fields": [
            {"name": "opr_from_cs1_to_cs2", "kind": "float", "required": True,
             "default": 0.0, "help": "Max flow from side 1 to side 2 (MW)."},
            {"name": "opr_from_cs2_to_cs1", "kind": "float", "required": True,
             "default": 0.0, "help": "Max flow from side 2 to side 1 (MW)."},
        ],
    },
    "entsoeCategory": {
        "label": "ENTSO-E category code (generator)",
        "detail": "Integer ENTSO-E category code.",
        "index": "id",
        "targets": {"Generators": "get_generators"},
        "fields": [
            {"name": "code", "kind": "int", "required": True, "default": 0,
             "help": "ENTSO-E category code."},
        ],
    },
}


def list_extensions_for_component(component: str) -> list[str]:
    """Return extension names whose ``targets`` mapping includes this component."""
    return [
        name for name, schema in CREATABLE_EXTENSIONS.items()
        if component in schema["targets"]
    ]


def list_extension_candidates(network, extension_name: str, component: str) -> list[str]:
    """Return ids of existing elements that can carry the selected extension."""
    schema = CREATABLE_EXTENSIONS.get(extension_name)
    if not schema:
        return []
    getter = schema["targets"].get(component)
    if not getter:
        return []
    try:
        df = getattr(network, getter)()
    except Exception:
        return []
    return sorted(df.index.tolist())


def validate_create_extension_fields(extension_name: str, fields: dict) -> list[str]:
    schema = CREATABLE_EXTENSIONS.get(extension_name)
    if not schema:
        return [f"Unknown extension: {extension_name!r}"]
    errors: list[str] = []
    for fdef in schema["fields"]:
        name = fdef["name"]
        if fdef.get("required") and fields.get(name) in (None, ""):
            errors.append(f"{name} is required.")
    if extension_name == "slackTerminal":
        bus = fields.get("bus_id") or ""
        elem = fields.get("element_id") or ""
        if bool(bus) == bool(elem):
            errors.append(
                "Exactly one of bus_id or element_id must be filled."
            )
    if extension_name == "busbarSectionPosition":
        for k in ("busbar_index", "section_index"):
            v = fields.get(k)
            if v is not None and int(v) < 0:
                errors.append(f"{k} must be >= 0.")
    if extension_name == "activePowerControl":
        mn = fields.get("min_target_p")
        mx = fields.get("max_target_p")
        if mn is not None and mx is not None and mx < mn:
            errors.append("max_target_p must be >= min_target_p.")
    return errors


def create_extension(
    network, extension_name: str, target_id: str, fields: dict
):
    """Attach a single extension row to an existing element.

    Validates against the registry, coerces types, drops optional columns when
    left blank, and routes the pypowsybl ``create_extensions`` call through
    the worker thread.
    """
    schema = CREATABLE_EXTENSIONS.get(extension_name)
    if not schema:
        raise ValueError(f"Unknown extension: {extension_name!r}")
    if not target_id:
        raise ValueError("Target id is required.")
    errors = validate_create_extension_fields(extension_name, fields)
    if errors:
        raise ValueError("; ".join(errors))

    row: dict[str, object] = {}
    for fdef in schema["fields"]:
        name = fdef["name"]
        raw_val = fields.get(name)
        if raw_val in (None, "") and fdef.get("optional_fill"):
            continue
        coercer = _EXT_KIND_COERCIONS.get(fdef["kind"])
        if fdef["kind"] == "choice":
            row[name] = str(raw_val)
        elif coercer is bool:
            row[name] = bool(raw_val)
        elif coercer is not None and raw_val not in (None, ""):
            row[name] = coercer(raw_val)
        else:
            row[name] = raw_val

    index_col = schema["index"]
    df = pd.DataFrame({k: [v] for k, v in row.items()},
                      index=pd.Index([target_id], name=index_col))

    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        raw.create_extensions(extension_name, df)

    run(_do_create)
    invalidate_on_topology_change()
    script_recorder.record_create_extension(extension_name, target_id, row, index_col)


# --- Secondary voltage control (network-level, two dataframes) ---
#
# The bus-id lister, unit lister, validator, and worker-routed dispatcher
# live in the shared ``iidm_viewer.component_creation`` module so the
# PySide6 and NiceGUI prototypes share them. The Streamlit wrapper adds
# the cache invalidation that every topology-affecting mutation needs.

from iidm_viewer.component_creation import (  # noqa: E402, F401  (re-exported)
    list_bus_ids,
    list_unit_candidates,
    validate_secondary_voltage_control,
)


def create_secondary_voltage_control(
    network, zones: list[dict], units: list[dict]
):
    from iidm_viewer.component_creation import (
        create_secondary_voltage_control as _shared,
    )
    _shared(network, zones, units)
    invalidate_on_topology_change()
    script_recorder.record_create_secondary_voltage_control(zones, units)


# --- Security Analysis ---

def build_n1_contingencies(
    network,
    element_type: str,
    nominal_v_set: set | None = None,
) -> list[dict]:
    """Build N-1 contingency definitions for every element of *element_type*.

    If *nominal_v_set* is provided, only include elements where at least one
    terminal voltage level has a nominal_v in the set.  Both the element table
    and the VL table are fetched in a single worker call.

    Returns list of {"id": "N1_<element_id>", "element_id": element_id}.
    """
    if element_type == "Lines":
        getter = "get_lines"
    elif element_type == "2-Winding Transformers":
        getter = "get_2_windings_transformers"
    else:
        return []

    vl_cols = ["voltage_level1_id", "voltage_level2_id"]
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        elem_df = getattr(raw, getter)(attributes=vl_cols)
        vl_df = raw.get_voltage_levels(attributes=["nominal_v"]) if nominal_v_set else None
        return elem_df, vl_df

    elem_df, vl_df = run(_gather)

    if elem_df.empty:
        return []

    if nominal_v_set and vl_df is not None and not vl_df.empty:
        def _matches(row):
            for col in vl_cols:
                vl_id = row.get(col)
                if vl_id and vl_id in vl_df.index:
                    if vl_df.at[vl_id, "nominal_v"] in nominal_v_set:
                        return True
            return False
        elem_df = elem_df[elem_df.apply(_matches, axis=1)]

    return [
        {"id": f"N1_{eid}", "element_id": eid, "element_ids": [eid]}
        for eid in elem_df.index
    ]


def build_n2_contingencies(
    network,
    element_type: str,
    nominal_v_set: set | None = None,
) -> list[dict]:
    """Build N-2 contingency definitions for every unique pair of elements
    of *element_type* whose terminals touch one of *nominal_v_set*.

    Pairs are unordered (A, B) with A < B by element id. Returns a list of
    ``{"id": "N2_<a>_<b>", "element_ids": [a, b]}``.
    """
    from itertools import combinations

    n1 = build_n1_contingencies(network, element_type, nominal_v_set)
    ids = sorted(c["element_id"] for c in n1)
    return [
        {"id": f"N2_{a}_{b}", "element_ids": [a, b]}
        for a, b in combinations(ids, 2)
    ]


def _apply_action(analysis, action: dict) -> None:
    """Dispatch a single action dict to the right pypowsybl ``add_*_action`` call.

    Supported action types (extend here to add more):

    - ``SWITCH``: ``switch_id``, ``open``
    - ``TERMINALS_CONNECTION``: ``element_id``, ``opening``, optional ``side``
    - ``GENERATOR_ACTIVE_POWER``: ``generator_id``, ``is_relative``, ``active_power``
    - ``LOAD_ACTIVE_POWER``: ``load_id``, ``is_relative``, ``active_power``
    - ``PHASE_TAP_CHANGER_POSITION``: ``transformer_id``, ``is_relative``, ``tap_position``,
      optional ``side``
    - ``RATIO_TAP_CHANGER_POSITION``: ``transformer_id``, ``is_relative``, ``tap_position``,
      optional ``side``
    - ``SHUNT_COMPENSATOR_POSITION``: ``shunt_id``, ``section``
    """
    from pypowsybl._pypowsybl import Side

    action_id = action["action_id"]
    atype = action["type"]
    side = Side.__members__.get(action.get("side", "NONE"), Side.NONE)

    if atype == "SWITCH":
        analysis.add_switch_action(
            action_id, action["switch_id"], bool(action["open"])
        )
    elif atype == "TERMINALS_CONNECTION":
        analysis.add_terminals_connection_action(
            action_id,
            action["element_id"],
            side=side,
            opening=bool(action.get("opening", True)),
        )
    elif atype == "GENERATOR_ACTIVE_POWER":
        analysis.add_generator_active_power_action(
            action_id,
            action["generator_id"],
            bool(action["is_relative"]),
            float(action["active_power"]),
        )
    elif atype == "LOAD_ACTIVE_POWER":
        analysis.add_load_active_power_action(
            action_id,
            action["load_id"],
            bool(action["is_relative"]),
            float(action["active_power"]),
        )
    elif atype == "PHASE_TAP_CHANGER_POSITION":
        analysis.add_phase_tap_changer_position_action(
            action_id,
            action["transformer_id"],
            bool(action["is_relative"]),
            int(action["tap_position"]),
            side=side,
        )
    elif atype == "RATIO_TAP_CHANGER_POSITION":
        analysis.add_ratio_tap_changer_position_action(
            action_id,
            action["transformer_id"],
            bool(action["is_relative"]),
            int(action["tap_position"]),
            side=side,
        )
    elif atype == "SHUNT_COMPENSATOR_POSITION":
        analysis.add_shunt_compensator_position_action(
            action_id,
            action["shunt_id"],
            int(action["section"]),
        )
    else:
        raise ValueError(f"Unsupported action type: {atype!r}")


def run_security_analysis(
    network,
    contingencies: list[dict],
    monitored_elements: list[dict] | None = None,
    limit_reductions: list[dict] | None = None,
    actions: list[dict] | None = None,
    operator_strategies: list[dict] | None = None,
    contingencies_json_paths: list[str] | None = None,
    actions_json_paths: list[str] | None = None,
    operator_strategies_json_paths: list[str] | None = None,
) -> dict:
    """Run AC security analysis on the worker thread.

    *contingencies* is a list of {"id": str, "element_id": str} dicts produced
    by :func:`build_n1_contingencies` (or any compatible builder).

    *monitored_elements* is an optional list of dicts, one per
    ``add_monitored_elements`` call:
        {
            "contingency_context_type": "ALL" | "NONE" | "SPECIFIC",
            "contingency_ids": list[str] | None,
            "branch_ids": list[str] | None,
            "voltage_level_ids": list[str] | None,
            "three_windings_transformer_ids": list[str] | None,
        }

    *limit_reductions* is an optional list of dicts, one per reduction entry:
        {
            "limit_type": "CURRENT",
            "permanent": bool,
            "temporary": bool,
            "value": float,
            "contingency_context": "ALL" | "NONE" | "SPECIFIC" (optional),
            "min_temporary_duration": int (optional),
            "max_temporary_duration": int (optional),
            "country": str (optional),
            "min_voltage": float (optional),
            "max_voltage": float (optional),
        }

    *actions* is an optional list of remedial-action dicts dispatched by
    :func:`_apply_action` — see that function for the per-type shape.

    *operator_strategies* is an optional list of dicts, one per
    ``add_operator_strategy`` call:
        {
            "operator_strategy_id": str,
            "contingency_id": str,
            "action_ids": list[str],
            "condition_type": "TRUE_CONDITION" | "ANY_VIOLATION_CONDITION"
                              | "ALL_VIOLATION_CONDITION"
                              | "AT_LEAST_ONE_VIOLATION_CONDITION",
        }

    Returns a serialized dict safe for ``st.session_state``:
        {
            "pre_status":    str,
            "pre_violations": DataFrame,
            "pre_branch_results": DataFrame,
            "pre_bus_results": DataFrame,
            "pre_3wt_results": DataFrame,
            "post": {contingency_id: {
                "status": str,
                "limit_violations": DataFrame,
                "branch_results": DataFrame,
                "bus_results": DataFrame,
                "three_windings_transformer_results": DataFrame,
            }},
            "operator_strategies": {strategy_id: {
                "status": str,
                "limit_violations": DataFrame,
                "branch_results": DataFrame,
                "bus_results": DataFrame,
                "three_windings_transformer_results": DataFrame,
                "contingency_id": str,
                "action_ids": list[str],
            }},
            "contingencies": list[dict],
        }
    """
    from iidm_viewer.lf_parameters import get_lf_parameters

    raw = object.__getattribute__(network, "_obj")
    generic, provider = get_lf_parameters()
    monitored_elements = monitored_elements or []
    limit_reductions = limit_reductions or []
    actions = actions or []
    operator_strategies = operator_strategies or []
    contingencies_json_paths = contingencies_json_paths or []
    actions_json_paths = actions_json_paths or []
    operator_strategies_json_paths = operator_strategies_json_paths or []

    def _run_sa():
        import pypowsybl.security as sa
        import pypowsybl.loadflow as lf
        from pypowsybl.flowdecomposition import ContingencyContextType
        from pypowsybl._pypowsybl import ConditionType, ViolationType

        analysis = sa.create_analysis()
        for c in contingencies:
            eids = list(c.get("element_ids") or ([c["element_id"]] if "element_id" in c else []))
            if len(eids) == 1:
                analysis.add_single_element_contingency(eids[0], c["id"])
            elif len(eids) > 1:
                analysis.add_multiple_elements_contingency(eids, c["id"])
        for p in contingencies_json_paths:
            analysis.add_contingencies_from_json_file(p)

        for me in monitored_elements:
            ctx_name = me.get("contingency_context_type", "ALL")
            ctx = ContingencyContextType.__members__.get(ctx_name, ContingencyContextType.ALL)
            analysis.add_monitored_elements(
                contingency_context_type=ctx,
                contingency_ids=me.get("contingency_ids") or None,
                branch_ids=me.get("branch_ids") or None,
                voltage_level_ids=me.get("voltage_level_ids") or None,
                three_windings_transformer_ids=me.get("three_windings_transformer_ids") or None,
            )

        if limit_reductions:
            # ``limit_type`` is the index column in pypowsybl's metadata; the
            # C bridge rejects it as a regular column.
            lr_df = pd.DataFrame(limit_reductions).set_index("limit_type")
            analysis.add_limit_reductions(lr_df)

        for action in actions:
            _apply_action(analysis, action)
        for p in actions_json_paths:
            analysis.add_actions_from_json_file(p)

        for strat in operator_strategies:
            cond_name = strat.get("condition_type", "TRUE_CONDITION")
            cond = ConditionType.__members__.get(cond_name, ConditionType.TRUE_CONDITION)
            vtype_names = strat.get("violation_types") or []
            vtypes = [
                ViolationType.__members__[n]
                for n in vtype_names
                if n in ViolationType.__members__
            ] or None
            vsubjects = list(strat.get("violation_subject_ids") or []) or None
            analysis.add_operator_strategy(
                strat["operator_strategy_id"],
                strat["contingency_id"],
                list(strat["action_ids"]),
                condition_type=cond,
                violation_subject_ids=vsubjects,
                violation_types=vtypes,
            )
        for p in operator_strategies_json_paths:
            analysis.add_operator_strategies_from_json_file(p)

        lf_params = lf.Parameters(**generic)
        if provider:
            lf_params.provider_parameters = {k: str(v) for k, v in provider.items()}
        params = sa.Parameters(load_flow_parameters=lf_params)

        result = analysis.run_ac(raw, parameters=params)

        # Serialize the native pypowsybl JSON view so the caller can download
        # it after the result object goes out of scope on the worker.
        import tempfile as _tempfile
        import os as _os

        with _tempfile.NamedTemporaryFile(suffix=".json", delete=False) as _tf:
            _json_path = _tf.name
        try:
            result.export_to_json(_json_path)
            with open(_json_path, "rb") as _fh:
                json_export_bytes = _fh.read()
        finally:
            try:
                _os.unlink(_json_path)
            except OSError:
                pass

        # Serialize all results before they leave the worker thread
        pre_result = result.pre_contingency_result
        pre_viol = pd.DataFrame(pre_result.limit_violations)

        def _select(
            df: pd.DataFrame,
            contingency_id: str | None,
            strategy_id: str = "",
        ) -> pd.DataFrame:
            """Slice a multi-indexed result DF by (contingency_id, operator_strategy_id).

            Index levels are ``(contingency_id, operator_strategy_id, element_id)``.
            ``""`` means "no contingency" for level 0 and "no strategy" for level 1.
            Returns an empty DataFrame if *df* is empty or the keys are absent.
            """
            if df is None or df.empty:
                return pd.DataFrame()
            try:
                if isinstance(df.index, pd.MultiIndex):
                    cid_key = "" if contingency_id is None else contingency_id
                    lvl0 = df.index.get_level_values(0)
                    mask = lvl0 == cid_key
                    if df.index.nlevels >= 3:
                        lvl1 = df.index.get_level_values(1)
                        mask = mask & (lvl1 == strategy_id)
                        return df[mask].reset_index(level=[0, 1], drop=True)
                    return df[mask].reset_index(level=0, drop=True)
                return df.copy()
            except Exception:
                return pd.DataFrame()

        branch_all = pd.DataFrame(result.branch_results) if result.branch_results is not None else pd.DataFrame()
        bus_all = pd.DataFrame(result.bus_results) if result.bus_results is not None else pd.DataFrame()
        t3w_all = (
            pd.DataFrame(result.three_windings_transformer_results)
            if result.three_windings_transformer_results is not None
            else pd.DataFrame()
        )

        post: dict = {}
        for cid, cr in result.post_contingency_results.items():
            post[cid] = {
                "status": cr.status.name,
                "limit_violations": pd.DataFrame(cr.limit_violations),
                "branch_results": _select(branch_all, cid),
                "bus_results": _select(bus_all, cid),
                "three_windings_transformer_results": _select(t3w_all, cid),
            }

        os_results: dict = {}
        for sid, osr in result.operator_strategy_results.items():
            strat = next(
                (s for s in operator_strategies if s["operator_strategy_id"] == sid),
                None,
            )
            cid = strat["contingency_id"] if strat else None
            os_results[sid] = {
                "status": osr.status.name,
                "limit_violations": pd.DataFrame(osr.limit_violations),
                "branch_results": _select(branch_all, cid, sid),
                "bus_results": _select(bus_all, cid, sid),
                "three_windings_transformer_results": _select(t3w_all, cid, sid),
                "contingency_id": cid,
                "action_ids": list(strat["action_ids"]) if strat else [],
            }

        return {
            "pre_status": pre_result.status.name,
            "pre_violations": pre_viol,
            "pre_branch_results": _select(branch_all, None),
            "pre_bus_results": _select(bus_all, None),
            "pre_3wt_results": _select(t3w_all, None),
            "post": post,
            "operator_strategies": os_results,
            "contingencies": contingencies,
            "json_export": json_export_bytes,
        }

    sa_result = run(_run_sa)
    script_recorder.record_run_security_analysis(
        contingencies,
        monitored_elements,
        limit_reductions,
        actions,
        operator_strategies,
        contingencies_json_paths,
        actions_json_paths,
        operator_strategies_json_paths,
        generic,
        provider,
    )
    return sa_result


# --- Short Circuit Analysis ---

def build_bus_faults(
    network,
    nominal_v_set: set | None = None,
    fault_type: str = "THREE_PHASE",
) -> list[dict]:
    """Build a bus-fault definition for every bus, optionally filtered by nominal voltage.

    Both the bus table and the VL table are fetched in a single worker call.

    Returns list of {"id": "SC_<bus_id>", "element_id": bus_id, "fault_type": fault_type}.
    """
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        buses = raw.get_buses(attributes=["voltage_level_id"])
        vl_df = raw.get_voltage_levels(attributes=["nominal_v"]) if nominal_v_set else None
        return buses, vl_df

    buses, vl_df = run(_gather)

    if buses.empty:
        return []

    if nominal_v_set and vl_df is not None and not vl_df.empty:
        def _matches(row):
            vl_id = row.get("voltage_level_id")
            if vl_id and vl_id in vl_df.index:
                return vl_df.at[vl_id, "nominal_v"] in nominal_v_set
            return False
        buses = buses[buses.apply(_matches, axis=1)]

    return [
        {"id": f"SC_{bid}", "element_id": bid, "fault_type": fault_type}
        for bid in buses.index
    ]


def run_short_circuit_analysis(network, faults: list[dict], sc_params: dict | None = None) -> dict:
    """Run short circuit analysis on the worker thread.

    *faults* is a list of {"id": str, "element_id": bus_id, "fault_type": str} dicts
    produced by :func:`build_bus_faults` (or any compatible builder).

    *sc_params* is a plain dict of scalar options read from the main thread:
        {
            "study_type": "SUB_TRANSIENT" | "TRANSIENT",
            "with_feeder_result": bool,
            "with_limit_violations": bool,
            "min_voltage_drop_proportional_threshold": float,
        }

    Returns a serialized dict safe for ``st.session_state``:
        {
            "fault_results": {fault_id: {
                "status": str,
                "short_circuit_power_mva": float | None,
                "current_kA": float | None,
                "feeder_results": DataFrame,
                "limit_violations": DataFrame,
            }},
            "faults": list[dict],
        }
    """
    raw = object.__getattribute__(network, "_obj")
    sc_params = sc_params or {}

    def _run_sc():
        import pypowsybl.shortcircuit as sc

        analysis = sc.create_analysis()
        for f in faults:
            analysis.set_bus_fault(f["id"], f["element_id"], 0.0, 0.0)

        params = sc.Parameters(
            study_type=sc.ShortCircuitStudyType.__members__.get(
                sc_params.get("study_type", "SUB_TRANSIENT"),
                sc.ShortCircuitStudyType.SUB_TRANSIENT,
            ),
            with_feeder_result=sc_params.get("with_feeder_result", True),
            with_limit_violations=sc_params.get("with_limit_violations", True),
            min_voltage_drop_proportional_threshold=float(
                sc_params.get("min_voltage_drop_proportional_threshold", 0.0)
            ),
        )

        result = analysis.run(raw, parameters=params)

        # Serialize all results before they leave the worker thread
        fr_df = result.fault_results          # DataFrame indexed by fault_id
        feeder_df_all = result.feeder_results  # flat DataFrame, may be multi-indexed
        viol_df_all = result.limit_violations  # flat DataFrame, may be multi-indexed

        def _filter_by_fault(df: pd.DataFrame, fid: str) -> pd.DataFrame:
            if df.empty:
                return pd.DataFrame()
            try:
                if isinstance(df.index, pd.MultiIndex):
                    lvl_vals = df.index.get_level_values(0)
                    return df[lvl_vals == fid].reset_index(drop=True)
                return df[df.index == fid].reset_index(drop=True)
            except Exception:
                return pd.DataFrame()

        fault_results: dict = {}
        for f in faults:
            fid = f["id"]
            if fid in fr_df.index:
                row = fr_df.loc[fid]
                status_val = row.get("status", "UNKNOWN")
                status_str = status_val.name if hasattr(status_val, "name") else str(status_val)
                pwr_raw = row.get("short_circuit_power", None)
                pwr = float(pwr_raw) if pwr_raw is not None and pd.notna(pwr_raw) else None
                cur_raw = row.get("current", None)
                cur_a = float(cur_raw) if cur_raw is not None and pd.notna(cur_raw) else None
                cur_ka = cur_a / 1000.0 if cur_a is not None else None
            else:
                status_str = "UNKNOWN"
                pwr = None
                cur_ka = None

            fault_results[fid] = {
                "status": status_str,
                "short_circuit_power_mva": pwr,
                "current_kA": cur_ka,
                "feeder_results": _filter_by_fault(feeder_df_all, fid),
                "limit_violations": _filter_by_fault(viol_df_all, fid),
            }

        return {
            "fault_results": fault_results,
            "faults": faults,
        }

    sc_result = run(_run_sc)
    script_recorder.record_run_short_circuit_analysis(faults, sc_params)
    return sc_result


def compute_target_v_q_sensitivities(network, gen_ids):
    """Return ``{gen_id: (dq_dv, q_ref) | None}`` for ``gen_ids``.

    Missing entries are computed in one batched AC sensitivity analysis on
    the powsybl worker thread (one LF factorization shared across every
    generator, plus one RHS solve per gen). Results are cached per
    ``(net_key, lf_gen, gen_id)`` in ``st.session_state["_dq_dv_cache"]``.
    The intended use is to call this once per rerun with every displayed
    PV generator so that selectbox navigation through them is instant.
    """
    cache = st.session_state.setdefault("_dq_dv_cache", {})
    raw = object.__getattribute__(network, "_obj")
    net_id = id(raw)
    lf_gen = st.session_state.get("_lf_gen", 0)

    gen_ids = list(gen_ids)
    missing = [g for g in gen_ids if (net_id, lf_gen, g) not in cache]

    if missing:
        def _run_sensitivity():
            try:
                import pypowsybl.sensitivity as sens
                from pypowsybl.sensitivity import (
                    ContingencyContextType,
                    SensitivityFunctionType,
                    SensitivityVariableType,
                )
                analysis = sens.create_ac_analysis()
                analysis.add_factor_matrix(
                    missing, missing, [],
                    ContingencyContextType.NONE,
                    SensitivityFunctionType.BUS_REACTIVE_POWER,
                    SensitivityVariableType.BUS_TARGET_VOLTAGE,
                )
                result = analysis.run(raw)
                sens_matrix = result.get_sensitivity_matrix()
                ref_matrix = result.get_reference_matrix()
                out = {}
                for gid in missing:
                    try:
                        out[gid] = (
                            float(sens_matrix.loc[gid, gid]),
                            float(ref_matrix.loc["reference_values", gid]),
                        )
                    except Exception:
                        out[gid] = None
                return out
            except Exception:
                return {gid: None for gid in missing}

        new_results = run(_run_sensitivity)
        for gid, val in new_results.items():
            cache[(net_id, lf_gen, gid)] = val

    return {g: cache[(net_id, lf_gen, g)] for g in gen_ids}


def compute_target_v_q_sensitivity(network, gen_id):
    """Single-gen convenience wrapper around ``compute_target_v_q_sensitivities``."""
    return compute_target_v_q_sensitivities(network, [gen_id]).get(gen_id)
