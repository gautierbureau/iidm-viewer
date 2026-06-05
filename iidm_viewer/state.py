import pandas as pd
import streamlit as st

from iidm_viewer.app_state import AppState as _BaseAppState
from iidm_viewer.powsybl_worker import NetworkProxy, run
from iidm_viewer import network_loader
from iidm_viewer.caches import (
    backend as _caches_backend,
    invalidate_on_load_flow,
    invalidate_on_network_replace,
    invalidate_on_topology_change,
)
from iidm_viewer.loadflow import LoadFlowResult, run_ac
from iidm_viewer import script_recorder


class AppState(_BaseAppState):
    """Streamlit-flavoured :class:`iidm_viewer.app_state.AppState`.

    Plugs ``st.session_state`` in as the storage backend so the
    inherited properties (``network``, ``selected_vl``,
    ``last_report_json``, the persisted LF / import overrides) read /
    write the same session-state keys the existing module-level
    functions in this file already use. Cache backend is the shared
    Streamlit one from :mod:`iidm_viewer.caches`.

    Notification hooks are no-ops because Streamlit's rerun model
    fans state changes out implicitly — every tab re-reads on its
    next rerun, so no listener registry is needed.

    The historical module-level functions (``load_network``,
    ``run_loadflow``, …) delegate to a session-scoped singleton of
    this class via :func:`app_state`, so the unified API and the
    legacy callers share one source of truth for every field.
    """

    # Translate AppState field names to the legacy session-state keys
    # the existing Streamlit code already uses. ``last_report_json``
    # historically lives at ``_lf_report_json``; keep it that way so
    # the LF report dialog and any other reader keeps finding it.
    _STREAMLIT_KEY_MAP = {
        "last_report_json": "_lf_report_json",
    }

    def __init__(self) -> None:
        super().__init__()
        # Swap in the Streamlit-backed cache backend (the singleton
        # used by the rest of ``iidm_viewer.caches``).
        self.cache_backend = _caches_backend

    # Storage hooks — back the AppState fields with ``st.session_state``
    # so the unified API and the legacy module-level functions share
    # one source of truth.
    def _get(self, key: str, default=None):
        return st.session_state.get(self._STREAMLIT_KEY_MAP.get(key, key), default)

    def _set(self, key: str, value) -> None:
        st.session_state[self._STREAMLIT_KEY_MAP.get(key, key)] = value

    # Notification hooks — no-ops. Streamlit reruns on every interaction
    # and every tab re-reads ``st.session_state`` on its next render, so
    # there's no listener registry to fire.
    def _emit_network_changed(self, network) -> None:
        pass

    def _emit_selected_vl_changed(self, vl_id) -> None:
        pass

    def _emit_loadflow_completed(self, result) -> None:
        pass

    def _run_ac(self, network, generic_params, provider_params) -> LoadFlowResult:
        # Resolves via this module's ``run_ac`` import so tests that
        # ``monkeypatch.setattr("iidm_viewer.state.run_ac", …)`` still
        # intercept the call.
        return run_ac(network, generic_params, provider_params)

    # ------------------------------------------------------------------
    # Streamlit-specific lifecycle extras
    # ------------------------------------------------------------------
    def install_network(self, network) -> None:
        """Install a network, plus the Streamlit-specific session-state
        housekeeping the legacy ``load_network`` / ``create_empty_network``
        used to do inline:

        * bump ``vl_selector_gen`` so the VL picker widget remounts;
        * pop ``_vl_set_by_click`` so a stale post-click flag doesn't carry over;
        * pop ``_export_*`` and ``va_nom_select`` so the previous network's
          widget state doesn't leak into the new one;
        * delete the per-method ``_change_log_*`` / ``_removal_log_*`` /
          ``_ext_change_log_*`` / ``_ext_removal_log_*`` / ``_export_cache_*``
          session-state keys (the Streamlit-only per-method change log
          shape; the shared ``self.change_log`` is reset by ``super()``).

        The shared cache invalidation, selected-VL reset and default-VL
        emission happen inside ``super().install_network``.
        """
        st.session_state["vl_selector_gen"] = (
            st.session_state.get("vl_selector_gen", 0) + 1
        )
        for k in ("_vl_set_by_click", "_export_bytes", "_export_fmt",
                  "_export_ext", "va_nom_select"):
            st.session_state.pop(k, None)
        for k in [
            key for key in list(st.session_state)
            if (
                key.startswith("_change_log_")
                or key.startswith("_removal_log_")
                or key.startswith("_ext_change_log_")
                or key.startswith("_ext_removal_log_")
                or key.startswith("_export_cache_")
            )
        ]:
            del st.session_state[k]
        super().install_network(network)


def app_state() -> AppState:
    """Return the per-session :class:`AppState` singleton.

    Created lazily on first call inside the current Streamlit session
    and stashed in ``st.session_state``. The AppState's stored fields
    (network, selected_vl, …) live in ``st.session_state`` directly
    via the ``_get`` / ``_set`` overrides, so any code reaching for
    those keys keeps working; the singleton itself carries the
    ``change_log`` instance and the cache backend reference.
    """
    instance = st.session_state.get("_app_state_instance")
    if instance is None:
        instance = AppState()
        st.session_state["_app_state_instance"] = instance
    return instance


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

    Thin wrapper around :meth:`AppState.load_network_from_bytes`.
    The cache invalidation, ``vl_selector_gen`` bump, per-method
    change-log cleanup and ``script_recorder`` recording happen inside
    the shared lifecycle (see :meth:`AppState.install_network`).

    *parameters* is forwarded to ``load_from_binary_buffer`` so callers can
    pass format-specific import options discovered via
    :func:`~iidm_viewer.io_options.get_format_parameters`.

    *post_processors* is a list of post-processor names to apply after
    parsing, e.g. ``['loadflowResultsCompletion']``.  Available names are
    returned by :func:`~iidm_viewer.io_options.get_import_post_processors`.

    The raw file bytes are stashed in ``_last_file_bytes`` so the UI can
    offer a "Reload with options" flow without requiring a second upload.
    """
    raw_bytes = uploaded_file.getvalue()
    network = app_state().load_network_from_bytes(
        uploaded_file.name,
        raw_bytes,
        parameters=parameters,
        post_processors=post_processors,
    )
    # Streamlit-specific extra: the "Reload with options" flow needs the
    # raw bytes; the shared lifecycle in the AppState doesn't know about it.
    st.session_state["_last_file_bytes"] = raw_bytes
    return network


def create_empty_network(network_id: str = "network"):
    """Create a blank network and install it as the session network.

    Thin wrapper around :meth:`AppState.create_empty_network`. Lets
    users bootstrap a model from scratch without uploading anything —
    they can then build it up via the Data Explorer's "Create a new …"
    forms. Like :func:`load_network`, the resulting object is a
    :class:`NetworkProxy` so every subsequent pypowsybl call runs on
    the worker thread.
    """
    network = app_state().create_empty_network(network_id)
    # Streamlit-specific extras: reset the file-upload widget keys so
    # the next upload starts from a clean state. These are widget keys,
    # not part of the shared AppState contract.
    st.session_state.pop("_last_file", None)
    st.session_state.pop("_last_file_id", None)
    return network


def get_network():
    """Return the currently-open network (or ``None``)."""
    return app_state().network


def run_loadflow(network):
    """Run AC load flow + invalidate Streamlit caches.

    Thin wrapper around :meth:`AppState.run_loadflow`. Reads the LF
    parameters from the sidebar dialog (Streamlit-specific) and
    returns the raw pypowsybl results list (legacy contract — callers
    index ``[0].status.name``).
    """
    from iidm_viewer.lf_parameters import get_lf_parameters

    generic, provider = get_lf_parameters()
    result = app_state().run_loadflow(generic, provider)
    if result is None:
        return None
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
    """Accumulate successfully-applied cell changes into the change log.

    Dual-writes:

    * The legacy per-method ``st.session_state[f"_change_log_{method_name}"]``
      list (no ``component`` key, entries shaped
      ``{element_id, property, before, after}``) — what the existing
      Streamlit Data Explorer render code consumes.
    * The shared :class:`iidm_viewer.change_log.ChangeLog` instance on
      :func:`app_state` — what new code (N-K variant comparison, future
      cross-host panels) reads from. Entries here carry the component
      label (e.g. ``"Generators"``), derived from ``method_name`` via
      :data:`component_registry.LABEL_FOR_METHOD`. Unknown method names
      are skipped on the shared side (the legacy list is still written).

    The collapse + net-diff invariants are the same in both stores —
    both go through :func:`iidm_viewer.change_log.merge_entry`.
    """
    from iidm_viewer.change_log import merge_entry
    from iidm_viewer.component_registry import LABEL_FOR_METHOD

    key = f"_change_log_{method_name}"
    log: list[dict] = list(st.session_state.get(key, []))

    # Shared ChangeLog write — only when we can resolve a component
    # label for the method name. Failing closed (instead of raising)
    # keeps the dual-write strictly additive for any caller using a
    # method name not in the registry yet.
    shared_log = None
    component_label = LABEL_FOR_METHOD.get(method_name)
    if component_label is not None:
        shared_log = app_state().change_log

    for element_id in changes_df.index:
        for col in changes_df.columns:
            new_val = changes_df.at[element_id, col]
            before_val = (
                original_df.at[element_id, col]
                if col in original_df.columns
                else None
            )
            # Legacy list: keep the existing ``component=""`` key
            # (Streamlit looks entries up by method-name index, not
            # by component, so the empty string keeps the on-disk
            # shape unchanged after the ``pop("component")`` below).
            merge_entry(log, "", element_id, col, before_val, new_val)
            # Shared ChangeLog: component label is the on-disk grouping.
            if shared_log is not None:
                shared_log.record(component_label, element_id, col, before_val, new_val)

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
#
# Builder + runner live in the framework-agnostic
# ``iidm_viewer.short_circuit_analysis`` module so the PySide6 and
# NiceGUI prototypes share them. The runner already calls
# ``script_recorder.record_run_short_circuit_analysis`` on the shared
# side, so no Streamlit-side wrapper is needed.
from iidm_viewer.short_circuit_analysis import (  # noqa: F401, E402
    build_bus_faults,
    run_short_circuit_analysis,
)


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
