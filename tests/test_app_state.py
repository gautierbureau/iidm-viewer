"""Tests for the host-agnostic :mod:`iidm_viewer.app_state`.

The Qt and NiceGUI subclasses get their own end-to-end tests in
``test_qt_prototype.py`` and ``test_nicegui_prototype.py``. Here we
exercise the base class directly with the default in-memory storage
backend.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from iidm_viewer.app_state import AppState, _StorageField
from iidm_viewer.cache_backend import (
    DictBackend,
    LF_GEN,
    LINES_ALL,
)
from iidm_viewer.change_log import ChangeLog
from iidm_viewer.loadflow import LoadFlowResult


# ---------------------------------------------------------------------------
# Storage descriptor
# ---------------------------------------------------------------------------


def test_storage_field_class_access_returns_descriptor():
    """``AppState._network`` (class-level) returns the descriptor itself
    so introspection still works."""
    assert isinstance(AppState.__dict__["_network"], _StorageField)


def test_storage_field_reads_via_get():
    state = AppState()
    state._set("network", "X")
    assert state._network == "X"


def test_storage_field_writes_via_set():
    state = AppState()
    state._network = "Y"
    assert state._get("network") == "Y"


def test_storage_field_independent_of_python_attributes():
    """Setting via the descriptor goes to ``_storage``, not the
    instance ``__dict__`` — so subclasses overriding ``_get`` / ``_set``
    (e.g. to read ``st.session_state``) take over cleanly."""
    state = AppState()
    state._network = "X"
    assert "network" in state._storage
    assert "_network" not in state.__dict__


# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


def test_init_defaults():
    state = AppState()
    assert state.network is None
    assert state.selected_vl is None
    assert state.last_report_json is None
    assert state.lf_generic_params == {}
    assert state.lf_provider_params == {}
    assert state.import_format is None
    assert state.import_params == {}
    assert state.import_post_processors == []
    assert isinstance(state.change_log, ChangeLog)
    assert isinstance(state.cache_backend, DictBackend)


def test_persisted_params_setters():
    state = AppState()
    state.lf_generic_params = {"distributed_slack": False}
    state.lf_provider_params = {"slackBusSelectionMode": "FIRST"}
    state.import_format = "XIIDM"
    state.import_params = {"iidm.import.xml.skip-validation": "true"}
    state.import_post_processors = ["odreReporter"]
    assert state.lf_generic_params == {"distributed_slack": False}
    assert state.lf_provider_params == {"slackBusSelectionMode": "FIRST"}
    assert state.import_format == "XIIDM"
    assert state.import_params == {"iidm.import.xml.skip-validation": "true"}
    assert state.import_post_processors == ["odreReporter"]


def test_persisted_params_setter_accepts_none_clears_to_empty():
    state = AppState()
    state.lf_generic_params = {"k": "v"}
    state.lf_generic_params = None
    assert state.lf_generic_params == {}


# ---------------------------------------------------------------------------
# Notification hooks
# ---------------------------------------------------------------------------


def test_on_network_changed_listener_fired_by_default_emit():
    state = AppState()
    seen: list = []
    state.on_network_changed(lambda n: seen.append(n))
    state._emit_network_changed("X")
    assert seen == ["X"]


def test_on_selected_vl_changed_listener_fired():
    state = AppState()
    seen: list = []
    state.on_selected_vl_changed(lambda v: seen.append(v))
    state._emit_selected_vl_changed("VL1")
    assert seen == ["VL1"]


def test_on_loadflow_completed_listener_fired():
    state = AppState()
    seen: list = []
    state.on_loadflow_completed(lambda r: seen.append(r))
    state._emit_loadflow_completed("R")
    assert seen == ["R"]


def test_multiple_listeners_all_fire_in_registration_order():
    state = AppState()
    seen: list = []
    state.on_network_changed(lambda n: seen.append(("a", n)))
    state.on_network_changed(lambda n: seen.append(("b", n)))
    state._emit_network_changed("X")
    assert seen == [("a", "X"), ("b", "X")]


# ---------------------------------------------------------------------------
# set_selected_vl
# ---------------------------------------------------------------------------


def test_set_selected_vl_writes_and_emits():
    state = AppState()
    seen: list = []
    state.on_selected_vl_changed(lambda v: seen.append(v))
    state.set_selected_vl("VL1")
    assert state.selected_vl == "VL1"
    assert seen == ["VL1"]


def test_set_selected_vl_no_op_when_unchanged():
    state = AppState()
    state._set("selected_vl", "VL1")
    seen: list = []
    state.on_selected_vl_changed(lambda v: seen.append(v))
    state.set_selected_vl("VL1")
    assert seen == []


def test_set_selected_vl_empty_string_normalised_to_none():
    state = AppState()
    state._set("selected_vl", "VL1")
    state.set_selected_vl("")
    assert state.selected_vl is None


# ---------------------------------------------------------------------------
# install_network
# ---------------------------------------------------------------------------


def test_install_network_resets_selected_vl_and_report(monkeypatch):
    state = AppState()
    state._set("selected_vl", "VL_STALE")
    state._set("last_report_json", "{}")

    monkeypatch.setattr(
        "iidm_viewer.app_state.network_loader.pick_default_vl",
        lambda net: None,
    )
    state.install_network(MagicMock())
    assert state.selected_vl is None
    assert state.last_report_json is None


def test_install_network_clears_change_log(monkeypatch):
    state = AppState()
    state.change_log.record("Lines", "L1", "p1", before=0.0, after=1.0)
    assert len(state.change_log.entries()) == 1

    monkeypatch.setattr(
        "iidm_viewer.app_state.network_loader.pick_default_vl",
        lambda net: None,
    )
    state.install_network(MagicMock())
    assert state.change_log.entries() == []


def test_install_network_invalidates_cache_backend(monkeypatch):
    state = AppState()
    state.cache_backend.set(LINES_ALL, {"marker": "stale"})
    state.cache_backend.set(LF_GEN, 5)

    monkeypatch.setattr(
        "iidm_viewer.app_state.network_loader.pick_default_vl",
        lambda net: None,
    )
    state.install_network(MagicMock())
    assert state.cache_backend.get(LINES_ALL) is None
    assert state.cache_backend.get(LF_GEN) == 0


def test_install_network_emits_network_then_vl(monkeypatch):
    state = AppState()
    monkeypatch.setattr(
        "iidm_viewer.app_state.network_loader.pick_default_vl",
        lambda net: "VL_DEFAULT",
    )
    seen: list = []
    state.on_network_changed(lambda n: seen.append(("net", n)))
    state.on_selected_vl_changed(lambda v: seen.append(("vl", v)))
    net = MagicMock()
    state.install_network(net)
    assert seen == [("net", net), ("vl", "VL_DEFAULT")]


def test_install_network_with_none_does_not_emit_default_vl(monkeypatch):
    state = AppState()
    state.install_network(None)
    assert state.network is None
    assert state.selected_vl is None


# ---------------------------------------------------------------------------
# notify_network_changed
# ---------------------------------------------------------------------------


def test_notify_network_changed_no_op_when_no_network():
    state = AppState()
    seen: list = []
    state.on_network_changed(lambda n: seen.append(n))
    state.notify_network_changed()
    assert seen == []


def test_notify_network_changed_emits_for_existing_network(monkeypatch):
    state = AppState()
    state._set("network", MagicMock())
    state._set("selected_vl", "VL_STALE")
    state._set("last_report_json", "{}")
    state.change_log.record("Lines", "L1", "p1", before=0.0, after=1.0)

    monkeypatch.setattr(
        "iidm_viewer.app_state.network_loader.pick_default_vl",
        lambda net: "VL_NEW",
    )
    seen: list = []
    state.on_network_changed(lambda n: seen.append(("net", n)))
    state.on_selected_vl_changed(lambda v: seen.append(("vl", v)))

    state.notify_network_changed()
    assert state.last_report_json is None
    assert state.change_log.entries() == []
    assert seen[0][0] == "net"
    assert seen[-1] == ("vl", "VL_NEW")


# ---------------------------------------------------------------------------
# run_loadflow / run_loadflow_no_notify
# ---------------------------------------------------------------------------


def test_run_loadflow_no_network_returns_none():
    state = AppState()
    assert state.run_loadflow() is None


def test_run_loadflow_calls_run_ac_with_cached_params(monkeypatch):
    state = AppState()
    state._set("network", MagicMock())
    state.lf_generic_params = {"distributed_slack": False}
    state.lf_provider_params = {"slackBusSelectionMode": "FIRST"}

    captured: dict = {}

    def fake_run_ac(net, g, p):
        captured["generic"] = g
        captured["provider"] = p
        return LoadFlowResult([], '{"report": "ok"}')

    monkeypatch.setattr("iidm_viewer.app_state.run_ac", fake_run_ac)
    monkeypatch.setattr(
        "iidm_viewer.app_state.script_recorder.record_run_loadflow",
        lambda *a, **kw: None,
    )

    result = state.run_loadflow()
    assert result is not None
    assert captured["generic"] == {"distributed_slack": False}
    assert captured["provider"] == {"slackBusSelectionMode": "FIRST"}
    assert state.last_report_json == '{"report": "ok"}'


def test_run_loadflow_bumps_lf_gen(monkeypatch):
    state = AppState()
    state._set("network", MagicMock())

    monkeypatch.setattr(
        "iidm_viewer.app_state.run_ac",
        lambda *a, **kw: LoadFlowResult([], "{}"),
    )
    monkeypatch.setattr(
        "iidm_viewer.app_state.script_recorder.record_run_loadflow",
        lambda *a, **kw: None,
    )
    assert state.cache_backend.get(LF_GEN, 0) == 0
    state.run_loadflow()
    assert state.cache_backend.get(LF_GEN) == 1


def test_run_loadflow_emits_loadflow_completed(monkeypatch):
    state = AppState()
    state._set("network", MagicMock())

    fake_result = LoadFlowResult([], "{}")
    monkeypatch.setattr(
        "iidm_viewer.app_state.run_ac", lambda *a, **kw: fake_result,
    )
    monkeypatch.setattr(
        "iidm_viewer.app_state.script_recorder.record_run_loadflow",
        lambda *a, **kw: None,
    )

    seen: list = []
    state.on_loadflow_completed(lambda r: seen.append(r))
    state.run_loadflow()
    assert seen == [fake_result]


def test_run_loadflow_no_notify_does_not_fire_listeners(monkeypatch):
    state = AppState()
    state._set("network", MagicMock())

    monkeypatch.setattr(
        "iidm_viewer.app_state.run_ac",
        lambda *a, **kw: LoadFlowResult([], "{}"),
    )
    monkeypatch.setattr(
        "iidm_viewer.app_state.script_recorder.record_run_loadflow",
        lambda *a, **kw: None,
    )

    seen: list = []
    state.on_loadflow_completed(lambda r: seen.append(r))
    state.run_loadflow_no_notify()
    assert seen == []


def test_run_loadflow_explicit_params_override_cached(monkeypatch):
    state = AppState()
    state._set("network", MagicMock())
    state.lf_generic_params = {"distributed_slack": False}

    captured: dict = {}

    def fake_run_ac(net, g, p):
        captured["generic"] = g
        return LoadFlowResult([], "{}")

    monkeypatch.setattr("iidm_viewer.app_state.run_ac", fake_run_ac)
    monkeypatch.setattr(
        "iidm_viewer.app_state.script_recorder.record_run_loadflow",
        lambda *a, **kw: None,
    )

    state.run_loadflow(generic_params={"distributed_slack": True})
    assert captured["generic"] == {"distributed_slack": True}


# ---------------------------------------------------------------------------
# _run_ac hook override
# ---------------------------------------------------------------------------


def test_subclass_run_ac_override_is_used():
    class Sub(AppState):
        def _run_ac(self, network, g, p):
            return LoadFlowResult([], '{"from": "sub"}')

    state = Sub()
    state._set("network", MagicMock())
    # No need to monkeypatch run_ac — the override takes precedence.
    state.run_loadflow_no_notify()
    assert state.last_report_json == '{"from": "sub"}'
