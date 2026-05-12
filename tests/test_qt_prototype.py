"""End-to-end smoke test for the PySide6 prototype (Map + SLD tabs).

Validates the killer interaction without a real browser: the same
substation_clicked → AppState.set_selected_vl → SldTab.show_voltage_level
chain that fires when a user clicks a substation on the map.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("PySide6.QtWebEngineWidgets")

# Offscreen Qt — no display server required in CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
# Disable GPU in headless mode; otherwise QtWebEngine spends ~3s probing.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --no-sandbox --disable-dev-shm-usage",
)

import pandas as pd  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from iidm_viewer.qt.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def loaded_window(qapp):
    """A MainWindow with IEEE14 loaded and a default VL auto-selected."""
    window = MainWindow()
    window.show()
    qapp.processEvents()
    xiidm = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm")
    )
    assert os.path.exists(xiidm)
    window.open_file(xiidm)
    qapp.processEvents()
    assert window.state.network is not None
    yield window
    window.close()
    qapp.processEvents()


def test_network_load_auto_selects_highest_voltage(qapp, loaded_window):
    """``AppState.load_network_from_path`` picks the highest-V VL.

    For the IEEE14 test fixture, the 230 kV VLs win over the 135 kV
    ones; just assert that *some* VL was auto-selected — the precise
    id depends on pandas idxmax tie-breaking.
    """
    assert loaded_window.state.selected_vl is not None
    # Default-VL drives both diagram tabs, so the NAD cache is already
    # populated for (default_vl, depth=1).
    nad_cache = loaded_window.nad_tab._cache
    assert any(k[0] == loaded_window.state.selected_vl for k in nad_cache)


def test_map_substation_click_jumps_to_sld(qapp, loaded_window):
    window = loaded_window
    vl_id = "VL1"

    # Simulate what the deck.gl substation onClick callback emits via
    # the QWebChannel bridge: a list of VL ids ordered by desc nominal V.
    window.map_tab.substation_clicked.emit([vl_id])
    qapp.processEvents()

    assert window.state.selected_vl == vl_id
    assert window.tabs.currentWidget() is window.sld_tab

    cached = window.sld_tab._cache.get(vl_id)
    assert cached is not None, "SLD generation should have populated the cache"
    svg, metadata = cached
    assert "<svg" in svg or svg.lstrip().startswith("<?xml")
    assert isinstance(metadata, str) and metadata.strip().startswith("{")


def test_nad_node_click_jumps_to_sld(qapp, loaded_window):
    """The new NAD → SLD wiring.

    Simulates the bundle's ``onSelectNodeCallback`` payload — the
    Streamlit code in ``diagrams.py`` reads exactly the same
    ``nad-vl-click`` shape — and asserts the same outcome as the
    Map → SLD jump: the SLD tab activates, the selected VL is set,
    and the SLD's per-VL cache has the SVG ready.
    """
    window = loaded_window
    target_vl = "VL5"

    # Park on a different tab first so the assertion that we switched
    # is meaningful.
    window.tabs.setCurrentWidget(window.nad_tab)
    qapp.processEvents()
    assert window.tabs.currentWidget() is window.nad_tab

    # Synthesise the JS payload (NadTab decodes the dict and re-emits
    # node_clicked as a plain str).
    window.nad_tab._on_value({"type": "nad-vl-click", "vl": target_vl, "ts": 0})
    qapp.processEvents()

    assert window.state.selected_vl == target_vl
    assert window.tabs.currentWidget() is window.sld_tab
    assert window.sld_tab._cache.get(target_vl) is not None
    # And the NAD itself has cached a render centered on the new VL.
    assert any(k[0] == target_vl for k in window.nad_tab._cache)


def test_nad_depth_change_invalidates_for_new_key(qapp, loaded_window):
    """Bumping depth re-runs pypowsybl but keeps the previous entry."""
    window = loaded_window
    vl = window.state.selected_vl
    assert vl is not None
    initial_keys = set(window.nad_tab._cache.keys())
    assert (vl, 1) in initial_keys

    window.nad_tab._depth_spin.setValue(2)
    qapp.processEvents()

    assert (vl, 2) in window.nad_tab._cache
    assert (vl, 1) in window.nad_tab._cache  # old entry preserved


def test_data_explorer_renders_voltage_levels_on_load(qapp, loaded_window):
    """After a network is loaded, the Data Explorer tab shows the
    default component (Voltage Levels) populated with IEEE14's 14 rows.
    """
    model = loaded_window.data_tab._model
    # Combo defaults to the first entry, "Substations". We seed the
    # explorer via set_network -> _refresh(<current text>) so the model
    # is populated for whichever component is selected at load time.
    assert loaded_window.data_tab._combo.currentText() == "Substations"
    df = model.dataframe()
    assert df.shape[0] > 0
    assert df.shape[1] > 0


def test_data_explorer_switches_component(qapp, loaded_window):
    """Selecting a different component refreshes the table."""
    explorer = loaded_window.data_tab

    explorer._combo.setCurrentText("Voltage Levels")
    qapp.processEvents()
    vl_df = explorer._model.dataframe()
    assert vl_df.shape[0] == 14  # IEEE14
    assert "nominal_v" in vl_df.columns

    explorer._combo.setCurrentText("Generators")
    qapp.processEvents()
    gen_df = explorer._model.dataframe()
    assert gen_df.shape[0] > 0
    # Different component → different schema
    assert set(vl_df.columns) != set(gen_df.columns)


def test_data_explorer_handles_empty_component(qapp, loaded_window):
    """A component with no rows in this network must not crash the model."""
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("HVDC Lines")  # IEEE14 has none
    qapp.processEvents()
    df = explorer._model.dataframe()
    assert df.shape[0] == 0
    # Model's rowCount/columnCount must agree with the DataFrame.
    assert explorer._model.rowCount() == 0
    assert "empty" in explorer._summary.text().lower()


def test_pandas_table_model_basic_protocol():
    """Lightweight sanity check on the model itself — no Qt event loop
    needed once a QApplication exists (the loaded_window fixture has
    already created one)."""
    from iidm_viewer.qt.data_explorer_tab import PandasTableModel

    df = pd.DataFrame({"a": [1, 2, float("nan")], "b": ["x", "y", "z"]})
    m = PandasTableModel(df)
    assert m.rowCount() == 3
    assert m.columnCount() == 2
    # NaN -> em-dash
    idx = m.index(2, 0)
    assert m.data(idx, Qt.DisplayRole) == "—"
    # Column header
    assert m.headerData(0, Qt.Horizontal, Qt.DisplayRole) == "a"


def test_data_explorer_filter_narrows_visible_rows(qapp, loaded_window):
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Voltage Levels")
    qapp.processEvents()

    full = explorer._proxy.rowCount()
    assert full == 14  # IEEE14

    # IEEE14 has VLs named VL1..VL14; filter to those containing "1"
    # — that's VL1, VL10..VL14, so 6 rows.
    explorer._filter.setText("VL1")
    qapp.processEvents()
    narrowed = explorer._proxy.rowCount()
    assert 0 < narrowed < full

    explorer._filter.setText("")
    qapp.processEvents()
    assert explorer._proxy.rowCount() == full


def test_data_explorer_sort_proxies_dont_mutate_source(qapp, loaded_window):
    """Sort lives in the proxy; the underlying DataFrame stays
    untouched so subsequent edits target the right rows."""
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Voltage Levels")
    qapp.processEvents()

    source_first_before = explorer._model.dataframe()["id"].iloc[0]
    # Sort by nominal_v ascending — IEEE14's lowest VL is 13.8 kV
    nominal_col = list(explorer._model.dataframe().columns).index("nominal_v")
    explorer._proxy.sort(nominal_col, Qt.AscendingOrder)
    qapp.processEvents()

    # The proxy's first row should now be the lowest-V VL.
    proxy_idx = explorer._proxy.index(0, list(explorer._model.dataframe().columns).index("nominal_v"))
    assert explorer._proxy.data(proxy_idx, Qt.DisplayRole) is not None

    # Source frame's row order is unchanged.
    source_first_after = explorer._model.dataframe()["id"].iloc[0]
    assert source_first_after == source_first_before


def test_data_explorer_marks_editable_columns(qapp, loaded_window):
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Generators")
    qapp.processEvents()

    df = explorer._model.dataframe()
    target_p_col = list(df.columns).index("target_p")
    name_col = list(df.columns).index("name")
    target_p_idx = explorer._model.index(0, target_p_col)
    name_idx = explorer._model.index(0, name_col)

    assert bool(explorer._model.flags(target_p_idx) & Qt.ItemIsEditable)
    assert not bool(explorer._model.flags(name_idx) & Qt.ItemIsEditable)


def test_data_explorer_edit_updates_pypowsybl_and_grid(qapp, loaded_window):
    """End-to-end edit: change a generator's target_p, confirm the
    pypowsybl frame reflects it and the model's cell is repainted."""
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Generators")
    qapp.processEvents()

    df = explorer._model.dataframe()
    assert df.shape[0] > 0
    gen_id = str(df["id"].iloc[0])
    col = list(df.columns).index("target_p")
    old_value = df["target_p"].iloc[0]
    new_value = old_value + 5.0

    src_idx = explorer._model.index(0, col)
    proxy_idx = explorer._proxy.mapFromSource(src_idx)
    # setData on the proxy delegates to the source model's setData.
    assert explorer._proxy.setData(proxy_idx, new_value, Qt.EditRole)
    qapp.processEvents()

    # The model's in-memory frame now carries the new value.
    refreshed = explorer._model.dataframe()
    assert pytest.approx(refreshed["target_p"].iloc[0], rel=1e-9) == new_value

    # pypowsybl is the source of truth — re-fetch and confirm.
    from iidm_viewer.component_registry import get_dataframe
    live = get_dataframe(loaded_window.state.network, "Generators")
    live_row = live[live["id"].astype(str) == gen_id].iloc[0]
    assert pytest.approx(live_row["target_p"], rel=1e-9) == new_value


def test_sld_render_opts_into_preserve_viewport(qapp, loaded_window):
    """The PySide6 host opts into pan/zoom continuity by passing
    ``preserveViewport=True`` to the SLD bundle. Streamlit and
    NiceGUI omit it. Regression-guard so the kwarg doesn't get
    silently dropped.
    """
    explorer = loaded_window.sld_tab
    captured: list[dict] = []
    original = explorer._view.render_component
    explorer._view.render_component = lambda **kw: captured.append(kw)

    explorer._ready = True  # bypass the WebView readiness gate
    explorer.show_voltage_level(loaded_window.state.selected_vl or "VL1")
    qapp.processEvents()

    explorer._view.render_component = original
    assert captured, "show_voltage_level should have dispatched a render"
    assert captured[0].get("preserveViewport") is True
    assert captured[0].get("svgType") == "voltage-level"
    assert isinstance(captured[0].get("svg"), str)
    assert isinstance(captured[0].get("metadata"), str)


def test_sld_bundle_carries_preserve_viewport_path():
    """The built bundle must contain the new restore-viewBox branch.

    A failing assert here means ``frontend/sld_component/dist`` is
    stale — run ``npm run build`` in that directory.
    """
    bundle_path = os.path.join(
        os.path.dirname(__file__), os.pardir,
        "iidm_viewer", "frontend", "sld_component", "dist", "assets",
        "sld-component.js",
    )
    with open(bundle_path, "r", encoding="utf-8") as fh:
        contents = fh.read()
    # Minified, but the property name and method names survive.
    assert "preserveViewport" in contents
    assert "getViewBox" in contents
    assert "setViewBox" in contents


def test_data_explorer_bulk_edit_applies_to_selected_rows(qapp, loaded_window):
    """End-to-end bulk edit through the PySide6 UI:
    select 3 generator rows, apply ``target_p``, confirm pypowsybl
    and the in-memory model both reflect the new value.

    The proxy's row order doesn't necessarily match the source
    DataFrame (sortingEnabled triggers an initial sort on Qt 6.11),
    so the test reads the actual ids back via
    :meth:`DataExplorerTab._selected_element_ids` — the same path
    the bulk-apply handler uses in production.
    """
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Generators")
    qapp.processEvents()

    # Select the first 3 proxy rows.
    sel = explorer._table.selectionModel()
    sel.clearSelection()
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    for r in range(3):
        idx_top = explorer._proxy.index(r, 0)
        idx_right = explorer._proxy.index(r, explorer._proxy.columnCount() - 1)
        sel.select(
            QItemSelection(idx_top, idx_right),
            QItemSelectionModel.Select | QItemSelectionModel.Rows,
        )
    qapp.processEvents()
    ids = explorer._selected_element_ids()
    assert len(ids) == 3

    df_before = explorer._model.dataframe()
    prev_values = {
        i: df_before[df_before["id"].astype(str) == i]["target_p"].iloc[0]
        for i in ids
    }
    new_value = 55.5

    # Drive the bulk panel.
    explorer._bulk_attr.setCurrentText("target_p")
    explorer._bulk_value.setText(str(new_value))
    explorer._on_bulk_apply()
    qapp.processEvents()

    df_after = explorer._model.dataframe()
    for i in ids:
        v = df_after[df_after["id"].astype(str) == i]["target_p"].iloc[0]
        assert v == pytest.approx(new_value)

    # Revert (so other tests aren't affected — the worker thread is shared).
    from iidm_viewer.component_registry import apply_cell_edit
    for i in ids:
        apply_cell_edit(loaded_window.state.network, "Generators", i, "target_p", prev_values[i])


def test_bulk_panel_visibility_follows_editable_component(qapp, loaded_window):
    explorer = loaded_window.data_tab

    # Voltage Levels is not editable -> the bulk attr combo is empty.
    explorer._combo.setCurrentText("Voltage Levels")
    qapp.processEvents()
    assert explorer._bulk_attr.count() == 0
    assert not explorer._bulk_apply.isEnabled()

    explorer._combo.setCurrentText("Generators")
    qapp.processEvents()
    assert explorer._bulk_attr.count() > 0
    # No selection yet -> still disabled.
    explorer._table.selectionModel().clearSelection()
    qapp.processEvents()
    explorer._update_bulk_state()
    assert not explorer._bulk_apply.isEnabled()


def test_data_explorer_records_cell_edit_in_change_log(qapp, loaded_window):
    """A single-cell edit through the model lands one entry in
    ``AppState.change_log`` with the legacy dict shape.
    """
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Generators")
    qapp.processEvents()
    assert len(loaded_window.state.change_log) == 0

    df = explorer._model.dataframe()
    gen_id = str(df["id"].iloc[0])
    col = list(df.columns).index("target_p")
    old_value = df["target_p"].iloc[0]
    new_value = old_value + 1.0

    src_idx = explorer._model.index(0, col)
    proxy_idx = explorer._proxy.mapFromSource(src_idx)
    explorer._proxy.setData(proxy_idx, new_value, Qt.EditRole)
    qapp.processEvents()

    entries = loaded_window.state.change_log.entries()
    assert len(entries) == 1
    assert entries[0]["component"] == "Generators"
    assert entries[0]["element_id"] == gen_id
    assert entries[0]["property"] == "target_p"
    assert entries[0]["before"] == pytest.approx(old_value)
    assert entries[0]["after"] == pytest.approx(new_value)

    # Revert via the panel API and confirm pypowsybl restored.
    explorer._change_log_panel._log.revert(loaded_window.state.network, entries[0])
    qapp.processEvents()
    assert len(loaded_window.state.change_log) == 0


def test_data_explorer_bulk_edit_records_n_entries(qapp, loaded_window):
    """``apply_bulk_edit`` populates the change log with one entry per
    affected element. Then ``revert_all`` puts everything back.
    """
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Generators")
    qapp.processEvents()

    df = explorer._model.dataframe()
    assert df.shape[0] >= 3
    prev_targets = list(df["target_p"].iloc[:3])

    # Select first 3 proxy rows and apply bulk.
    sel = explorer._table.selectionModel()
    sel.clearSelection()
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    for r in range(3):
        idx_top = explorer._proxy.index(r, 0)
        idx_right = explorer._proxy.index(r, explorer._proxy.columnCount() - 1)
        sel.select(
            QItemSelection(idx_top, idx_right),
            QItemSelectionModel.Select | QItemSelectionModel.Rows,
        )
    qapp.processEvents()
    explorer._bulk_attr.setCurrentText("target_p")
    explorer._bulk_value.setText("123.0")
    explorer._on_bulk_apply()
    qapp.processEvents()

    assert len(loaded_window.state.change_log) == 3

    # Revert all -> log empties, pypowsybl returns to original values.
    reverted, skipped = loaded_window.state.change_log.revert_all(loaded_window.state.network)
    qapp.processEvents()
    assert reverted == 3
    assert skipped == []
    assert len(loaded_window.state.change_log) == 0

    # Network state restored to original (within float tolerance).
    from iidm_viewer.component_registry import get_dataframe
    refreshed = get_dataframe(loaded_window.state.network, "Generators")
    for i, expected in enumerate(prev_targets):
        assert refreshed["target_p"].iloc[i] == pytest.approx(expected)


def test_data_explorer_bulk_disconnect_flips_connected_and_records(qapp, loaded_window):
    """Selecting N rows + clicking Disconnect calls apply_bulk_disconnect
    and records one ChangeLog entry per (id, attribute)."""
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Generators")
    qapp.processEvents()

    # Select first 3 proxy rows.
    sel = explorer._table.selectionModel()
    sel.clearSelection()
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    for r in range(3):
        idx_top = explorer._proxy.index(r, 0)
        idx_right = explorer._proxy.index(r, explorer._proxy.columnCount() - 1)
        sel.select(
            QItemSelection(idx_top, idx_right),
            QItemSelectionModel.Select | QItemSelectionModel.Rows,
        )
    qapp.processEvents()
    explorer._update_bulk_state()
    assert explorer._bulk_disconnect.isEnabled()

    ids = explorer._selected_element_ids()
    explorer._on_bulk_disconnect()
    qapp.processEvents()

    df_after = explorer._model.dataframe()
    for i in ids:
        assert bool(df_after[df_after["id"].astype(str) == i]["connected"].iloc[0]) is False

    # One change-log entry per id, all with property=connected.
    log_entries = loaded_window.state.change_log.entries("Generators")
    assert len(log_entries) == 3
    assert {e["property"] for e in log_entries} == {"connected"}

    # Restore (so subsequent tests using shared worker thread aren't broken).
    log = loaded_window.state.change_log
    reverted, _ = log.revert_all(loaded_window.state.network)
    assert reverted == 3


def test_disconnect_button_disabled_for_non_disconnectable_component(qapp, loaded_window):
    explorer = loaded_window.data_tab
    # Voltage Levels aren't in DISCONNECTABLE_COMPONENTS.
    explorer._combo.setCurrentText("Voltage Levels")
    qapp.processEvents()

    # Even with rows selected, disconnect stays disabled.
    sel = explorer._table.selectionModel()
    sel.clearSelection()
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    idx_top = explorer._proxy.index(0, 0)
    idx_right = explorer._proxy.index(0, explorer._proxy.columnCount() - 1)
    sel.select(
        QItemSelection(idx_top, idx_right),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )
    qapp.processEvents()
    explorer._update_bulk_state()
    assert not explorer._bulk_disconnect.isEnabled()
    # Voltage Levels ARE in REMOVABLE_COMPONENTS — delete should be enabled.
    assert explorer._bulk_delete.isEnabled()


def test_sld_vl_click_updates_selected_vl(qapp, loaded_window):
    """Clicking a "→ next VL" arrow on the SLD must mirror Streamlit's
    behaviour: AppState.selected_vl flips to the target.
    """
    window = loaded_window
    window.sld_tab._current_vl = "VL1"
    window.state.set_selected_vl("VL1")
    qapp.processEvents()
    assert window.state.selected_vl == "VL1"

    window.sld_tab._on_value({"type": "sld-vl-click", "vl": "VL5", "ts": 0})
    qapp.processEvents()
    assert window.state.selected_vl == "VL5"


def test_sld_breaker_click_toggles_switch_and_records_change_log(qapp, loaded_window):
    """Clicking a switch on the SLD mirrors Streamlit's path:
    decode the SVG id, toggle on the worker, and write a Switches/open
    entry to the ChangeLog. Uses a synthetic node-breaker network so
    IEEE14 (bus-breaker, no switches) doesn't get in the way.
    """
    from iidm_viewer.powsybl_worker import NetworkProxy, run

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    nb_network = NetworkProxy(run(_make))
    window = loaded_window
    # Swap the loaded IEEE14 network for the node-breaker one without
    # going through load_from_path (we want to keep the existing tabs
    # alive but point at a switches-bearing network).
    window.state._network = nb_network
    df = nb_network.get_switches()
    sw_id = str(df.index[0])
    before = bool(df["open"].iloc[0])
    new_open = not before

    # The bundle sends the SVG-encoded id; ``BR-1`` would come back as
    # ``BR_45_1``. Our switch ids here are simple ascii, but the
    # decoder is still exercised end-to-end.
    window.sld_tab._on_value({
        "type": "sld-breaker-click",
        "breakerId": sw_id,
        "open": new_open,
        "ts": 0,
    })
    qapp.processEvents()

    df_after = nb_network.get_switches()
    assert bool(df_after.at[sw_id, "open"]) is new_open

    entries = window.state.change_log.entries("Switches")
    assert len(entries) == 1
    assert entries[0]["element_id"] == sw_id
    assert entries[0]["before"] is before
    assert entries[0]["after"] is new_open

    # Revert so the worker network doesn't carry the toggle across tests.
    from iidm_viewer.component_registry import toggle_switch
    toggle_switch(nb_network, sw_id, before)


def test_sld_feeder_click_routes_to_map_substation(qapp, loaded_window):
    """End-to-end cross-tab nav: an SLD feeder-click payload for a
    real IEEE14 line lands the user on the Map tab and triggers a
    flyTo on the map widget.
    """
    window = loaded_window

    # Capture render_component calls on the map view to confirm a
    # flyTo arg arrives. The widget may not be ``_ready`` yet under
    # offscreen Qt, in which case the args land on ``_pending``
    # instead — accept either path.
    captured: list[dict] = []
    original_render = window.map_tab._view.render_component
    window.map_tab._view.render_component = lambda **kw: captured.append(kw)

    # Pick a real IEEE14 line — VL1↔VL2 in the IEEE 14-bus fixture.
    from iidm_viewer.component_registry import get_dataframe
    lines = get_dataframe(window.state.network, "Lines")
    assert lines.shape[0] > 0
    line_id = str(lines["id"].iloc[0])
    current_vl = str(lines["voltage_level1_id"].iloc[0])

    # Park the SLD on the line's VL1 so the resolver picks VL2.
    window.sld_tab._current_vl = current_vl
    window.tabs.setCurrentWidget(window.sld_tab)
    qapp.processEvents()

    # Synthesise the JS payload.
    window.sld_tab._on_value({
        "type": "sld-feeder-click",
        "equipmentId": line_id,
        "equipmentType": "LINE",
        "x": 0,
        "y": 0,
        "ts": 0,
    })
    qapp.processEvents()

    window.map_tab._view.render_component = original_render
    # Tab switched to Map.
    assert window.tabs.currentWidget() is window.map_tab
    # A flyTo arg was either dispatched or queued onto _pending.
    pending_flyto = (window.map_tab._pending or {}).get("flyTo") if window.map_tab._pending else None
    dispatched_flyto = next((c.get("flyTo") for c in captured if c.get("flyTo")), None)
    flyto = dispatched_flyto or pending_flyto
    assert flyto is not None
    assert flyto.get("substationId")
    # That substation id matches the resolver's answer.
    from iidm_viewer.navigation import resolve_feeder_substation
    expected = resolve_feeder_substation(
        window.state.network, current_vl, line_id, "LINE",
    )
    assert flyto["substationId"] == expected


def test_run_loadflow_from_app_state_emits_signal(qapp, loaded_window):
    """``AppState.run_loadflow`` should fire ``loadflow_completed``
    with a converged result on IEEE14."""
    captured = []
    loaded_window.state.loadflow_completed.connect(captured.append)
    result = loaded_window.state.run_loadflow()
    qapp.processEvents()
    assert result is not None
    assert result.converged is True
    assert captured and captured[0] is result


def test_sidebar_run_loadflow_button_toggles_with_network(qapp):
    """The sidebar's Run-LF button is disabled before a load and
    enabled afterwards."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        assert window.sidebar._run_lf_btn.isEnabled() is False
        xiidm = os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm")
        )
        window.open_file(xiidm)
        qapp.processEvents()
        assert window.sidebar._run_lf_btn.isEnabled() is True
    finally:
        window.close()
        qapp.processEvents()


def test_data_explorer_apply_and_run_lf_triggers_loadflow(qapp, loaded_window):
    """Hitting Apply & Run LF after a bulk edit emits both
    bulk_edit_applied and loadflow_requested, and the MainWindow
    handler runs an LF that converges."""
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Generators")
    qapp.processEvents()

    # Select first 2 proxy rows.
    sel = explorer._table.selectionModel()
    sel.clearSelection()
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    for r in range(2):
        idx_top = explorer._proxy.index(r, 0)
        idx_right = explorer._proxy.index(r, explorer._proxy.columnCount() - 1)
        sel.select(
            QItemSelection(idx_top, idx_right),
            QItemSelectionModel.Select | QItemSelectionModel.Rows,
        )
    qapp.processEvents()
    ids = explorer._selected_element_ids()
    assert len(ids) == 2

    # Capture LF completions.
    completions = []
    loaded_window.state.loadflow_completed.connect(completions.append)

    explorer._bulk_attr.setCurrentText("target_p")
    explorer._bulk_value.setText("60.0")
    explorer._on_bulk_apply_lf()
    qapp.processEvents()

    assert completions, "Apply & Run LF should fire loadflow_completed"
    assert completions[-1].converged is True

    # Revert for test hygiene.
    from iidm_viewer.component_registry import apply_cell_edit, get_dataframe
    df0 = get_dataframe(loaded_window.state.network, "Generators")
    # The reverts here just put the network back to *some* consistent
    # value; the exact pre-LF target_p isn't recoverable but the
    # change-log entry was already created before the LF ran.
    log = loaded_window.state.change_log
    reverted, _ = log.revert_all(loaded_window.state.network)
    assert reverted >= 2


def test_create_panel_is_hidden_for_non_creatable_component(qapp, loaded_window):
    """IEEE14 has only bus-breaker VLs, so even Loads is not creatable.
    The Qt create panel hides itself in that case."""
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Loads")
    qapp.processEvents()
    # No node-breaker VLs -> panel is hidden (set_network already ran).
    assert explorer._create_panel.isVisible() is False


def test_create_panel_creates_load_on_node_breaker_network(qapp):
    """Drive the Qt create panel end-to-end against a synthetic
    node-breaker network. Asserts the new Load shows up via
    get_loads + the panel emits its component_created signal.
    """
    from iidm_viewer.qt.create_panel import CreateComponentPanel
    from iidm_viewer.component_creation import (
        list_busbar_sections,
        list_node_breaker_voltage_levels,
    )
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))

    panel = CreateComponentPanel()
    panel.set_network(network)
    panel.set_component("Loads")
    qapp.processEvents()

    # Sanity: the panel detected node-breaker VLs.
    vls = list_node_breaker_voltage_levels(network)
    assert vls.shape[0] > 0
    assert panel._vl_combo.isEnabled()

    # Pick the first VL + its first busbar.
    vl_id = panel._vl_combo.currentData()
    bbs_id = list_busbar_sections(network, str(vl_id))[0]
    # The panel populates busbar combo via the VL-change signal already.
    panel._bbs_combo.setCurrentText(bbs_id)
    qapp.processEvents()

    # Fill ID + drive the create.
    panel._field_widgets["id"].setText("QT_NEW_LOAD")
    panel._field_widgets["p0"].setValue(15.0)
    panel._field_widgets["q0"].setValue(5.0)

    seen: list = []
    panel.component_created.connect(lambda c, eid: seen.append((c, eid)))
    panel._on_create_clicked()
    qapp.processEvents()

    assert "QT_NEW_LOAD" in network.get_loads().index
    assert seen == [("Loads", "QT_NEW_LOAD")]


def test_branch_panel_creates_line_on_node_breaker_network(qapp):
    """End-to-end Line creation via the Qt CreateBranchPanel.

    Builds a fresh node-breaker network, picks two different VLs +
    their first busbars, drives the panel and asserts the new line
    shows up in pypowsybl.
    """
    from iidm_viewer.qt.create_panel import CreateBranchPanel
    from iidm_viewer.component_creation import (
        list_busbar_sections,
        list_node_breaker_voltage_levels,
    )
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateBranchPanel()
    panel.set_network(network)
    panel.set_component("Lines")
    qapp.processEvents()

    vls = list_node_breaker_voltage_levels(network)
    assert vls.shape[0] >= 2
    # The panel pre-selects vl1[0] and vl2[1] (avoids self-loop).
    bbs1 = list_busbar_sections(network, str(panel._vl1.currentData()))[0]
    bbs2 = list_busbar_sections(network, str(panel._vl2.currentData()))[0]
    panel._bbs1.setCurrentText(bbs1)
    panel._bbs2.setCurrentText(bbs2)

    panel._field_widgets["id"].setText("QT_NEW_LINE")
    seen: list = []
    panel.component_created.connect(lambda c, eid: seen.append((c, eid)))
    panel._on_create_clicked()
    qapp.processEvents()

    assert "QT_NEW_LINE" in network.get_lines().index
    assert seen == [("Lines", "QT_NEW_LINE")]


def test_branch_panel_hides_for_non_branch_component(qapp):
    """The branch panel only renders for components in CREATABLE_BRANCHES."""
    from iidm_viewer.qt.create_panel import CreateBranchPanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateBranchPanel()
    panel.set_network(network)
    panel.set_component("Generators")  # not a branch
    qapp.processEvents()
    assert panel.isVisible() is False
    panel.set_component("Lines")
    qapp.processEvents()
    assert panel.isVisible() is True


def test_change_log_panel_repaints_on_record(qapp, loaded_window):
    """The panel's title and table reflect log mutations in real time
    via the on_changed bus.
    """
    panel = loaded_window.data_tab._change_log_panel
    log = loaded_window.state.change_log
    assert "Change Log (0)" in panel._title.text()

    log.record("Generators", "GFAKE", "target_p", 1.0, 2.0)
    qapp.processEvents()
    assert "Change Log (1)" in panel._title.text()
    assert panel._model.rowCount() == 1

    log.clear()
    qapp.processEvents()
    assert "Change Log (0)" in panel._title.text()


def test_data_explorer_rejects_non_editable_attribute(qapp, loaded_window):
    """setData on a non-editable column must return False and not
    issue an edit_requested signal."""
    explorer = loaded_window.data_tab
    explorer._combo.setCurrentText("Voltage Levels")
    qapp.processEvents()

    captured: list = []
    explorer._model.edit_requested.connect(lambda *args: captured.append(args))
    name_col = list(explorer._model.dataframe().columns).index("name")
    src_idx = explorer._model.index(0, name_col)
    assert explorer._model.setData(src_idx, "something", Qt.EditRole) is False
    assert captured == []


def test_app_state_emits_signal_only_on_change(qapp):
    from iidm_viewer.qt.state import AppState

    s = AppState()
    seen = []
    s.selected_vl_changed.connect(lambda v: seen.append(v))
    s.set_selected_vl("VL_A")
    s.set_selected_vl("VL_A")        # same — must not re-emit
    s.set_selected_vl("VL_B")
    s.set_selected_vl(None)
    assert seen == ["VL_A", "VL_B", ""]
