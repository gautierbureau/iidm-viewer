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


def test_nicegui_appstate_nk_variant_lifecycle():
    """Build → Run LF → Clear lifecycle on the NiceGUI AppState. Each
    transition fires the right listener callback exactly once."""
    import pypowsybl.network as pn
    from iidm_viewer.cache_backend import LF_GEN
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.variants import (
        INITIAL_VARIANT_ID, NK_VARIANT_ID, list_variants,
    )
    from iidm_viewer.web.state import AppState

    state = AppState()
    state.install_network(NetworkProxy(run(pn.create_ieee14)))

    variant_seen: list = []
    lf_seen: list = []
    state.on_nk_variant_changed(variant_seen.append)
    state.on_nk_loadflow_completed(lf_seen.append)

    state.build_nk_variant({"id": "x", "element_ids": ["L1-2-1"]})
    assert state.nk_variant_id == NK_VARIANT_ID
    assert NK_VARIANT_ID in list_variants(state.network)
    assert variant_seen == [NK_VARIANT_ID]

    result = state.run_nk_loadflow({}, {})
    assert result is not None
    assert lf_seen == [result]
    gens = state.cache_backend.get(LF_GEN, {}) or {}
    assert gens.get(NK_VARIANT_ID, 0) >= 1
    assert gens.get(INITIAL_VARIANT_ID, 0) == 0

    state.clear_nk_variant()
    assert state.nk_variant_id is None
    assert NK_VARIANT_ID not in list_variants(state.network)
    assert variant_seen[-1] is None


def test_nicegui_appstate_nk_run_lf_returns_none_without_variant():
    """Running an N-K LF before the variant is built short-circuits."""
    from iidm_viewer.web.state import AppState

    state = AppState()
    seen: list = []
    state.on_nk_loadflow_completed(seen.append)
    assert state.run_nk_loadflow({}, {}) is None
    assert seen == []


def test_nicegui_build_nk_variant_card_uses_shared_helpers():
    """The NiceGUI N-K picker card must funnel through
    ``normalize_manual_contingency`` and the AppState's
    ``build_nk_variant`` / ``run_nk_loadflow`` / ``clear_nk_variant``
    so the picker UI stays in lockstep with the Streamlit + PySide6
    hosts."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_nk_variant_card)
    for token in (
        "normalize_manual_contingency",
        "build_nk_variant",
        "run_nk_loadflow",
        "clear_nk_variant",
        "MANUAL_GROUPING_TOKENS",
        "MANUAL_TYPE_IDS_KEY",
        "on_nk_variant_changed",
        "on_nk_loadflow_completed",
    ):
        assert token in src, f"N-K card should reference {token}"


def test_nicegui_reactive_curves_threads_variant_id():
    """The NiceGUI Reactive Curves tab builder must thread variant_id
    into ``build_reactive_curves_view_model`` so the active variant
    drives the gens frame fetch."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_reactive_curves)
    assert "variant_id=" in src
    assert "on_nk_variant_changed" in src


def test_nicegui_operational_limits_threads_variant_id():
    """Same as Reactive Curves but for the Operational Limits tab."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_operational_limits)
    assert "variant_id=" in src
    assert "on_nk_variant_changed" in src


def test_nicegui_data_explorer_threads_variant_id_and_gates_writes():
    """The Data Explorer builder threads variant_id into the view-model
    builder AND gates the cell-edit + bulk-edit handlers on
    InitialState so N-K mode stays read-only."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_data_explorer)
    assert "variant_id=" in src
    assert "N-K view is read-only" in src
    assert "on_nk_variant_changed" in src


def test_nicegui_sld_panel_threads_variant_id():
    """The NiceGUI SLD tab panel must surface a view-mode select and
    wire ``_sld_variant_id`` to the dock's ``nk_variant_changed``
    listener."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert "sld_view_mode_select" in src
    assert "_sld_variant_id" in src
    # _push_sld reads _sld_variant_id when building the cache key.
    push_src = inspect.getsource(app._push_sld)
    assert "_sld_variant_id" in push_src


def test_nicegui_main_page_includes_nk_variant_card():
    """The main page's sidebar must invoke ``_build_nk_variant_card``
    so the picker is registered alongside the existing LF controls."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert "_build_nk_variant_card()" in src


def test_nicegui_appstate_install_network_clears_nk():
    """A network swap drops the dock state and fires
    nk_variant_changed(None)."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.web.state import AppState

    state = AppState()
    state.install_network(NetworkProxy(run(pn.create_ieee14)))
    state.build_nk_variant({"id": "x", "element_ids": ["L1-2-1"]})

    seen: list = []
    state.on_nk_variant_changed(seen.append)
    state.install_network(NetworkProxy(run(pn.create_ieee14)))
    assert state.nk_variant_id is None
    assert seen == [None]


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
    # Long pypowsybl column names (``regulated_element_id``,
    # ``voltage_regulator_on``, …) must wrap rather than truncate.
    assert default["wrapHeaderText"] is True
    assert default["autoHeaderHeight"] is True


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


def test_app_state_caches_last_report_json_for_view_logs():
    """The "View Logs" button needs ``AppState.last_report_json`` —
    cleared on a fresh network load, populated after every LF run."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.web.state import AppState

    state = AppState()
    assert state.last_report_json is None

    net = NetworkProxy(run(pn.create_ieee14))
    state.install_network(net)
    assert state.last_report_json is None

    result = state.run_loadflow()
    assert result is not None
    assert state.last_report_json
    # Loading a fresh network resets the cached report.
    state.install_network(NetworkProxy(run(pn.create_ieee14)))
    assert state.last_report_json is None


def test_view_logs_dialog_helper_uses_shared_parser_and_gates_empty_input():
    """``_open_lf_report_dialog`` notifies the user when no report is
    available and runs the shared parser otherwise. We inspect the
    function source so the contract can't drift to inline parsing."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._open_lf_report_dialog)
    assert "parse_report_to_tree" in src
    assert "No load flow report available" in src
    # The dialog renders a severity multiselect populated from the shared
    # SEVERITY_LEVELS constant — guards against hard-coded lists.
    assert "SEVERITY_LEVELS" in src
    # Re-parse on every severity change.
    assert "_rebuild_tree" in src


def test_load_options_dialog_uses_shared_helpers():
    """``_open_load_options_dialog`` must funnel everything through
    the shared io_options + render_params_form helpers — guards against
    a future refactor reintroducing inline parsing."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._open_load_options_dialog)
    for token in (
        "get_import_formats",
        "get_import_post_processors",
        "get_format_parameters",
        "filter_changed_params",
        "_render_params_form",
        "_state.import_format",
        "_state.import_params",
        "_state.import_post_processors",
    ):
        assert token in src, f"NiceGUI load-options dialog should reference {token}"


def test_import_options_button_lives_in_left_drawer():
    """The drawer must carry the "Import options…" button so users can
    set format / params / post-processors before uploading a file."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    drawer_start = src.index("ui.left_drawer(")
    drawer_end = src.index("with ui.tabs(", drawer_start)
    drawer_src = src[drawer_start:drawer_end]
    assert "Import options" in drawer_src
    assert "_open_load_options_dialog" in drawer_src


def test_upload_handler_forwards_import_params():
    """The upload handler must thread ``_state.import_params`` and
    ``_state.import_post_processors`` into ``load_from_path`` so the
    LoadOptionsDialog actually affects the next upload."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    handler_start = src.index("async def handle_upload(e):")
    handler_end = src.index(
        "file_lbl.set_text(os.path.basename(name))", handler_start,
    )
    handler_src = src[handler_start:handler_end]
    assert "_state.import_params" in handler_src
    assert "_state.import_post_processors" in handler_src
    assert "parameters=" in handler_src
    assert "post_processors=" in handler_src


def test_refresh_create_panel_no_notify_when_no_node_breaker_vls():
    """Regression for "form blocks the app when no busbar sections":
    on an empty / bus-breaker-only network the refresh path used to
    fire ``ui.notify`` on every redraw, which felt like the UI was
    blocking. The new contract: the expansion stays visible with an
    inline ``ui.label`` placeholder, no toast — same UX as Streamlit's
    ``st.info`` branch.
    """
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._refresh_create_panel)
    assert "ui.notify(" not in src, (
        "_refresh_create_panel must not fire ui.notify — the toast "
        "re-fires on every refresh and feels like the app is blocking."
    )
    # Inline placeholder for both empty-VL and empty-BBS cases.
    assert "node-breaker voltage levels" in src
    assert "No busbar sections" in src


def test_create_extension_panel_uses_shared_module():
    """The NiceGUI extension-create panel builder + refresh helpers
    must funnel everything through :mod:`iidm_viewer.extension_creation`
    — guards against re-introducing inline pypowsybl calls."""
    import inspect
    from iidm_viewer.web import app

    for fn in (
        app._build_create_extension_panel_widgets,
        app._refresh_create_extension_panel,
        app._populate_for_extension,
    ):
        src = inspect.getsource(fn)
        assert "iidm_viewer.extension_creation" in src or (
            "CREATABLE_EXTENSIONS" in src
            or "create_extension" in src
            or "list_extensions_for_component" in src
            or "list_extension_candidates" in src
        ), f"{fn.__name__} should reference the shared module"


def test_extension_create_panel_wired_into_data_explorer():
    """The Data Explorer builder must construct + refresh the
    extension-create panel alongside the other create expansions."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_data_explorer)
    assert "_build_create_extension_panel_widgets" in src
    assert "_refresh_create_extension_panel" in src
    assert "extension_create_state" in src


def test_extensions_explorer_helper_uses_shared_module():
    """``_build_extensions_explorer`` must funnel everything through
    the shared :mod:`iidm_viewer.extensions_data` helpers — guards
    against future inline pypowsybl calls."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_extensions_explorer)
    for token in (
        "list_extension_names",
        "get_extensions_information",
        "get_extension_df",
        "remove_extension",
        "update_extension",
        "ExtensionsExplorerViewModel",
    ):
        assert token in src, f"NiceGUI extensions tab should reference {token}"


def test_extensions_tab_registered_in_main_page():
    """The NiceGUI main page must expose the Extensions Explorer as
    the fifth tab + invoke ``_build_extensions_explorer`` inside its
    panel — same UX as Streamlit's two data-explorer tabs."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert "Data Explorer Extensions" in src
    assert "_build_extensions_explorer()" in src
    # Refresh hook on LF completion + on network changes.
    assert "refresh_extensions_tab" in src


def test_blank_network_dialog_uses_shared_helper():
    """``_open_blank_network_dialog`` must funnel through the shared
    ``network_loader.create_empty`` + ``_state.install_network`` —
    no inline pypowsybl probes."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._open_blank_network_dialog)
    assert "_create_empty_network" in src
    assert "_state.install_network" in src
    assert "import pypowsybl" not in src
    assert "pn.create_empty" not in src


def test_blank_network_button_lives_in_left_drawer():
    """The drawer must carry the "Start with empty network" button
    right after the upload widget, mirroring Streamlit's sidebar."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    drawer_start = src.index("ui.left_drawer(")
    drawer_end = src.index("with ui.tabs(", drawer_start)
    drawer_src = src[drawer_start:drawer_end]
    assert "Start with empty network" in drawer_src
    assert "_open_blank_network_dialog" in drawer_src


def test_network_reduction_dialog_uses_shared_actions():
    """``_open_network_reduction_dialog`` must funnel everything
    through the shared reduction module — no inline pypowsybl calls."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._open_network_reduction_dialog)
    for token in (
        "list_voltage_level_ids",
        "reduce_by_voltage_range",
        "reduce_by_ids",
        "reduce_by_ids_and_depths",
        "_state.notify_network_changed",
        "REDUCTION_METHODS",
    ):
        assert token in src, f"NiceGUI reduction dialog should reference {token}"


def test_network_reduction_button_lives_in_left_drawer():
    """The drawer must carry the "Network Reduction" button. Gated on
    network presence — flipped by ``_on_state_network``."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    drawer_start = src.index("ui.left_drawer(")
    drawer_end = src.index("with ui.tabs(", drawer_start)
    drawer_src = src[drawer_start:drawer_end]
    assert "Network Reduction" in drawer_src
    assert "_open_network_reduction_dialog" in drawer_src
    # The on-network-change handler must flip the button.
    assert "reduction_btn.set_enabled(network is not None)" in src


def test_app_state_notify_network_changed_refires_listeners():
    """``notify_network_changed`` re-fires the network listener for
    the same network — used by reduction to refresh listeners after
    an in-place mutation."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.web.state import AppState

    state = AppState()
    state.install_network(NetworkProxy(run(pn.create_ieee14)))
    seen: list = []
    state.on_network_changed(seen.append)

    state.notify_network_changed()
    assert len(seen) == 1
    assert seen[0] is state.network


def test_save_network_dialog_uses_shared_helpers():
    """The "Save network" modal must funnel through the shared
    :func:`network_loader.export_network` and
    :func:`network_loader.guess_mime_for_export` — guards against a
    future refactor that re-implements either inline."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._open_save_network_dialog)
    assert "export_network" in src
    assert "guess_mime_for_export" in src
    assert "get_export_formats" in src
    # The download uses NiceGUI's ``ui.download`` API; we don't pin the
    # exact call shape (it changed between 2.x and 3.x) but we want a
    # download invocation present.
    assert "ui.download" in src


def test_import_and_save_dialogs_do_not_use_expansion_add():
    """Regression for the ``AttributeError: 'Expansion' object has no
    attribute 'add'`` crash reported by the user.

    NiceGUI's :class:`Expansion` element accepts children via a
    ``with`` block — not via a non-existent ``.add(child)`` method.
    Both modal dialogs (Import options, Save network) previously
    relied on the missing API and crashed the moment the user clicked
    the sidebar button.
    """
    import inspect

    from iidm_viewer.web import app

    for fn in (app._open_load_options_dialog, app._open_save_network_dialog):
        src = inspect.getsource(fn)
        # ``ui.expansion`` itself must still be there — only the wrong
        # ``.add(`` follow-up call is forbidden. ``params_container``
        # has to be defined inside a ``with`` block on the expansion.
        assert "ui.expansion" in src
        assert ".add(params_container)" not in src
        # The replacement pattern: nest the column inside the
        # expansion's ``with`` block.
        assert "with params_box:" in src


def test_save_network_button_is_gated_on_network_presence():
    """The drawer's "Save network" button toggles with the network —
    matches the Streamlit sidebar gate."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    drawer_start = src.index("ui.left_drawer(")
    drawer_end = src.index("with ui.tabs(", drawer_start)
    drawer_src = src[drawer_start:drawer_end]
    assert "Save network" in drawer_src
    # The on-network-change handler must flip the button.
    assert "save_btn.set_enabled(network is not None)" in src


def test_lf_parameters_dialog_uses_shared_helpers():
    """``_open_lf_parameters_dialog`` must funnel everything through
    the shared :mod:`iidm_viewer.lf_parameters_schema` helpers so
    parsing / coercion / filtering stays in one place."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._open_lf_parameters_dialog)
    for token in (
        "GENERIC_PARAMETERS",
        "group_provider_params_by_category",
        "parse_provider_options",
        "coerce_provider_value",
        "filter_changed_generic_params",
        "filter_changed_provider_params",
    ):
        assert token in src, f"NiceGUI LF params dialog should reference {token}"


def test_app_state_caches_lf_params_and_forwards_to_run_loadflow(monkeypatch):
    """``AppState.run_loadflow`` should pick up ``lf_generic_params`` /
    ``lf_provider_params`` when the caller doesn't pass them."""
    import pypowsybl.network as pn
    from iidm_viewer import loadflow as lf_mod
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.web.state import AppState

    state = AppState()
    state.install_network(NetworkProxy(run(pn.create_ieee14)))
    state.lf_generic_params = {"distributed_slack": False}
    state.lf_provider_params = {"slackBusSelectionMode": "FIRST"}

    captured: dict = {}

    def fake_run_ac(net, generic, provider):
        captured["generic"] = generic
        captured["provider"] = provider
        return lf_mod.LoadFlowResult([], "{}")

    monkeypatch.setattr("iidm_viewer.web.state.run_ac", fake_run_ac)
    state.run_loadflow()
    assert captured["generic"] == {"distributed_slack": False}
    assert captured["provider"] == {"slackBusSelectionMode": "FIRST"}


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


def test_clear_diagrams_blanks_caches_and_last_args():
    """``_clear_diagrams`` wipes the diagram caches *and* replaces the
    cached ``_last_*`` payloads with blanks. The blanks are what gets
    resent when an iframe re-mounts after a tab switch — without them
    the previously-rendered SVG would come back."""
    from iidm_viewer.web import app

    # Seed cache + last-args so we can prove they get wiped.
    # Cache keys are (vl_id, depth, variant_id) for NAD and
    # (container_id, variant_id) for SLD as of N-K step 8.
    from iidm_viewer.variants import INITIAL_VARIANT_ID
    app._nad_cache[("VL1", 1, INITIAL_VARIANT_ID)] = ("<svg/>", "{}")
    app._sld_cache[("VL1", INITIAL_VARIANT_ID)] = ("<svg/>", "{}")
    app._last_nad = {"svg": "<svg>...</svg>", "metadata": "{}", "height": 700}
    app._last_sld = {
        "svg": "<svg>...</svg>", "metadata": "{}",
        "height": 700, "svgType": "voltage-level",
    }

    app._clear_diagrams()

    assert app._nad_cache == {}
    assert app._sld_cache == {}
    assert app._last_nad == {"svg": "", "metadata": "", "height": 700}
    assert app._last_sld == {
        "svg": "", "metadata": "", "height": 700,
        "svgType": "voltage-level",
    }


def test_on_state_network_clears_diagrams_on_swap():
    """Regression for: "I start from an empty network after I already
    loaded one, and the NAD / SLD are not refreshed."

    When the open network is swapped, ``_on_state_network`` must call
    ``_clear_diagrams`` *before* deciding what to do with the picker
    so the previous network's diagrams disappear immediately — even
    when no default VL gets picked (empty network → no
    ``_on_state_vl`` follow-up to overwrite the SVGs).
    """
    import inspect

    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    handler_idx = src.index("def _on_state_network(network):")
    handler_end = src.index("def _on_state_vl", handler_idx)
    handler_src = src[handler_idx:handler_end]
    assert "_clear_diagrams()" in handler_src


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


def test_reactive_curves_tab_registered_in_main_page():
    """``Reactive Capability Curves`` must be a top-level tab + its
    refresh closure must be wired into the page-wide listeners (network
    swap, VL change, LF completion)."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert 'ui.tab("Reactive Capability Curves")' in src
    assert "refresh_reactive_curves = _build_reactive_curves()" in src
    # The refresh closure must fire on the same hooks as the other
    # data-driven tabs.
    assert "refresh_reactive_curves()" in src


def test_reactive_curves_builder_uses_shared_view_model():
    """The NiceGUI builder must compose the shared view model + plot
    helpers — that's how PySide6 + Streamlit stay in sync."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_reactive_curves)
    assert "build_reactive_curves_view_model" in src
    assert "build_generator_plot_data" in src
    assert "build_containment_summary" in src
    assert "STATUS_DIAMOND_COLOR" in src


def test_nicegui_left_drawer_carries_view_script_button():
    """The NiceGUI sidebar must surface a "View live Script" button so
    the user can open the auto-recorded HMI-mirror script — parity with
    Streamlit + PySide6."""
    import inspect

    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert 'ui.button("View live Script")' in src
    assert "view_script_btn.on_click(_open_session_script_dialog)" in src


def test_open_session_script_dialog_uses_shared_recorder_and_generator():
    """The NiceGUI dialog must reuse ``script_recorder`` + ``generate_script``
    so it stays in lockstep with Streamlit + PySide6."""
    import inspect

    from iidm_viewer.web import app

    src = inspect.getsource(app._open_session_script_dialog)
    assert "from iidm_viewer.script_generator import generate_script" in src
    assert "script_recorder.get_log" in src
    assert "script_recorder.is_paused" in src
    assert "script_recorder.set_paused" in src
    assert "script_recorder.clear_log" in src


def test_nicegui_state_records_load_and_create_empty_and_loadflow():
    """``AppState`` mutators must drive the shared recorder so the
    NiceGUI host's Session Script captures the same ops as Streamlit."""
    import os

    import pypowsybl.network as pn

    from iidm_viewer import script_recorder
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.web.state import AppState

    script_recorder.reset_store()
    try:
        state = AppState()
        # Load a real network — recorder seeds with a ``load_network`` op.
        xiidm = os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm"),
        )
        state.load_network_from_path(xiidm)
        ops = script_recorder.get_log()
        assert ops and ops[0]["kind"] == "load_network"
        # Loadflow appends a ``run_loadflow`` op.
        state.run_loadflow()
        assert script_recorder.get_log()[-1]["kind"] == "run_loadflow"
        # Empty network resets the log and seeds with ``create_empty``.
        empty = NetworkProxy(run(pn.create_empty))
        state.install_network(empty)
        script_recorder.record_create_empty("blank")
        seeded = script_recorder.get_log()
        assert seeded[0]["kind"] == "create_empty"
        assert seeded[0]["network_id"] == "blank"
    finally:
        script_recorder.reset_store()


def test_operational_limits_tab_registered_in_main_page():
    """``Operational Limits`` must be a top-level tab + its refresh
    closure must be wired into the page-wide listeners (network swap +
    load-flow completion)."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert 'ui.tab("Operational Limits")' in src
    assert "refresh_operational_limits = _build_operational_limits()" in src
    assert "refresh_operational_limits()" in src


def test_operational_limits_builder_uses_shared_view_model():
    """The NiceGUI builder must compose the shared view model + chart
    builder so PySide6 + Streamlit stay in sync."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_operational_limits)
    assert "build_operational_limits_view_model" in src
    assert "build_element_chart" in src


def test_security_analysis_tab_registered_in_main_page():
    """``Security Analysis`` must be a top-level tab + its refresh
    closure must be wired into the network-changed listener."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert 'ui.tab("Security Analysis")' in src
    assert "refresh_security_analysis = _build_security_analysis()" in src
    assert "refresh_security_analysis()" in src


def test_security_analysis_builder_uses_shared_core():
    """The NiceGUI builder must compose the shared contingency builders
    + runner + summary so PySide6 + Streamlit stay in sync."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_security_analysis)
    assert "build_n1_contingencies" in src
    assert "build_n2_contingencies" in src
    assert "run_security_analysis" in src
    assert "summarize_security_results" in src


def test_short_circuit_analysis_tab_registered_in_main_page():
    """``Short Circuit Analysis`` must be a top-level tab + its refresh
    closure must be wired into the network-changed listener."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert 'ui.tab("Short Circuit Analysis")' in src
    assert "refresh_short_circuit_analysis = _build_short_circuit_analysis()" in src
    assert "refresh_short_circuit_analysis()" in src


def test_short_circuit_analysis_builder_uses_shared_core():
    """The NiceGUI builder must compose the shared bus-fault builder +
    runner + view-model so PySide6 + Streamlit stay in sync. After the
    Step 5 view-model extraction the summary / metric calls go through
    ``ShortCircuitViewModel`` rather than the bare helper functions."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_short_circuit_analysis)
    assert "build_bus_faults" in src
    assert "run_short_circuit_analysis" in src
    assert "ShortCircuitViewModel" in src
    assert "vm.summary_df" in src
    assert "vm.failure_count" in src
    assert "vm.with_violations_count" in src
    assert "make_sc_params" in src


def test_pmax_visualization_tab_registered_in_main_page():
    """``Pmax Visualization`` must be a top-level tab + its refresh
    closure must be wired into the network-changed + VL-changed +
    LF-completed listeners."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert 'ui.tab("Pmax Visualization")' in src
    assert "refresh_pmax = _build_pmax_visualization()" in src
    # Tab must follow VL changes (the "Only lines connected to VL X"
    # toggle relies on the listener firing).
    assert src.count("refresh_pmax()") >= 3


def test_pmax_visualization_builder_uses_shared_core():
    """The NiceGUI builder must compose the shared compute + chart +
    view-model so PySide6 + Streamlit stay in sync. After Step 6 the
    summary + filter calls go through ``PmaxViewModel``."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_pmax_visualization)
    assert "compute_pmax_data" in src
    assert "build_pangle_chart" in src
    assert "PmaxViewModel" in src
    assert "vm.display_df" in src
    assert "vm.rows_df" in src


def test_voltage_analysis_tab_registered_in_main_page():
    """``Voltage Analysis`` must be a top-level tab + its refresh
    closure must be wired into the network-changed + LF-completed
    listeners (no VL-changed wiring — the section is network-wide)."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert 'ui.tab("Voltage Analysis")' in src
    assert "refresh_voltage_analysis = _build_voltage_analysis()" in src
    # Called from the on_state_network branch (both load + clear) and
    # the on_loadflow_completed listener → at least 3 sites.
    assert src.count("refresh_voltage_analysis()") >= 3


def test_voltage_analysis_builder_uses_shared_core():
    """The NiceGUI builder must compose the shared compute + display
    helpers so PySide6 + Streamlit stay in sync."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_voltage_analysis)
    assert "compute_voltage_analysis" in src
    assert "build_bus_summary" in src
    assert "build_bus_detail" in src
    assert "build_shunt_display" in src
    assert "build_svc_display" in src
    assert "split_shunts_by_b" in src
    assert "shunt_totals" in src
    assert "svc_totals" in src
    assert "bus_pu_classify" in src


def test_injection_map_tab_registered_in_main_page():
    """``Injection Map`` must be a top-level tab + its refresh closure
    must be wired into the network-changed + LF-completed listeners.
    No VL-changed wiring — the section is network-wide."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert 'ui.tab("Injection Map")' in src
    assert "refresh_injection_map = _build_injection_map()" in src
    # Called from the on_state_network branch (both load + clear) and
    # the on_loadflow_completed listener → at least 3 sites.
    assert src.count("refresh_injection_map()") >= 3


def test_injection_map_builder_uses_shared_helpers():
    """The NiceGUI builder must compose the shared Leaflet HTML +
    caption helpers from :mod:`iidm_viewer.injection_map` so PySide6 +
    Streamlit stay in sync."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_injection_map)
    assert "build_injection_map_html" in src
    assert "injection_map_caption" in src
    assert "_extract_injection_data" in src
    assert "InjectionMapViewModel" in src
    assert "_METRIC_OPTIONS" in src
    assert "_VIEW_OPTIONS" in src
    # The HTML lands in a sandboxed iframe via srcdoc.
    assert "srcdoc" in src


def test_overview_tab_registered_in_main_page():
    """``Overview`` must be a top-level tab + its refresh closure must
    be wired into the network-changed + LF-completed listeners. No
    VL-changed wiring — the section is network-wide."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app.main_page)
    assert 'ui.tab("Overview")' in src
    assert "refresh_overview = _build_overview()" in src
    # Called from the on_state_network branch (both load + clear) and
    # the on_loadflow_completed listener → at least 3 sites.
    assert src.count("refresh_overview()") >= 3


def test_overview_builder_uses_shared_core():
    """The NiceGUI builder must compose the shared compute + display
    helpers so PySide6 + Streamlit stay in sync."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_overview)
    assert "compute_overview_data" in src
    assert "build_country_totals_display" in src
    assert "build_losses_by_country_display" in src
    assert "country_totals_has_lf" in src


def test_voltage_analysis_builder_renders_geographical_map():
    """The NiceGUI tab must include the Leaflet voltage map driven by
    the shared :func:`build_voltage_map_html` helper — ports parity
    with the Streamlit + PySide6 hosts."""
    import inspect
    from iidm_viewer.web import app

    src = inspect.getsource(app._build_voltage_analysis)
    # Builder + worker-routed fetch from the shared core.
    assert "build_voltage_map_html" in src
    assert "_extract_voltage_map_data" in src
    assert "voltage_map_caption" in src
    assert "nominal_voltage_options" in src
    # Controls: nominal voltage filter, layout, view mode, ± pu spin.
    assert "_LAYOUT_OPTIONS" in src
    assert "_VIEW_OPTIONS" in src
    # The HTML lands in a sandboxed iframe via srcdoc.
    assert "srcdoc" in src
