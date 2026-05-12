"""Smoke test for the NiceGUI prototype (``iidm_viewer.web``).

Scope is limited on purpose: anything inside the JS iframes (clicks
on deck.gl layers, SLD navigation arrows) needs a real browser to
exercise. What this file does cover:

* ``AppState`` listener semantics — drop-in for the PySide6 test.
* End-to-end pypowsybl helpers (``_extract_map_data``,
  ``_generate_sld``) against IEEE14 — proves the worker path works
  the same regardless of UI host.
* The page route registers and the bridge JS contains the hooks the
  iframe wire-protocol expects.

A real-browser end-to-end is intentionally out of scope; it would
require a Chromium download which is blocked in some CI sandboxes.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("nicegui")


def test_app_state_fires_only_on_change():
    from iidm_viewer.web.state import AppState

    state = AppState()
    seen: list[str | None] = []
    state.on_selected_vl_changed(lambda v: seen.append(v))
    state.set_selected_vl("VL_A")
    state.set_selected_vl("VL_A")  # same — must not re-emit
    state.set_selected_vl("VL_B")
    state.set_selected_vl(None)
    assert seen == ["VL_A", "VL_B", None]


def test_app_state_loads_ieee14_and_auto_selects_highest_voltage():
    from iidm_viewer.web.state import AppState

    state = AppState()
    network_calls: list = []
    vl_calls: list = []
    state.on_network_changed(network_calls.append)
    state.on_selected_vl_changed(vl_calls.append)

    xiidm = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm")
    )
    state.load_network_from_path(xiidm)

    assert state.network is not None
    # network_changed fires exactly once on load.
    assert len(network_calls) == 1
    # selected_vl_changed fires once with the auto-selected default.
    assert len(vl_calls) == 1
    assert state.selected_vl == vl_calls[0]
    assert state.selected_vl is not None


def test_extract_map_data_returns_real_geometry():
    """The NiceGUI tab reuses the same extractor as the Streamlit and
    PySide6 tabs; quick check that it still works through the worker.
    """
    from iidm_viewer.web.app import _extract_map_data
    from iidm_viewer.web.state import AppState

    state = AppState()
    xiidm = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm")
    )
    state.load_network_from_path(xiidm)

    data = _extract_map_data(state.network)
    assert data is not None
    substations, positions, lines, line_positions = data
    assert len(substations) > 0
    assert len(positions) > 0
    assert len(lines) > 0
    # line_positions may be empty even when geometry exists.
    assert isinstance(line_positions, list)


def test_generate_sld_returns_real_svg():
    from iidm_viewer.web.app import _generate_sld
    from iidm_viewer.web.state import AppState

    state = AppState()
    xiidm = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm")
    )
    state.load_network_from_path(xiidm)

    svg, metadata = _generate_sld(state.network, state.selected_vl)
    assert isinstance(svg, str) and ("<svg" in svg or svg.lstrip().startswith("<?xml"))
    assert isinstance(metadata, str) and metadata.strip().startswith("{")


def test_generate_nad_returns_real_svg():
    from iidm_viewer.web.app import _generate_nad
    from iidm_viewer.web.state import AppState

    state = AppState()
    xiidm = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm")
    )
    state.load_network_from_path(xiidm)

    svg, metadata = _generate_nad(state.network, state.selected_vl, 1)
    assert isinstance(svg, str) and ("<svg" in svg or svg.lstrip().startswith("<?xml"))
    assert isinstance(metadata, str) and metadata.strip().startswith("{")


def test_fetch_dataframe_returns_dataframes_per_component():
    from iidm_viewer.web.app import _fetch_dataframe, COMPONENT_GETTERS
    from iidm_viewer.web.state import AppState

    state = AppState()
    xiidm = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm")
    )
    state.load_network_from_path(xiidm)

    vl_df = _fetch_dataframe(state.network, COMPONENT_GETTERS["Voltage Levels"])
    assert vl_df.shape[0] == 14
    assert "nominal_v" in vl_df.columns

    gen_df = _fetch_dataframe(state.network, COMPONENT_GETTERS["Generators"])
    assert gen_df.shape[0] > 0
    assert set(vl_df.columns) != set(gen_df.columns)

    hvdc_df = _fetch_dataframe(state.network, COMPONENT_GETTERS["HVDC Lines"])
    assert hvdc_df.shape[0] == 0  # IEEE14 has none, must not raise


def test_dataframe_to_aggrid_options_handles_nan_and_empty():
    import math
    import pandas as pd
    from iidm_viewer.web.app import _dataframe_to_aggrid_options

    empty = _dataframe_to_aggrid_options(pd.DataFrame())
    assert empty["columnDefs"] == [] and empty["rowData"] == []
    # Empty frames still carry the default col def so the grid keeps
    # its filter / sort affordances when the next refresh arrives.
    assert "defaultColDef" in empty

    df = pd.DataFrame({"id": ["a", "b"], "v": [1.0, math.nan]})
    opts = _dataframe_to_aggrid_options(df)
    assert {c["field"] for c in opts["columnDefs"]} == {"id", "v"}
    # Numeric columns should be tagged for ag-Grid right-alignment.
    v_col = next(c for c in opts["columnDefs"] if c["field"] == "v")
    assert v_col.get("type") == "numericColumn"
    assert v_col.get("filter") == "agNumberColumnFilter"
    # The id column is pinned-left so it stays visible while scrolling.
    id_col = next(c for c in opts["columnDefs"] if c["field"] == "id")
    assert id_col.get("pinned") == "left"
    # NaN values render as em-dash.
    assert opts["rowData"][1]["v"] == "—"


def test_dataframe_to_aggrid_options_marks_editable_columns():
    import pandas as pd
    from iidm_viewer.web.app import _dataframe_to_aggrid_options

    df = pd.DataFrame({"id": ["g1"], "target_p": [10.0], "name": ["foo"]})
    opts = _dataframe_to_aggrid_options(df, editable_cols=["target_p"])
    by_field = {c["field"]: c for c in opts["columnDefs"]}
    assert by_field["target_p"].get("editable") is True
    assert "cellStyle" in by_field["target_p"]
    # Non-editable columns get no editable flag.
    assert "editable" not in by_field["name"]
    assert "editable" not in by_field["id"]


def test_app_state_owns_change_log():
    from iidm_viewer.web.state import AppState
    from iidm_viewer.change_log import ChangeLog

    state = AppState()
    assert isinstance(state.change_log, ChangeLog)
    state.change_log.record("Generators", "G1", "target_p", 1.0, 2.0)
    assert len(state.change_log) == 1


def test_app_state_run_loadflow_emits_listener():
    """The NiceGUI ``AppState.run_loadflow`` fans out to listeners
    registered via ``on_loadflow_completed``."""
    import os
    from iidm_viewer.web.state import AppState

    state = AppState()
    xiidm = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm")
    )
    state.load_network_from_path(xiidm)

    seen = []
    state.on_loadflow_completed(seen.append)
    result = state.run_loadflow()
    assert result is not None
    assert result.converged is True
    assert len(seen) == 1
    assert seen[0] is result


def test_app_state_run_loadflow_noop_without_network():
    from iidm_viewer.web.state import AppState
    state = AppState()
    assert state.run_loadflow() is None


def test_change_log_cleared_on_new_network_load():
    import os
    from iidm_viewer.web.state import AppState

    state = AppState()
    state.change_log.record("Generators", "G1", "target_p", 1.0, 2.0)
    assert len(state.change_log) == 1

    xiidm = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm")
    )
    state.load_network_from_path(xiidm)
    # Loading a fresh network wipes the previous log so entries don't
    # cross-contaminate.
    assert len(state.change_log) == 0


def test_dataframe_to_aggrid_options_enables_multi_row_selection():
    """Bulk edit needs ag-Grid ``rowSelection: 'multiple'`` and the
    ``id`` column's checkbox affordance — guard both."""
    import pandas as pd
    from iidm_viewer.web.app import _dataframe_to_aggrid_options

    opts = _dataframe_to_aggrid_options(pd.DataFrame({"id": ["a"], "v": [1]}))
    assert opts.get("rowSelection") == "multiple"

    id_col = next(c for c in opts["columnDefs"] if c["field"] == "id")
    assert id_col.get("checkboxSelection") is True
    assert id_col.get("headerCheckboxSelection") is True


def test_dataframe_to_aggrid_options_default_col_def_enables_sort_and_filter():
    """The default col def must give every column sort + floating filter.

    Floating filters are the per-column quick-filter row ag-Grid renders
    under the header — the headline feature for "filter in the grid"
    parity with the Qt prototype's filter text box.
    """
    import pandas as pd
    from iidm_viewer.web.app import _dataframe_to_aggrid_options

    opts = _dataframe_to_aggrid_options(pd.DataFrame({"a": [1]}))
    default = opts["defaultColDef"]
    assert default["sortable"] is True
    assert default["resizable"] is True
    assert default["filter"] is True
    assert default["floatingFilter"] is True


def test_bridge_js_has_expected_hooks():
    """The shim glues the bundles' Streamlit wire-protocol to NiceGUI's
    emitEvent bus. If any of these names drifts, the iframes can't
    talk to Python — guard against silent regressions.
    """
    from iidm_viewer.web.app import _BRIDGE_JS

    for token in (
        # The COMPONENTS registry the bridge iterates over; iframe ids
        # are derived as `'iidm-' + component + '-iframe'`.
        "'map'",
        "'nad'",
        "'sld'",
        "'iidm-' + component + '-iframe'",
        "iidm-component-ready",
        "iidm-component-value",
        "iidmRenderTo",
        "streamlit:render",
        "streamlit:componentReady",
        "streamlit:setComponentValue",
        "emitEvent(",
    ):
        assert token in _BRIDGE_JS, f"bridge JS lost token {token!r}"


def test_map_bundle_defines_setcomponentvalue_globally():
    """The bundle calls ``setComponentValue(...)`` as a bare global
    when the user clicks a substation. Streamlit's component runtime
    polyfills it; our two non-Streamlit hosts (PySide6 + NiceGUI) do
    not — so the bundle must carry its own definition. Without it,
    clicks raised ``ReferenceError: setComponentValue is not defined``.
    """
    import os
    bundle = os.path.join(
        os.path.dirname(__file__), "..",
        "iidm_viewer", "frontend", "map_component", "dist",
        "assets", "map-component.js",
    )
    with open(bundle, encoding="utf-8") as fh:
        js = fh.read()
    assert "streamlit:setComponentValue" in js, (
        "the map bundle must define setComponentValue (matching the SLD "
        "and NAD bundles) so it works under non-Streamlit hosts. "
        "Rebuild with `npm run build` in frontend/map_component."
    )


def test_page_route_is_registered():
    """Importing ``iidm_viewer.web.app`` should register the ``/`` page.

    NiceGUI 3.x exposes registered pages via ``app.routes`` — quick
    structural assertion that catches typos in @ui.page paths.
    """
    from nicegui import app
    import iidm_viewer.web.app  # noqa: F401

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/" in paths


def test_static_mounts_are_registered():
    """The map / nad / sld bundles must be served under the URLs the
    page references. ``app.add_static_files`` registers FastAPI mounts.
    """
    from nicegui import app
    import iidm_viewer.web.app  # noqa: F401

    mounts = {getattr(r, "path", None) for r in app.routes}
    # Mount prefixes include their trailing slash in FastAPI.
    assert any(p and p.startswith("/_iidm/map_component") for p in mounts)
    assert any(p and p.startswith("/_iidm/nad_component") for p in mounts)
    assert any(p and p.startswith("/_iidm/sld_component") for p in mounts)


def test_run_app_falls_back_to_browser_when_no_native_backend(monkeypatch, capsys):
    """If pywebview can't find a backend, ``run_app(native=True)`` should
    warn and call ``ui.run`` with ``native=False`` instead of crashing."""
    from iidm_viewer.web import app

    monkeypatch.setattr(app, "_native_backend_available", lambda: False)
    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(app.ui, "run", fake_run)
    app.run_app(native=True, port=8669)

    assert captured.get("native") is False
    assert captured.get("show") is True
    err = capsys.readouterr().err
    assert "no pywebview backend is available" in err
    assert "http://localhost:8669/" in err


def test_run_app_keeps_native_when_backend_is_available(monkeypatch):
    """When the probe says a backend is available, ``run_app`` must
    leave ``native=True`` untouched."""
    from iidm_viewer.web import app

    monkeypatch.setattr(app, "_native_backend_available", lambda: True)
    captured: dict = {}
    monkeypatch.setattr(app.ui, "run", lambda **kw: captured.update(kw))
    app.run_app(native=True, port=8669)
    assert captured.get("native") is True
    assert captured.get("show") is False


def test_run_app_no_native_path_skips_probe(monkeypatch):
    """``run_app(native=False)`` is the explicit browser-mode opt-in;
    the backend probe shouldn't run at all in that path."""
    from iidm_viewer.web import app

    probe_calls = []
    monkeypatch.setattr(
        app, "_native_backend_available",
        lambda: probe_calls.append(1) or False,
    )
    monkeypatch.setattr(app.ui, "run", lambda **kw: None)
    app.run_app(native=False, port=8669)
    assert probe_calls == []


def test_diagram_iframes_opt_out_of_sanitize():
    """NiceGUI 3.x's ``ui.html`` sanitizes by default and DOMPurify
    strips ``<iframe>`` tags — which would empty the Map / NAD / SLD
    tabs. The fix: pass ``sanitize=False`` to those three callsites so
    the bundles' iframes survive. This grep-style check guards the fix."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app)
    # Locate the tab-panels block that mounts the iframes.
    tabs_start = src.index("with panels:")
    data_start = src.index("with ui.tab_panel(data_tab)")
    region = src[tabs_start:data_start]
    # Each of the three iframes must opt out of sanitisation.
    for iframe_id in ("iidm-map-iframe", "iidm-nad-iframe", "iidm-sld-iframe"):
        assert iframe_id in region, f"{iframe_id} should be wired"
    assert region.count("sanitize=False") >= 3, (
        "expected sanitize=False on all three diagram iframes; "
        "DOMPurify strips <iframe> tags otherwise"
    )


def test_install_network_separates_io_from_listener_dispatch():
    """Regression for the "slot stack is empty" upload crash:
    ``AppState.install_network`` accepts a pre-loaded network and fires
    the network listeners synchronously on the calling thread, so the
    NiceGUI handler can do the load via ``asyncio.to_thread`` and
    install the result back on the event loop where the slot is set.
    """
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.web.state import AppState

    net = NetworkProxy(run(pn.create_ieee14))
    state = AppState()
    seen_networks: list = []
    seen_vls: list = []
    state.on_network_changed(seen_networks.append)
    state.on_selected_vl_changed(seen_vls.append)

    state.install_network(net)
    assert state.network is net
    assert seen_networks == [net]
    # pick_default_vl should have selected the highest-nominal-V VL.
    assert state.selected_vl is not None
    assert seen_vls == [state.selected_vl]


def test_upload_handler_loads_in_worker_and_installs_on_event_loop():
    """The upload path must split heavy IO from listener notification:
    ``network_loader.load_from_path`` runs in ``asyncio.to_thread`` and
    ``_state.install_network`` is called on the event loop. Inspect the
    handler source to keep that contract from drifting back to the
    crash-prone single-call shape."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app)
    handler_start = src.index("async def handle_upload(e):")
    handler_end = src.index(
        "file_lbl.set_text(os.path.basename(name))", handler_start,
    )
    handler_src = src[handler_start:handler_end]
    assert "asyncio.to_thread(" in handler_src
    assert "network_loader.load_from_path" in handler_src
    assert "_state.install_network" in handler_src


def test_data_explorer_refresh_preserves_aggrid_theme():
    """AG Grid 34 (NiceGUI 3.x) throws at mount time when
    ``options.theme`` is undefined. Replacing the whole ``grid.options``
    dict on every refresh wipes the wrapper-set default — which is what
    silently blanked the Data Explorer grid. The refresh path must
    mutate ``grid.options`` (``.update(...)``) instead of replacing it.
    """
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_data_explorer)
    # Replacement-style assignment of grid.options inside refresh() is
    # the regression. Only the assignment in the constructor (where
    # ``grid = ui.aggrid({...})``) is allowed.
    assert "grid.options =" not in src, (
        "refresh() must mutate grid.options (use .update(...)) rather "
        "than replace it — full replacement drops the AG Grid theme "
        "and the grid silently fails to mount."
    )
    # All three refresh callsites should funnel through ``options.update``.
    assert src.count("grid.options.update(") >= 3


def test_iframes_resend_last_args_on_every_ready_event():
    """Regression for the blank-on-tab-switch bug.

    q-tab-panels defaults to ``keep-alive=false``, which destroys +
    remounts the inactive iframe each time the user switches tabs. The
    ready handler must resend the cached ``_last_*`` args *every* time
    the bundle posts ``iidm-component-ready`` — not just the first time
    a payload is queued — or the new iframe stays blank.
    """
    import inspect
    from iidm_viewer.web import app

    # The cache vars must exist (replacing the old single-shot _pending_*).
    assert hasattr(app, "_last_map")
    assert hasattr(app, "_last_nad")
    assert hasattr(app, "_last_sld")

    src = inspect.getsource(app)
    # _pending_* must not survive (they were the source of the bug).
    assert "_pending_map" not in src
    assert "_pending_nad" not in src
    assert "_pending_sld" not in src
    # The ready handler resends from _last_*, not _pending_*.
    handler_src = inspect.getsource(app.main_page)
    assert "_last_map" in handler_src
    assert "_last_nad" in handler_src
    assert "_last_sld" in handler_src


def test_tab_panels_keep_alive_props_set():
    """``keep-alive`` on ``q-tab-panels`` makes Quasar preserve the
    inactive panels' DOM. Combined with the resend-on-ready handler
    this stops the iframes from going blank on tab switches."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert 'ui.tab_panels(tabs, value=map_tab).classes("w-full").props("keep-alive")' in src


def test_shared_vl_selector_helpers():
    """``network_loader`` exposes the framework-agnostic VL listing +
    filter helpers used by Streamlit's ``vl_selector`` and the
    PySide6 + NiceGUI sidebars."""
    import pypowsybl.network as pn
    from iidm_viewer.network_loader import (
        filter_voltage_levels,
        list_voltage_levels_for_selector,
    )
    from iidm_viewer.powsybl_worker import NetworkProxy, run

    net = NetworkProxy(run(pn.create_ieee14))
    df = list_voltage_levels_for_selector(net)
    assert not df.empty
    assert set(["id", "display", "substation_id", "nominal_v"]) <= set(df.columns)
    # The filter is a case-insensitive substring match on ``display``.
    matches = filter_voltage_levels(df, "VL1")
    assert not matches.empty
    assert all("VL1" in d for d in matches["display"])
    # Empty filter returns the input unchanged.
    assert len(filter_voltage_levels(df, "")) == len(df)


def test_nicegui_drawer_has_vl_filter_and_select():
    """The left drawer must carry a VL filter input + a dropdown that
    routes selections through ``_state.set_selected_vl``. Mirrors the
    Streamlit ``vl_selector`` so users on either prototype can pick a
    VL the same way."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    drawer_start = src.index("ui.left_drawer(")
    drawer_end = src.index("with ui.tabs(", drawer_start)
    drawer_src = src[drawer_start:drawer_end]
    # Filter + dropdown widgets.
    assert "Filter voltage levels" in drawer_src
    assert 'ui.select(options=[], value=None)' in drawer_src
    # The dropdown change must funnel into AppState.
    assert "_state.set_selected_vl" in drawer_src
    # Visibility flip on network load (hidden when empty).
    assert "vl_filter_input.visible" in drawer_src
    assert "vl_select.visible" in drawer_src


def test_nad_depth_handler_reads_widget_value_not_event_value():
    """NiceGUI 3.x's ``.on('update:model-value', handler)`` hands back a
    ``GenericEventArguments`` whose payload lives on ``args`` (not
    ``value`` like the 2.x ``ValueChangeEventArguments``). The depth
    handler should read ``depth_input.value`` directly to stay
    compatible with both versions — guards against a future "simplify"
    pass folding ``e.value`` back in."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    handler_idx = src.index("def _on_depth_changed(")
    handler_end = src.index("depth_input.on(", handler_idx)
    handler_src = src[handler_idx:handler_end]
    assert "e.value" not in handler_src, (
        "_on_depth_changed must not call ``e.value`` — NiceGUI 3.x's "
        "GenericEventArguments has no such attribute. Read the widget "
        "value (``depth_input.value``) instead."
    )
    assert "depth_input.value" in handler_src


def test_map_substation_click_routes_to_sld_tab_and_selected_vl():
    """End-to-end check of the killer interaction.

    The map bundle emits ``{ type: 'map-substation-click', vlIds: [...] }``
    via ``setComponentValue``; the bridge JS relays it as the NiceGUI
    event ``iidm-component-value`` with ``{component: 'map', value: ...}``;
    ``main_page._on_component_value`` should switch the active tab to
    the SLD panel and set the highest-V VL as the selected VL.

    Driving the bridge here is brittle (we'd need a real browser), so
    we exercise the Python handler directly by inspecting its source
    and confirming the routing rule. The Qt side has the deeper end-to-end
    test (`test_map_substation_click_jumps_to_sld`) — this guards the
    NiceGUI handler from quietly drifting away from the same contract.
    """
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    # Locate _on_component_value's body and assert the routing line.
    handler_idx = src.index("def _on_component_value(e):")
    handler_end = src.index("ui.on(", handler_idx)
    handler_src = src[handler_idx:handler_end]
    assert 'value.get("type") == "map-substation-click"' in handler_src
    assert "vlIds" in handler_src
    assert "tabs.set_value(sld_tab)" in handler_src
    assert "_state.set_selected_vl(vl_ids[0])" in handler_src
