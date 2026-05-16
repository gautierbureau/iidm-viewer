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
    # Hook the Session Script recorder into per-tab session state so
    # the op log lives alongside the rest of the Streamlit state.
    # PySide6 / NiceGUI keep the recorder's default module-level dict.
    script_recorder.set_store(st.session_state)


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
    script_recorder.record_load_network(
        uploaded_file.name, parameters or {}, post_processors or [],
    )
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
    script_recorder.record_create_empty(network_id)
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
# The registry, the worker-routed remove / update wrappers and the
# read-only set live in :mod:`iidm_viewer.extensions_data` so the
# PySide6 and NiceGUI prototypes share them. Streamlit re-exports
# ``EDITABLE_EXTENSIONS`` and wraps the two mutators with the
# session-state cache invalidation that the rest of the app needs.

from iidm_viewer.extensions_data import (  # noqa: E402, F401  (re-exported)
    EDITABLE_EXTENSIONS,
    READONLY_EXTENSIONS,
)


def remove_extension(network, extension_name: str, ids: list):
    """Remove extension rows from the network on the worker thread."""
    from iidm_viewer.extensions_data import (
        remove_extension as _shared_remove_extension,
    )
    _shared_remove_extension(network, extension_name, ids)
    invalidate_on_topology_change()


def update_extension(network, extension_name: str, changes_df):
    """Apply a DataFrame of changes to an extension via ``update_extensions``."""
    from iidm_viewer.extensions_data import (
        update_extension as _shared_update_extension,
    )
    _shared_update_extension(network, extension_name, changes_df)
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
    """Streamlit wrapper around the shared dispatcher; adds cache
    invalidation + Session Script recording."""
    from iidm_viewer.component_creation import create_container as _shared
    _shared(network, component, fields)
    invalidate_on_topology_change(affects_geography=True)
    script_recorder.record_create_container(
        component,
        CREATABLE_CONTAINERS[component]["create_function"],
        fields,
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
    from iidm_viewer.component_creation import (
        CREATABLE_TAP_CHANGERS,
        create_tap_changer as _shared,
    )
    _shared(network, kind, transformer_id, main_fields, steps)
    invalidate_on_topology_change(affects_geography=True)
    spec = CREATABLE_TAP_CHANGERS[kind]
    script_recorder.record_create_tap_changer(
        kind,
        spec["create_method"],
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
# The registry, candidate lister, validator and worker-routed
# dispatcher live in :mod:`iidm_viewer.extension_creation` so the
# PySide6 and NiceGUI prototypes share them. Streamlit re-exports
# ``CREATABLE_EXTENSIONS`` + the helpers and wraps ``create_extension``
# with the session-state cache invalidation that the rest of the app
# expects.

from iidm_viewer.extension_creation import (  # noqa: E402, F401  (re-exported)
    CREATABLE_EXTENSIONS,
    list_extension_candidates,
    list_extensions_for_component,
    validate_create_extension_fields,
)


def create_extension(network, extension_name: str, target_id: str, fields: dict):
    from iidm_viewer.extension_creation import (
        CREATABLE_EXTENSIONS,
        create_extension as _shared,
    )
    _shared(network, extension_name, target_id, fields)
    invalidate_on_topology_change()
    # The shared dispatcher trims + coerces ``fields`` internally before
    # calling pypowsybl; the recording just needs the user-supplied
    # ``fields`` and the schema's index column.
    index_col = CREATABLE_EXTENSIONS[extension_name]["index"]
    script_recorder.record_create_extension(
        extension_name, target_id, fields, index_col,
    )


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
#
# Pipeline + builders + runner all live in the framework-agnostic
# ``iidm_viewer.security_analysis`` module so the PySide6 and NiceGUI
# prototypes share them. ``run_security_analysis`` already calls
# ``script_recorder.record_run_security_analysis`` on the shared
# side, so no Streamlit-side wrapper is needed.
from iidm_viewer.security_analysis import (  # noqa: F401, E402
    apply_action as _apply_action,
    build_n1_contingencies,
    build_n2_contingencies,
    run_security_analysis,
)


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

    Thin Streamlit cache around
    :func:`iidm_viewer.reactive_curves.compute_target_v_q_sensitivities`
    (which owns the worker-routed sensitivity call). Results are cached
    per ``(net_id, lf_gen, gen_id)`` in
    ``st.session_state["_dq_dv_cache"]`` so a selectbox-only rerun
    through PV generators reuses the previous factorization.
    """
    from iidm_viewer.reactive_curves import (
        compute_target_v_q_sensitivities as _shared_compute,
    )

    cache = st.session_state.setdefault("_dq_dv_cache", {})
    raw = object.__getattribute__(network, "_obj")
    net_id = id(raw)
    lf_gen = st.session_state.get("_lf_gen", 0)

    gen_ids = list(gen_ids)
    missing = [g for g in gen_ids if (net_id, lf_gen, g) not in cache]
    if missing:
        for gid, val in _shared_compute(network, missing).items():
            cache[(net_id, lf_gen, gid)] = val
    return {g: cache[(net_id, lf_gen, g)] for g in gen_ids}


def compute_target_v_q_sensitivity(network, gen_id):
    """Single-gen convenience wrapper around ``compute_target_v_q_sensitivities``."""
    return compute_target_v_q_sensitivities(network, [gen_id]).get(gen_id)
