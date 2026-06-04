"""Host-agnostic application state.

Streamlit, PySide6 (``qt/``) and NiceGUI (``web/``) hosts share the same
notion of "the open network, the selected voltage level, the last load
flow report, plus a change log and a cache backend"; they differ only in
**storage** (``st.session_state`` vs in-memory) and **notification**
(Streamlit's implicit reruns vs PySide signals vs NiceGUI callbacks).

:class:`AppState` is the shared base: it carries the persistent fields,
the cache backend, the change log, and the shared lifecycle methods
(:meth:`install_network`, :meth:`set_selected_vl`,
:meth:`notify_network_changed`, :meth:`run_loadflow`).

Subclasses plug in:

* ``_get(key)`` / ``_set(key, value)`` — storage. The default uses an
  in-memory dict; the eventual Streamlit subclass overrides them to read
  / write ``st.session_state``.
* ``_emit_network_changed`` / ``_emit_selected_vl_changed`` /
  ``_emit_loadflow_completed`` — notification. The default fires
  registered listener callbacks (``on_*_changed`` / ``on_loadflow_completed``);
  the PySide6 subclass overrides them to call ``Signal.emit`` so existing
  Qt signal-connect code keeps working.

The GraalVM thread-affinity rule from AGENTS.md §1 is preserved — every
pypowsybl call goes through :mod:`iidm_viewer.network_loader` or
:mod:`iidm_viewer.loadflow`, both of which route through
``iidm_viewer.powsybl_worker.run``.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from iidm_viewer import network_loader, script_recorder
from iidm_viewer.cache_backend import (
    CacheBackend,
    DictBackend,
    invalidate_load_flow,
    invalidate_network_replace,
)
from iidm_viewer.change_log import ChangeLog
from iidm_viewer.loadflow import LoadFlowResult, run_ac
from iidm_viewer.powsybl_worker import NetworkProxy


# Listener type aliases — host-agnostic.
NetworkListener = Callable[[Optional[NetworkProxy]], None]
VlListener = Callable[[Optional[str]], None]
LoadFlowListener = Callable[[LoadFlowResult], None]


class _StorageField:
    """Descriptor mapping an instance attribute to ``_get`` / ``_set``.

    Lets existing test fixtures keep poking ``state._network = ...``
    while the underlying storage is whatever ``_get`` / ``_set`` plug
    in (an in-memory dict by default; ``st.session_state`` for the
    eventual Streamlit subclass).
    """

    __slots__ = ("key",)

    def __init__(self, key: str) -> None:
        self.key = key

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance._get(self.key)

    def __set__(self, instance, value):
        instance._set(self.key, value)


class AppState:
    """Single source of truth for the open network + selected VL.

    Hosts inherit from this class and override the ``_emit_*`` hooks (and,
    if their storage isn't an in-memory dict, ``_get`` / ``_set``).
    """

    # Storage-backed instance attributes — exposed as plain ``_network``
    # / ``_selected_vl`` / ``_last_report_json`` for backward compat with
    # tests that poke them directly.
    _network = _StorageField("network")
    _selected_vl = _StorageField("selected_vl")
    _last_report_json = _StorageField("last_report_json")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(self) -> None:
        # Default storage: an in-memory dict. Subclasses can override
        # ``_get`` / ``_set`` to plug in a host-specific backing store
        # (e.g. ``st.session_state``).
        self._storage: dict[str, Any] = {}

        # Listener registries — fed by ``on_*_changed`` and consumed by
        # the default ``_emit_*`` hooks.
        self._network_listeners: list[NetworkListener] = []
        self._vl_listeners: list[VlListener] = []
        self._loadflow_listeners: list[LoadFlowListener] = []

        # Always-present collaborators. They hold mutable state across
        # the process's lifetime so they're not stored via ``_set``.
        self.change_log = ChangeLog()
        self.cache_backend: CacheBackend = DictBackend()

        # Persisted user overrides — accessed via the properties below
        # so subclasses don't need to know the storage key.
        self._set("lf_generic_params", {})
        self._set("lf_provider_params", {})
        self._set("import_format", None)
        self._set("import_params", {})
        self._set("import_post_processors", [])

    # ------------------------------------------------------------------
    # Storage hooks (default = in-memory dict)
    # ------------------------------------------------------------------
    def _get(self, key: str, default: Any = None) -> Any:
        return self._storage.get(key, default)

    def _set(self, key: str, value: Any) -> None:
        self._storage[key] = value

    # ------------------------------------------------------------------
    # Notification hooks (default = call registered listener callbacks)
    # ------------------------------------------------------------------
    def _emit_network_changed(self, network: Optional[NetworkProxy]) -> None:
        for cb in list(self._network_listeners):
            cb(network)

    def _emit_selected_vl_changed(self, vl_id: Optional[str]) -> None:
        for cb in list(self._vl_listeners):
            cb(vl_id)

    def _emit_loadflow_completed(self, result: LoadFlowResult) -> None:
        for cb in list(self._loadflow_listeners):
            cb(result)

    # ------------------------------------------------------------------
    # Listener registration
    # ------------------------------------------------------------------
    def on_network_changed(self, listener: NetworkListener) -> None:
        self._network_listeners.append(listener)

    def on_selected_vl_changed(self, listener: VlListener) -> None:
        self._vl_listeners.append(listener)

    def on_loadflow_completed(self, listener: LoadFlowListener) -> None:
        self._loadflow_listeners.append(listener)

    # ------------------------------------------------------------------
    # Public read-only properties
    # ------------------------------------------------------------------
    @property
    def network(self) -> Optional[NetworkProxy]:
        return self._get("network")

    @property
    def selected_vl(self) -> Optional[str]:
        return self._get("selected_vl")

    @property
    def last_report_json(self) -> Optional[str]:
        """JSON-encoded report from the most recent :meth:`run_loadflow`,
        or ``None`` when no LF has been run since the current network was
        installed."""
        return self._get("last_report_json")

    # ------------------------------------------------------------------
    # Persisted user overrides (get/set pairs)
    # ------------------------------------------------------------------
    @property
    def lf_generic_params(self) -> dict:
        return self._get("lf_generic_params") or {}

    @lf_generic_params.setter
    def lf_generic_params(self, value: Optional[dict]) -> None:
        self._set("lf_generic_params", value or {})

    @property
    def lf_provider_params(self) -> dict:
        return self._get("lf_provider_params") or {}

    @lf_provider_params.setter
    def lf_provider_params(self, value: Optional[dict]) -> None:
        self._set("lf_provider_params", value or {})

    @property
    def import_format(self) -> Optional[str]:
        return self._get("import_format")

    @import_format.setter
    def import_format(self, value: Optional[str]) -> None:
        self._set("import_format", value)

    @property
    def import_params(self) -> dict:
        return self._get("import_params") or {}

    @import_params.setter
    def import_params(self, value: Optional[dict]) -> None:
        self._set("import_params", value or {})

    @property
    def import_post_processors(self) -> list:
        return self._get("import_post_processors") or []

    @import_post_processors.setter
    def import_post_processors(self, value: Optional[list]) -> None:
        self._set("import_post_processors", value or [])

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def load_network_from_path(
        self,
        path: str,
        *,
        parameters: Optional[dict] = None,
        post_processors: Optional[list] = None,
    ) -> NetworkProxy:
        """Load a network from disk and install it.

        ``parameters`` / ``post_processors`` default to the AppState's
        persisted overrides (set by a "Load Options" dialog). Both the
        load and the default-VL pick run on the pypowsybl worker thread
        via :mod:`iidm_viewer.network_loader`.
        """
        params = parameters if parameters is not None else (self.import_params or None)
        pp = post_processors if post_processors is not None else (self.import_post_processors or None)
        network = network_loader.load_from_path(
            path, parameters=params, post_processors=pp,
        )
        self.install_network(network)
        script_recorder.record_load_network(
            os.path.basename(path) or path, params, pp,
        )
        return network

    def load_network_from_bytes(
        self,
        file_name: str,
        raw_bytes: bytes,
        *,
        parameters: Optional[dict] = None,
        post_processors: Optional[list] = None,
    ) -> NetworkProxy:
        """Load a network from an in-memory buffer and install it.

        Convenience for the Streamlit subclass's file-upload flow. The
        worker round-trip lives in
        :func:`network_loader.load_from_bytes`.
        """
        params = parameters if parameters is not None else (self.import_params or None)
        pp = post_processors if post_processors is not None else (self.import_post_processors or None)
        network = network_loader.load_from_bytes(
            file_name, raw_bytes, parameters=params, post_processors=pp,
        )
        self.install_network(network)
        script_recorder.record_load_network(file_name, params, pp)
        return network

    def create_empty_network(self, network_id: str = "network") -> NetworkProxy:
        """Build a blank pypowsybl network and install it."""
        network = network_loader.create_empty(network_id)
        self.install_network(network)
        script_recorder.record_create_empty(network_id)
        return network

    def install_network(self, network: Optional[NetworkProxy]) -> None:
        """Make ``network`` the active one + broadcast listeners.

        Shared by every load entry point so the state-reset + cache
        invalidation + listener-fire sequence stays in lockstep.
        """
        default_vl = network_loader.pick_default_vl(network) if network else None
        # Pop every cache slot before listeners run so any cache-backed
        # consumer rebuilds against the new network.
        invalidate_network_replace(self.cache_backend)
        self._set("network", network)
        self._set("selected_vl", None)
        self._set("last_report_json", None)
        self.change_log.clear()
        self._emit_network_changed(network)
        if default_vl:
            self.set_selected_vl(default_vl)

    def set_selected_vl(self, vl_id: Optional[str]) -> None:
        new = vl_id or None
        if new == self._get("selected_vl"):
            return
        self._set("selected_vl", new)
        self._emit_selected_vl_changed(new)

    def notify_network_changed(self) -> None:
        """Re-broadcast the *same* network as if it had been freshly loaded.

        Used after irreversible in-place mutations (e.g. network
        reduction) so the diagram tabs and the data explorer refresh
        against the new topology without going through a full reload.
        """
        network = self._get("network")
        if network is None:
            return
        self._set("selected_vl", None)
        self._set("last_report_json", None)
        self.change_log.clear()
        invalidate_network_replace(self.cache_backend)
        default_vl = network_loader.pick_default_vl(network)
        self._emit_network_changed(network)
        if default_vl:
            self.set_selected_vl(default_vl)

    def run_loadflow(
        self,
        generic_params: Optional[dict] = None,
        provider_params: Optional[dict] = None,
    ) -> Optional[LoadFlowResult]:
        """Run AC load flow on the open network and broadcast the result.

        Returns ``None`` when no network is loaded.
        """
        result = self.run_loadflow_no_notify(generic_params, provider_params)
        if result is not None:
            self._emit_loadflow_completed(result)
        return result

    def run_loadflow_no_notify(
        self,
        generic_params: Optional[dict] = None,
        provider_params: Optional[dict] = None,
    ) -> Optional[LoadFlowResult]:
        """Run AC load flow without broadcasting to listeners.

        Splitting the result production from the notification lets
        NiceGUI fire listeners on the event-loop thread after the LF
        runs on a worker thread via ``asyncio.to_thread``.
        """
        network = self._get("network")
        if network is None:
            return None
        if generic_params is None:
            generic_params = self._get("lf_generic_params") or None
        if provider_params is None:
            provider_params = self._get("lf_provider_params") or None
        result = self._run_ac(network, generic_params, provider_params)
        self._set("last_report_json", getattr(result, "report_json", None))
        invalidate_load_flow(self.cache_backend)
        script_recorder.record_run_loadflow(generic_params, provider_params)
        return result

    def _run_ac(
        self,
        network: NetworkProxy,
        generic_params: Optional[dict],
        provider_params: Optional[dict],
    ) -> LoadFlowResult:
        """Hook around :func:`iidm_viewer.loadflow.run_ac`.

        Subclasses override this when they need a host-scoped name (so
        ``monkeypatch.setattr("iidm_viewer.<host>.state.run_ac", …)``
        intercepts the LF call from tests). The default just calls
        the import in this module.
        """
        return run_ac(network, generic_params, provider_params)
