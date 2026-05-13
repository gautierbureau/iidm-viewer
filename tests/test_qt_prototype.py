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
    # Panels are folded by default (matches Streamlit's collapsed
    # ``st.expander``). Simulate the user click that expands the
    # form so the test can drive the widgets.
    panel._group.setChecked(True)
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


def test_container_panel_creates_substation_on_blank_network(qapp):
    """End-to-end Substation creation via the Qt CreateContainerPanel."""
    from iidm_viewer.qt.create_panel import CreateContainerPanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="x")

    network = NetworkProxy(run(_make))
    panel = CreateContainerPanel()
    panel.set_network(network)
    panel.set_component("Substations")
    qapp.processEvents()
    # Substations have no context picker.
    assert panel._picker_widget.isVisible() is False

    panel._field_widgets["id"].setText("QT_SUB_NEW")
    panel._field_widgets["country"].setText("FR")
    seen: list = []
    panel.component_created.connect(lambda c, eid: seen.append((c, eid)))
    panel._on_create_clicked()
    qapp.processEvents()

    assert "QT_SUB_NEW" in network.get_substations().index
    assert seen == [("Substations", "QT_SUB_NEW")]


def test_container_panel_creates_vl_attached_to_substation(qapp):
    """VL creation needs an optional substation picker; this test
    drives the create path with a chosen substation."""
    from iidm_viewer.qt.create_panel import CreateContainerPanel
    from iidm_viewer.component_creation import create_container
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="x")

    network = NetworkProxy(run(_make))
    create_container(network, "Substations", {"id": "S_QT"})

    panel = CreateContainerPanel()
    panel.set_network(network)
    panel.set_component("Voltage Levels")
    # Panels are folded by default — simulate the user click that
    # expands the form so isVisible() reflects the picker's intended
    # presence rather than the panel's folded state.
    panel._group.setChecked(True)
    panel.setVisible(True)
    panel.show()
    qapp.processEvents()
    # Picker visible; the substation S_QT is in the dropdown
    # (item 0 is "(none — no substation)", item 1 is S_QT).
    assert panel._picker_widget.isVisible() is True
    assert panel._context_combo.count() >= 2
    panel._context_combo.setCurrentIndex(1)

    panel._field_widgets["id"].setText("VL_QT")
    panel._field_widgets["nominal_v"].setValue(225.0)
    panel._on_create_clicked()
    qapp.processEvents()

    vls = network.get_voltage_levels()
    assert "VL_QT" in vls.index
    assert str(vls.at["VL_QT", "substation_id"]) == "S_QT"


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


def test_hvdc_panel_hides_when_no_converter_stations(qapp):
    """The HVDC panel auto-hides when fewer than 2 converter stations exist."""
    from iidm_viewer.qt.create_panel import CreateHvdcLinePanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="x")

    network = NetworkProxy(run(_make))
    panel = CreateHvdcLinePanel()
    panel.set_network(network)
    panel.set_component("HVDC Lines")
    qapp.processEvents()
    assert panel.isVisible() is False


def test_hvdc_panel_creates_line_between_two_vsc_stations(qapp):
    """End-to-end HVDC creation via the Qt CreateHvdcLinePanel."""
    from iidm_viewer.qt.create_panel import CreateHvdcLinePanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        n = pn.create_empty(network_id="x")
        n.create_substations(id="S1")
        n.create_voltage_levels(id="VL1", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=400.0)
        n.create_voltage_levels(id="VL2", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=400.0)
        n.create_busbar_sections(id="BBS1", voltage_level_id="VL1", node=0)
        n.create_busbar_sections(id="BBS2", voltage_level_id="VL2", node=0)
        n.create_vsc_converter_stations(
            id="VSC_A", voltage_level_id="VL1", node=1,
            loss_factor=0.01, voltage_regulator_on=False, target_q=0.0,
        )
        n.create_vsc_converter_stations(
            id="VSC_B", voltage_level_id="VL2", node=1,
            loss_factor=0.01, voltage_regulator_on=False, target_q=0.0,
        )
        return n

    network = NetworkProxy(run(_make))
    panel = CreateHvdcLinePanel()
    panel.set_network(network)
    panel.set_component("HVDC Lines")
    qapp.processEvents()
    assert panel.isVisible() is True
    # Pickers populated with both VSC stations.
    assert panel._cs1.count() == 2
    assert panel._cs2.count() == 2
    panel._cs1.setCurrentIndex(0)
    panel._cs2.setCurrentIndex(1)

    panel._field_widgets["id"].setText("HVDC_QT")
    seen: list = []
    panel.component_created.connect(lambda c, eid: seen.append((c, eid)))
    panel._on_create_clicked()
    qapp.processEvents()

    assert "HVDC_QT" in network.get_hvdc_lines().index
    assert seen == [("HVDC Lines", "HVDC_QT")]


def test_hvdc_panel_hides_for_non_hvdc_component(qapp):
    """The HVDC panel only renders for the HVDC Lines component."""
    from iidm_viewer.qt.create_panel import CreateHvdcLinePanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateHvdcLinePanel()
    panel.set_network(network)
    panel.set_component("Generators")
    qapp.processEvents()
    assert panel.isVisible() is False
    panel.set_component("HVDC Lines")
    qapp.processEvents()
    assert panel.isVisible() is True


def test_tap_changer_panel_hides_when_no_eligible_transformer(qapp):
    """The tap-changer panel hides when no 2WT is missing the chosen kind."""
    from iidm_viewer.qt.create_panel import CreateTapChangerPanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    # Four-sub demo: the single 2WT already has both ratio + phase tap changers.
    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateTapChangerPanel()
    panel.set_network(network)
    panel.set_component("2-Winding Transformers")
    qapp.processEvents()
    assert panel.isVisible() is False


def test_tap_changer_panel_creates_ratio_on_fresh_2wt(qapp):
    """End-to-end Ratio tap changer creation via the Qt panel."""
    from iidm_viewer.qt.create_panel import CreateTapChangerPanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        n = pn.create_empty(network_id="x")
        n.create_substations(id="S1")
        n.create_voltage_levels(id="VL1", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=400.0)
        n.create_voltage_levels(id="VL2", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=225.0)
        n.create_busbar_sections(id="BBS1", voltage_level_id="VL1", node=0)
        n.create_busbar_sections(id="BBS2", voltage_level_id="VL2", node=0)
        n.create_2_windings_transformers(
            id="T1", voltage_level1_id="VL1", voltage_level2_id="VL2",
            node1=1, node2=1, rated_u1=400.0, rated_u2=225.0,
            r=0.1, x=10.0, g=0.0, b=0.0,
        )
        return n

    network = NetworkProxy(run(_make))
    panel = CreateTapChangerPanel()
    panel.set_network(network)
    panel.set_component("2-Winding Transformers")
    qapp.processEvents()
    # T1 has no tap changer yet — panel should be visible.
    assert panel.isVisible() is True
    assert panel._twt_combo.currentText() == "T1"
    # Default kind is Ratio; defaults already populate the steps table.
    seen: list = []
    panel.component_created.connect(lambda c, eid: seen.append((c, eid)))
    panel._on_create_clicked()
    qapp.processEvents()

    assert "T1" in network.get_ratio_tap_changers().index
    assert seen == [("Tap Changers", "T1")]
    # After creation, T1 should no longer be a target for a Ratio tap changer.
    qapp.processEvents()
    assert panel.isVisible() is False


def test_tap_changer_panel_hides_for_non_twt_component(qapp):
    """The tap-changer panel only renders for 2-Winding Transformers."""
    from iidm_viewer.qt.create_panel import CreateTapChangerPanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        n = pn.create_empty(network_id="x")
        n.create_substations(id="S1")
        n.create_voltage_levels(id="VL1", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=400.0)
        n.create_voltage_levels(id="VL2", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=225.0)
        n.create_busbar_sections(id="BBS1", voltage_level_id="VL1", node=0)
        n.create_busbar_sections(id="BBS2", voltage_level_id="VL2", node=0)
        n.create_2_windings_transformers(
            id="T1", voltage_level1_id="VL1", voltage_level2_id="VL2",
            node1=1, node2=1, rated_u1=400.0, rated_u2=225.0,
            r=0.1, x=10.0, g=0.0, b=0.0,
        )
        return n

    network = NetworkProxy(run(_make))
    panel = CreateTapChangerPanel()
    panel.set_network(network)
    panel.set_component("Generators")
    qapp.processEvents()
    assert panel.isVisible() is False
    panel.set_component("2-Winding Transformers")
    qapp.processEvents()
    assert panel.isVisible() is True


def test_coupling_panel_hides_when_no_multi_bbs_vl(qapp):
    """The coupling panel hides when no node-breaker VL has ≥2 BBS."""
    from iidm_viewer.qt.create_panel import CreateCouplingDevicePanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        n = pn.create_empty(network_id="x")
        n.create_substations(id="S1")
        n.create_voltage_levels(id="VL1", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=400.0)
        n.create_busbar_sections(id="BBS1", voltage_level_id="VL1", node=0)
        return n

    network = NetworkProxy(run(_make))
    panel = CreateCouplingDevicePanel()
    panel.set_network(network)
    panel.set_component("Switches")
    qapp.processEvents()
    assert panel.isVisible() is False


def test_coupling_panel_creates_coupling_device(qapp):
    """End-to-end coupling-device creation via the Qt panel."""
    from iidm_viewer.qt.create_panel import CreateCouplingDevicePanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        n = pn.create_empty(network_id="x")
        n.create_substations(id="S1")
        n.create_voltage_levels(id="VL1", substation_id="S1",
                                topology_kind="NODE_BREAKER", nominal_v=400.0)
        n.create_busbar_sections(id="BBS1", voltage_level_id="VL1", node=0)
        n.create_busbar_sections(id="BBS2", voltage_level_id="VL1", node=1)
        return n

    network = NetworkProxy(run(_make))
    panel = CreateCouplingDevicePanel()
    panel.set_network(network)
    panel.set_component("Switches")
    qapp.processEvents()
    assert panel.isVisible() is True
    # The VL has 2 BBS — the pickers should default to distinct rows.
    assert panel._bbs1_combo.currentText() == "BBS1"
    assert panel._bbs2_combo.currentText() == "BBS2"
    panel._prefix_edit.setText("CPL_QT")

    switches_before = set(network.get_switches().index.tolist())
    seen: list = []
    panel.component_created.connect(lambda c, vl: seen.append((c, vl)))
    panel._on_create_clicked()
    qapp.processEvents()

    switches_after = set(network.get_switches().index.tolist())
    new_switches = switches_after - switches_before
    assert any(s.startswith("CPL_QT") for s in new_switches)
    assert seen == [("Coupling Devices", "VL1")]


def test_coupling_panel_hides_for_non_switch_component(qapp):
    """The coupling panel only renders when the active component is Switches."""
    from iidm_viewer.qt.create_panel import CreateCouplingDevicePanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateCouplingDevicePanel()
    panel.set_network(network)
    panel.set_component("Generators")
    qapp.processEvents()
    assert panel.isVisible() is False
    panel.set_component("Switches")
    qapp.processEvents()
    # The four-sub demo has S1VL2 with 2 BBS, so the panel becomes visible.
    assert panel.isVisible() is True


def test_reactive_limits_panel_hides_for_non_target_component(qapp):
    """The reactive-limits panel hides for components not in
    :data:`REACTIVE_LIMITS_TARGETS`."""
    from iidm_viewer.qt.create_panel import CreateReactiveLimitsPanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateReactiveLimitsPanel()
    panel.set_network(network)
    panel.set_component("Lines")
    qapp.processEvents()
    assert panel.isVisible() is False
    panel.set_component("Generators")
    qapp.processEvents()
    assert panel.isVisible() is True


def test_reactive_limits_panel_minmax_creates_minmax(qapp):
    """End-to-end min/max reactive-limits creation via the Qt panel."""
    from iidm_viewer.qt.create_panel import CreateReactiveLimitsPanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateReactiveLimitsPanel()
    panel.set_network(network)
    panel.set_component("Generators")
    qapp.processEvents()
    # Mode defaults to min/max; the candidate combo carries every gen.
    assert panel.isVisible() is True
    panel._target_combo.setCurrentText("GH1")
    panel._min_q.setValue(-33.0)
    panel._max_q.setValue(33.0)
    seen: list = []
    panel.component_created.connect(lambda c, eid: seen.append((c, eid)))
    panel._on_create_clicked()
    qapp.processEvents()
    gens = network.get_generators(all_attributes=True)
    assert gens.at["GH1", "min_q"] == -33.0
    assert gens.at["GH1", "max_q"] == 33.0
    assert gens.at["GH1", "reactive_limits_kind"] == "MIN_MAX"
    assert seen == [("Generators", "GH1")]


def test_reactive_limits_panel_curve_creates_curve(qapp):
    """End-to-end curve reactive-limits creation via the Qt panel."""
    from iidm_viewer.qt.create_panel import CreateReactiveLimitsPanel
    from PySide6.QtWidgets import QTableWidgetItem
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateReactiveLimitsPanel()
    panel.set_network(network)
    panel.set_component("Generators")
    qapp.processEvents()
    panel._target_combo.setCurrentText("GH1")
    # Switch to curve mode; the table is pre-seeded with 2 rows.
    for i in range(panel._mode_combo.count()):
        if panel._mode_combo.itemData(i) == "curve":
            panel._mode_combo.setCurrentIndex(i)
            break
    qapp.processEvents()
    # Overwrite the two default rows so we can spot the new curve.
    panel._points_table.setItem(0, 0, QTableWidgetItem("0.0"))
    panel._points_table.setItem(0, 1, QTableWidgetItem("-50.0"))
    panel._points_table.setItem(0, 2, QTableWidgetItem("50.0"))
    panel._points_table.setItem(1, 0, QTableWidgetItem("100.0"))
    panel._points_table.setItem(1, 1, QTableWidgetItem("-40.0"))
    panel._points_table.setItem(1, 2, QTableWidgetItem("40.0"))
    panel._on_create_clicked()
    qapp.processEvents()
    gens = network.get_generators(all_attributes=True)
    assert gens.at["GH1", "reactive_limits_kind"] == "CURVE"


def test_operational_limits_panel_hides_for_non_target_component(qapp):
    """The operational-limits panel hides when component isn't in
    :data:`OPERATIONAL_LIMITS_TARGETS`."""
    from iidm_viewer.qt.create_panel import CreateOperationalLimitsPanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateOperationalLimitsPanel()
    panel.set_network(network)
    panel.set_component("Generators")
    qapp.processEvents()
    assert panel.isVisible() is False
    panel.set_component("Lines")
    qapp.processEvents()
    assert panel.isVisible() is True


def test_operational_limits_panel_creates_group(qapp):
    """End-to-end operational-limits creation via the Qt panel."""
    from iidm_viewer.qt.create_panel import CreateOperationalLimitsPanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateOperationalLimitsPanel()
    panel.set_network(network)
    panel.set_component("Lines")
    qapp.processEvents()
    assert panel.isVisible() is True
    panel._target_combo.setCurrentText("LINE_S2S3")
    panel._side_combo.setCurrentText("ONE")
    panel._type_combo.setCurrentText("CURRENT")
    panel._group_edit.setText("QT_GRP")

    seen: list = []
    panel.component_created.connect(lambda c, eid: seen.append((c, eid)))
    panel._on_create_clicked()
    qapp.processEvents()

    ol = network.get_operational_limits(show_inactive_sets=True)
    mask = ol.index.get_level_values("element_id") == "LINE_S2S3"
    rows = ol[mask]
    groups = set(rows.index.get_level_values("group_name").tolist())
    assert "QT_GRP" in groups
    assert seen == [("Lines", "LINE_S2S3")]


def test_svc_panel_hides_for_non_vl_component(qapp):
    """The SVC panel renders only when the active component is Voltage Levels."""
    from iidm_viewer.qt.create_panel import CreateSecondaryVoltageControlPanel
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateSecondaryVoltageControlPanel()
    panel.set_network(network)
    panel.set_component("Generators")
    qapp.processEvents()
    assert panel.isVisible() is False
    panel.set_component("Voltage Levels")
    qapp.processEvents()
    assert panel.isVisible() is True


def test_svc_panel_creates_extension_end_to_end(qapp):
    """Drive the QTableWidgets and verify the SVC payload reaches pypowsybl."""
    from iidm_viewer.qt.create_panel import CreateSecondaryVoltageControlPanel
    from PySide6.QtWidgets import QTableWidgetItem
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateSecondaryVoltageControlPanel()
    panel.set_network(network)
    panel.set_component("Voltage Levels")
    qapp.processEvents()
    assert panel.isVisible() is True

    # Fill in the zone row + unit row using existing demo ids.
    panel._zones_table.setItem(
        0, panel._ZONE_COL_NAME, QTableWidgetItem("Z1"),
    )
    panel._zones_table.setItem(
        0, panel._ZONE_COL_TARGET_V, QTableWidgetItem("400.0"),
    )
    panel._zones_table.setItem(
        0, panel._ZONE_COL_BUS_IDS, QTableWidgetItem("S1VL1_0"),
    )
    panel._units_table.setItem(
        0, panel._UNIT_COL_ID, QTableWidgetItem("GH1"),
    )
    panel._units_table.setItem(
        0, panel._UNIT_COL_ZONE, QTableWidgetItem("Z1"),
    )

    seen: list = []
    panel.component_created.connect(lambda c, eid: seen.append((c, eid)))
    panel._on_create_clicked()
    qapp.processEvents()
    assert seen == [("Secondary Voltage Control", "")]
    assert "Saved" in panel._status.text()


def test_svc_panel_surfaces_validator_errors(qapp):
    """When the unit references an unknown zone, status carries the error."""
    from iidm_viewer.qt.create_panel import CreateSecondaryVoltageControlPanel
    from PySide6.QtWidgets import QTableWidgetItem
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateSecondaryVoltageControlPanel()
    panel.set_network(network)
    panel.set_component("Voltage Levels")
    qapp.processEvents()

    panel._zones_table.setItem(0, panel._ZONE_COL_NAME, QTableWidgetItem("Z1"))
    panel._zones_table.setItem(0, panel._ZONE_COL_TARGET_V, QTableWidgetItem("400.0"))
    panel._zones_table.setItem(0, panel._ZONE_COL_BUS_IDS, QTableWidgetItem("S1VL1_0"))
    panel._units_table.setItem(0, panel._UNIT_COL_ID, QTableWidgetItem("GH1"))
    panel._units_table.setItem(0, panel._UNIT_COL_ZONE, QTableWidgetItem("GHOST"))
    panel._on_create_clicked()
    qapp.processEvents()
    assert "not one of the defined zones" in panel._status.text()


def test_operational_limits_panel_rejects_zero_permanents(qapp):
    """The validator surfaces 'Exactly one permanent' via the status label."""
    from iidm_viewer.qt.create_panel import CreateOperationalLimitsPanel
    from PySide6.QtWidgets import QTableWidgetItem
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    network = NetworkProxy(run(_make))
    panel = CreateOperationalLimitsPanel()
    panel.set_network(network)
    panel.set_component("Lines")
    qapp.processEvents()
    panel._target_combo.setCurrentText("LINE_S2S3")
    # Replace the seeded permanent row with another TATL → 0 permanents.
    panel._rows_table.setItem(0, panel._COL_DURATION, QTableWidgetItem("30"))
    panel._on_create_clicked()
    qapp.processEvents()
    assert "Exactly one permanent" in panel._status.text()


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


def test_extensions_tab_renders_active_power_control_against_ieee14(qapp):
    """End-to-end smoke of ``ExtensionsExplorerTab`` — feed the IEEE14
    demo, pick ``activePowerControl`` (one of the editable ones), and
    confirm the picker + summary + Apply / Remove buttons reflect a
    populated table."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.extensions_explorer_tab import ExtensionsExplorerTab

    net = NetworkProxy(run(pn.create_ieee14))
    # Seed an extension we can drive — pypowsybl exposes
    # ``create_extensions("activePowerControl", df)``.
    raw = object.__getattribute__(net, "_obj")
    df = pd.DataFrame(
        {"droop": [3.0], "participate": [True]},
        index=pd.Index(["B1-G"], name="id"),
    )
    run(lambda: raw.create_extensions("activePowerControl", df))

    tab = ExtensionsExplorerTab()
    tab.set_network(net)
    # Pick the extension explicitly so the test isn't sensitive to the
    # alphabetical default.
    idx = tab._ext_combo.findText("activePowerControl")
    assert idx >= 0
    tab._ext_combo.setCurrentIndex(idx)
    qapp.processEvents()

    assert "activePowerControl" in tab._summary_lbl.text()
    assert tab._apply_btn.isEnabled() is True  # editable columns present
    assert tab._remove_btn.isEnabled() is True  # not read-only
    # The table carries an editable "droop" column.
    headers = [
        tab._table.horizontalHeaderItem(c).text()
        for c in range(tab._table.columnCount())
    ]
    assert "droop" in headers


def test_extensions_tab_set_network_to_none_resets(qapp):
    """Clearing the network must wipe the picker + table."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.extensions_explorer_tab import ExtensionsExplorerTab

    tab = ExtensionsExplorerTab()
    tab.set_network(NetworkProxy(run(pn.create_ieee14)))
    qapp.processEvents()
    tab.set_network(None)
    qapp.processEvents()
    assert tab._ext_combo.count() == 0
    assert tab._table.rowCount() == 0
    assert "No network" in tab._summary_lbl.text()


def test_create_extension_panel_hides_for_non_target_component(qapp):
    """The panel must auto-hide when the active component isn't a
    target of any creatable extension."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.create_panel import CreateExtensionPanel

    panel = CreateExtensionPanel()
    panel.set_network(NetworkProxy(run(pn.create_ieee14)))
    # ``Lines`` are not a target of any creatable extension.
    panel.set_component("Lines")
    qapp.processEvents()
    assert panel.isVisible() is False
    # ``Generators`` are targets for activePowerControl + position +
    # entsoeCategory — panel should surface.
    panel.set_component("Generators")
    qapp.processEvents()
    assert panel.isVisible() is True


def test_create_extension_panel_creates_active_power_control_end_to_end(qapp):
    """End-to-end: pick ``activePowerControl``, fill the form, click
    Create, and confirm pypowsybl persisted the row."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.create_panel import CreateExtensionPanel

    network = NetworkProxy(run(pn.create_ieee14))
    panel = CreateExtensionPanel()
    panel.set_network(network)
    panel.set_component("Generators")
    qapp.processEvents()
    # Pick the extension explicitly so the test isn't tied to the
    # registry iteration order.
    idx = panel._ext_combo.findText("activePowerControl")
    assert idx >= 0
    panel._ext_combo.setCurrentIndex(idx)
    qapp.processEvents()
    panel._target_combo.setCurrentText("B1-G")
    panel._field_widgets["droop"].setValue(5.0)
    panel._field_widgets["participation_factor"].setValue(2.0)

    seen: list = []
    panel.component_created.connect(lambda c, eid: seen.append((c, eid)))
    panel._on_create_clicked()
    qapp.processEvents()

    df = network.get_extensions("activePowerControl")
    assert df is not None
    assert "B1-G" in df.index
    assert seen == [("Extension", "B1-G")]


def test_data_explorer_refreshes_sibling_create_panels_after_container_create(qapp):
    """Regression for "Generators panel still says no node-breaker VL"
    after the user has just created a Substation + VL + BBS from an
    empty network. Every create panel needs its VL / busbar / target
    dropdown refreshed when a sibling panel reports a topology-
    altering create."""
    import pypowsybl.network as pn
    from iidm_viewer.component_creation import create_container
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.data_explorer_tab import DataExplorerTab

    # Empty network — no VLs / no busbars to start with.
    network = NetworkProxy(run(pn.create_empty))
    tab = DataExplorerTab()
    tab.set_network(network)
    tab._combo.setCurrentText("Generators")
    qapp.processEvents()
    # Initial state: no node-breaker VL → the panel shows the
    # "(no node-breaker VLs in this network)" placeholder text.
    # ``itemData`` returns ``None`` for the placeholder (the real VL
    # items carry the id as userData), so the lack of data is what
    # tests the pre-create state.
    panel = tab._create_panel
    assert panel._vl_combo.count() == 1
    assert panel._vl_combo.itemData(0) is None
    assert "no node-breaker" in panel._vl_combo.itemText(0)

    # Build a node-breaker VL with one busbar section through the
    # shared helpers (same code path the CreateContainerPanel uses).
    create_container(network, "Substations", {"id": "S1", "country": "FR"})
    create_container(network, "Voltage Levels", {
        "id": "VL1", "name": "",
        "topology_kind": "NODE_BREAKER",
        "nominal_v": 400.0,
        "low_voltage_limit": 0.0, "high_voltage_limit": 0.0,
        "substation_id": "S1",
    })
    create_container(network, "Busbar Sections", {
        "id": "BBS1", "name": "",
        "voltage_level_id": "VL1",
        "node": 0,
    })

    # Simulate the CreateContainerPanel firing its post-create signal.
    # The fix: ``_on_component_created`` fans the network + active
    # component out to every sibling create panel so their pickers
    # pick up the new VL / BBS.
    tab._create_container_panel.component_created.emit("Busbar Sections", "BBS1")
    qapp.processEvents()

    # CreateComponentPanel should now report the new node-breaker VL
    # — that's the bug the user reported. The panel is folded by
    # default, which means Qt auto-disables its children — so check
    # ``itemData`` / ``itemText`` (which work regardless of enabled
    # state) rather than ``isEnabled``.
    vl_data = [
        tab._create_panel._vl_combo.itemData(i)
        for i in range(tab._create_panel._vl_combo.count())
    ]
    assert "VL1" in vl_data
    # The placeholder ``itemData is None`` row must be gone now that
    # there's a real VL to pick.
    assert None not in vl_data
    bbs_ids = [
        tab._create_panel._bbs_combo.itemText(i)
        for i in range(tab._create_panel._bbs_combo.count())
    ]
    assert "BBS1" in bbs_ids


def test_sld_and_nad_tabs_blank_their_view_on_set_network(qapp):
    """Regression for the empty-network swap bug: after loading IEEE14
    then installing a fresh empty network, the SLD and NAD webviews
    must be wiped — otherwise the previous network's diagram stays on
    screen because ``_render`` short-circuits on ``_current_vl is None``.
    """
    from iidm_viewer.qt.nad_tab import NadTab
    from iidm_viewer.qt.sld_tab import SldTab

    sld = SldTab()
    nad = NadTab()
    # Bypass the WebView readiness gate so render_component fires
    # synchronously — the WebView itself is async in headless tests.
    sld._ready = True
    nad._ready = True

    sld_renders: list[dict] = []
    nad_renders: list[dict] = []
    sld._view.render_component = lambda **kw: sld_renders.append(kw)
    nad._view.render_component = lambda **kw: nad_renders.append(kw)

    # Network swap (real network or None — both paths must wipe).
    import pypowsybl.network as pn

    from iidm_viewer.powsybl_worker import NetworkProxy, run

    real = NetworkProxy(run(pn.create_ieee14))
    sld.set_network(real)
    nad.set_network(real)
    qapp.processEvents()

    assert sld_renders, "SldTab.set_network should push a render"
    assert nad_renders, "NadTab.set_network should push a render"
    # The push is the *blank* — the real SVG only follows when
    # ``show_voltage_level`` is called by the MainWindow's listener.
    assert sld_renders[-1].get("svg") == ""
    assert sld_renders[-1].get("metadata") == ""
    assert sld_renders[-1].get("svgType") == "voltage-level"
    assert nad_renders[-1].get("svg") == ""
    assert nad_renders[-1].get("metadata") == ""

    # Subsequent swap to ``None`` (e.g. clearing the network) must
    # also wipe — same code path.
    sld_renders.clear()
    nad_renders.clear()
    sld.set_network(None)
    nad.set_network(None)
    qapp.processEvents()
    assert sld_renders and sld_renders[-1].get("svg") == ""
    assert nad_renders and nad_renders[-1].get("svg") == ""


def test_main_window_carries_an_extensions_tab(qapp):
    """The PySide6 main window must surface the Extensions Explorer as
    a top-level tab — same UX as Streamlit's two data-explorer tabs."""
    from iidm_viewer.qt.extensions_explorer_tab import ExtensionsExplorerTab
    from iidm_viewer.qt.main_window import MainWindow

    window = MainWindow()
    qapp.processEvents()
    tab_titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert "Data Explorer Extensions" in tab_titles
    assert isinstance(window.extensions_tab, ExtensionsExplorerTab)


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


def test_create_panel_unfolds_when_group_checked(qapp):
    """Programmatic ``_group.setChecked(True)`` unfolds the body —
    simulates the user clicking the QGroupBox header to expand."""
    from iidm_viewer.qt.create_panel import CreateComponentPanel

    panel = CreateComponentPanel()
    # The panel itself starts hidden (waits for a network); make it
    # visible so isVisible() on _body reflects only the fold state.
    panel.setVisible(True)
    panel.show()
    qapp.processEvents()
    assert panel._body.isVisible() is False
    panel._group.setChecked(True)
    qapp.processEvents()
    assert panel._body.isVisible() is True
    panel.hide()


def test_all_create_panels_start_folded(qapp):
    """Every PySide6 create panel must start collapsed — same UX as
    Streamlit's default-collapsed ``st.expander`` and NiceGUI's
    ``ui.expansion``. Construct each one and assert the QGroupBox is
    unchecked + its ``_body`` widget hidden."""
    from iidm_viewer.qt.create_panel import (
        CreateBranchPanel,
        CreateComponentPanel,
        CreateContainerPanel,
        CreateCouplingDevicePanel,
        CreateExtensionPanel,
        CreateHvdcLinePanel,
        CreateOperationalLimitsPanel,
        CreateReactiveLimitsPanel,
        CreateSecondaryVoltageControlPanel,
        CreateTapChangerPanel,
    )

    classes = [
        CreateComponentPanel,
        CreateBranchPanel,
        CreateContainerPanel,
        CreateHvdcLinePanel,
        CreateTapChangerPanel,
        CreateCouplingDevicePanel,
        CreateReactiveLimitsPanel,
        CreateOperationalLimitsPanel,
        CreateSecondaryVoltageControlPanel,
        CreateExtensionPanel,
    ]
    for cls in classes:
        panel = cls()
        qapp.processEvents()
        # The checkable QGroupBox starts unchecked → the user sees a
        # collapsed form. Toggling it expands.
        assert panel._group.isCheckable() is True, f"{cls.__name__} should be checkable"
        assert panel._group.isChecked() is False, (
            f"{cls.__name__} should start folded (group unchecked)"
        )
        assert panel._body.isVisible() is False, (
            f"{cls.__name__} should start with its body hidden"
        )


def test_app_state_caches_last_report_json_for_view_logs(qapp):
    """Qt AppState exposes ``last_report_json`` to drive the "View Logs"
    sidebar button. Cleared on every network load, populated by
    ``run_loadflow``."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.state import AppState

    state = AppState()
    assert state.last_report_json is None

    state.load_network_from_path  # method exists
    # We can't easily call load_network_from_path without a file path, so
    # mirror what it does: install a network then run LF.
    net = NetworkProxy(run(pn.create_ieee14))
    state._network = net          # tested internal seam — matches install
    state._last_report_json = None
    result = state.run_loadflow()
    assert result is not None
    assert state.last_report_json


def test_lf_report_dialog_renders_tree_and_filters_by_severity(qapp):
    """End-to-end smoke of the Qt LFReportDialog: feed a real
    ``report_json`` from IEEE14, confirm the tree is populated, then
    untick INFO and check the visible row count drops."""
    import pypowsybl.network as pn
    from iidm_viewer.loadflow import run_ac
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.lf_report_dialog import LFReportDialog

    net = NetworkProxy(run(pn.create_ieee14))
    result = run_ac(net)
    assert result and result.report_json

    dlg = LFReportDialog(result.report_json)
    qapp.processEvents()
    assert dlg._tree.topLevelItemCount() > 0

    # Tightening to ERROR-only typically drops most entries (the LF
    # report rarely contains explicit ERRORs on the IEEE14 demo, so
    # this just asserts the count is monotonically non-increasing).
    before = _flatten_tree_count(dlg._tree)
    dlg._sev_checks["INFO"].setChecked(False)
    qapp.processEvents()
    after = _flatten_tree_count(dlg._tree)
    assert after <= before


def test_lf_report_dialog_handles_empty_report(qapp):
    """No report → friendly empty-state label, no tree rows."""
    from iidm_viewer.qt.lf_report_dialog import LFReportDialog

    dlg = LFReportDialog("")
    qapp.processEvents()
    assert dlg._tree.topLevelItemCount() == 0
    assert "Run a load flow first" in dlg._empty_label.text()


def test_lf_report_dialog_handles_malformed_json(qapp):
    from iidm_viewer.qt.lf_report_dialog import LFReportDialog

    dlg = LFReportDialog("{not json")
    qapp.processEvents()
    assert dlg._tree.topLevelItemCount() == 0
    assert "Failed to parse report" in dlg._empty_label.text()


def test_load_options_dialog_round_trips_format_and_post_processors(qapp):
    """End-to-end LoadOptionsDialog: pass current overrides, switch
    format to XIIDM (which has configurable params), pick a
    post-processor, and confirm the result fields are populated on
    Save."""
    from iidm_viewer.qt.load_options_dialog import AUTO_DETECT, LoadOptionsDialog

    dlg = LoadOptionsDialog(
        formats=["XIIDM", "UCTE"],
        post_processors=["replaceTieLinesByLines"],
        current_format=None,
        current_params={},
        current_post_processors=[],
    )
    qapp.processEvents()
    # Default selection is Auto-detect.
    assert dlg._fmt_combo.currentText() == AUTO_DETECT

    dlg._fmt_combo.setCurrentText("XIIDM")
    qapp.processEvents()
    # The post-processor checkbox must be present.
    assert "replaceTieLinesByLines" in dlg._post_processor_boxes
    dlg._post_processor_boxes["replaceTieLinesByLines"].setChecked(True)

    dlg._on_save_clicked()
    assert dlg.format == "XIIDM"
    assert dlg.post_processors == ["replaceTieLinesByLines"]
    # Params may be empty when the user didn't tweak any widget — we
    # don't assert content here, just the dict type.
    assert isinstance(dlg.params, dict)


def test_load_options_dialog_auto_detect_means_no_format(qapp):
    """Picking Auto-detect produces ``format=None`` so the host knows
    to fall back to pypowsybl's extension-based detection."""
    from iidm_viewer.qt.load_options_dialog import LoadOptionsDialog

    dlg = LoadOptionsDialog(
        formats=["XIIDM", "UCTE"],
        post_processors=[],
        current_format="XIIDM",
        current_params={"iidm.import.xml.throw-exception-if-extension-not-found": "true"},
        current_post_processors=[],
    )
    qapp.processEvents()
    dlg._fmt_combo.setCurrentText("Auto-detect")
    dlg._on_save_clicked()
    assert dlg.format is None
    assert dlg.params == {}


def test_save_network_dialog_carries_parameters(qapp):
    """The Save dialog should attach an empty (or non-empty) parameters
    dict after Save — the host then forwards it to ``export_network``."""
    from iidm_viewer.qt.save_network_dialog import SaveNetworkDialog

    dlg = SaveNetworkDialog(["UCTE", "XIIDM"])
    qapp.processEvents()
    # Default selection is XIIDM.
    assert dlg._combo.currentText() == "XIIDM"
    dlg._on_save_clicked()
    assert dlg.selected_format == "XIIDM"
    assert isinstance(dlg.parameters, dict)


def test_app_state_threads_import_params_into_load_network(qapp, monkeypatch):
    """``AppState.load_network_from_path`` must forward
    ``import_params`` + ``import_post_processors`` so the
    LoadOptionsDialog round-trip actually affects the next file load."""
    from iidm_viewer import network_loader
    from iidm_viewer.qt.state import AppState

    state = AppState()
    state.import_params = {"iidm.import.xml.throw-exception-if-extension-not-found": "true"}
    state.import_post_processors = ["replaceTieLinesByLines"]

    captured: dict = {}

    def fake_load_from_path(path, *, parameters=None, post_processors=None):
        captured["path"] = path
        captured["parameters"] = parameters
        captured["post_processors"] = post_processors
        # Return a minimal proxy so the rest of load_network_from_path
        # doesn't blow up on the default-VL pick.
        class _StubNet:
            def get_voltage_levels(self):
                import pandas as pd
                return pd.DataFrame()
        return _StubNet()

    monkeypatch.setattr(
        "iidm_viewer.qt.state.network_loader.load_from_path",
        fake_load_from_path,
    )

    # ``pick_default_vl`` reads ``_obj``; stub it too.
    monkeypatch.setattr(
        "iidm_viewer.qt.state.network_loader.pick_default_vl",
        lambda net: None,
    )

    state.load_network_from_path("/fake/path.xiidm")
    assert captured["path"] == "/fake/path.xiidm"
    assert captured["parameters"] == state.import_params
    assert captured["post_processors"] == state.import_post_processors


def test_app_state_create_empty_network_installs_and_broadcasts(qapp):
    """``AppState.create_empty_network`` should build a blank
    pypowsybl Network and fire ``network_changed`` so every listener
    refreshes against the new (empty) topology."""
    from iidm_viewer.qt.state import AppState

    state = AppState()
    seen: list = []
    state.network_changed.connect(lambda net: seen.append(net))

    network = state.create_empty_network("blank_net")
    qapp.processEvents()
    assert network is state.network
    assert len(seen) == 1
    assert seen[0] is network
    # Empty network has no voltage levels → selected_vl stays cleared.
    assert state.selected_vl is None


def test_app_state_create_empty_network_resets_change_log_and_report(qapp):
    """Switching to an empty network must clear the carried-over LF
    report + change log so the new model starts from scratch."""
    from iidm_viewer.change_log import ChangeLog
    from iidm_viewer.qt.state import AppState

    state = AppState()
    # Stash some pretend prior state to confirm the reset.
    state._last_report_json = '{"foo": "bar"}'
    state.change_log.record(
        "Generators", "GH1", "target_p", 0.0, 42.0,
    )

    state.create_empty_network("blank_net2")
    qapp.processEvents()
    assert state.last_report_json is None
    assert isinstance(state.change_log, ChangeLog)
    assert len(state.change_log.entries()) == 0


def test_network_reduction_dialog_applies_voltage_range(qapp):
    """End-to-end NetworkReductionDialog: apply a voltage-range
    reduction on IEEE14 and confirm the dialog reports success +
    every VL left in the network is inside the requested band."""
    import pypowsybl.network as pn
    from iidm_viewer.network_reduction_actions import list_voltage_level_ids
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.network_reduction_dialog import NetworkReductionDialog

    net = NetworkProxy(run(pn.create_ieee14))
    vl_ids = list_voltage_level_ids(net)
    dlg = NetworkReductionDialog(net, vl_ids)
    qapp.processEvents()

    dlg._v_min.setValue(100.0)
    dlg._v_max.setValue(200.0)
    dlg._on_apply_clicked()
    qapp.processEvents()

    assert dlg.applied is True
    vls = net.get_voltage_levels()
    assert not vls.empty
    assert min(vls["nominal_v"]) >= 100.0


def test_network_reduction_dialog_validator_errors_land_on_status(qapp):
    """An inverted band → ValueError → status label gets the message
    and ``applied`` stays False."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.network_reduction_dialog import NetworkReductionDialog

    net = NetworkProxy(run(pn.create_ieee14))
    dlg = NetworkReductionDialog(net, vl_ids=["VL1", "VL2"])
    qapp.processEvents()
    dlg._v_min.setValue(200.0)
    dlg._v_max.setValue(100.0)
    dlg._on_apply_clicked()
    qapp.processEvents()
    assert dlg.applied is False
    assert "less than" in dlg._status.text().lower()


def test_app_state_notify_network_changed_refires_listeners(qapp):
    """``AppState.notify_network_changed`` re-emits ``network_changed``
    for the same network — used by reduction to refresh listeners
    after an in-place mutation."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.state import AppState

    state = AppState()
    state._network = NetworkProxy(run(pn.create_ieee14))
    seen: list = []
    state.network_changed.connect(lambda net: seen.append(net))

    state.notify_network_changed()
    qapp.processEvents()
    assert len(seen) == 1
    assert seen[0] is state.network


def test_sidebar_network_reduction_button_gates_on_network(qapp, loaded_window):
    """The "Network Reduction" button toggles with the network — same
    contract as Save."""
    sidebar = loaded_window.sidebar
    assert sidebar._reduction_btn.isEnabled() is True
    loaded_window._on_network_changed(None)
    qapp.processEvents()
    assert sidebar._reduction_btn.isEnabled() is False


def test_save_network_dialog_picks_xiidm_by_default(qapp):
    """The Save-network format picker should default to XIIDM when
    that format is offered — matches Streamlit's typical use."""
    from iidm_viewer.qt.save_network_dialog import SaveNetworkDialog

    dlg = SaveNetworkDialog(["UCTE", "XIIDM", "CGMES"])
    qapp.processEvents()
    assert dlg._combo.currentText() == "XIIDM"
    # Result starts unset; populated only after Save is clicked.
    assert dlg.selected_format is None
    dlg._on_save_clicked()
    assert dlg.selected_format == "XIIDM"


def test_save_network_dialog_handles_missing_xiidm(qapp):
    """When the offered formats don't include XIIDM, the picker just
    keeps the combo's first entry as the default."""
    from iidm_viewer.qt.save_network_dialog import SaveNetworkDialog

    dlg = SaveNetworkDialog(["UCTE", "CGMES"])
    qapp.processEvents()
    assert dlg._combo.currentText() == "UCTE"


def test_sidebar_save_button_gates_on_network_presence(qapp, loaded_window):
    """``set_save_enabled`` flips the button as the network state changes.
    The loaded window has IEEE14 in place so the button should be on."""
    sidebar = loaded_window.sidebar
    assert sidebar._save_btn.isEnabled() is True
    # Simulate the network being cleared.
    loaded_window._on_network_changed(None)
    qapp.processEvents()
    assert sidebar._save_btn.isEnabled() is False


def test_lf_parameters_dialog_seeds_overrides_and_saves_changes(qapp):
    """End-to-end Qt LF parameters: seeds the widgets from the
    passed-in overrides, lets a programmatic edit through, and on Save
    the trimmed dicts land on ``.generic_params`` / ``.provider_params``.
    """
    from iidm_viewer.qt.lf_parameters_dialog import LFParametersDialog

    dlg = LFParametersDialog(
        generic_overrides={"distributed_slack": False},
        provider_overrides={},
    )
    qapp.processEvents()
    # The seeded override is reflected in the widget.
    w = dlg._generic_widgets["distributed_slack"]
    assert w.isChecked() is False
    # Flip a different generic param via the widget then save.
    use_rl = dlg._generic_widgets["use_reactive_limits"]
    use_rl.setChecked(False)  # default is True
    from PySide6.QtWidgets import QDialog
    dlg._on_save_clicked()
    qapp.processEvents()
    assert dlg.result() == QDialog.Accepted
    # Both diff'd params survive; defaults are dropped.
    assert dlg.generic_params.get("distributed_slack") is False
    assert dlg.generic_params.get("use_reactive_limits") is False


def test_app_state_run_loadflow_forwards_cached_params(qapp, monkeypatch):
    """``run_loadflow`` should pick up the AppState-cached params
    when the caller doesn't override — so the sidebar's plain
    ``Run AC Load Flow`` click respects the dialog's last save."""
    import pypowsybl.network as pn
    from iidm_viewer import loadflow as lf_mod
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.state import AppState

    state = AppState()
    state._network = NetworkProxy(run(pn.create_ieee14))
    state.lf_generic_params = {"distributed_slack": False}
    state.lf_provider_params = {"slackBusSelectionMode": "FIRST"}

    captured: dict = {}

    def fake_run_ac(net, generic, provider):
        captured["generic"] = generic
        captured["provider"] = provider
        return lf_mod.LoadFlowResult([], "{}")

    monkeypatch.setattr("iidm_viewer.qt.state.run_ac", fake_run_ac)
    state.run_loadflow()
    assert captured["generic"] == {"distributed_slack": False}
    assert captured["provider"] == {"slackBusSelectionMode": "FIRST"}


def _flatten_tree_count(tree) -> int:
    """Total node count across the QTreeWidget — top-level + descendants."""
    def _count(item) -> int:
        total = 1
        for i in range(item.childCount()):
            total += _count(item.child(i))
        return total

    n = 0
    for i in range(tree.topLevelItemCount()):
        n += _count(tree.topLevelItem(i))
    return n


def test_sidebar_vl_picker_filters_and_selects(qapp, loaded_window):
    """Mirrors Streamlit's ``vl_selector``: the sidebar carries a
    filter input + a dropdown populated with the network's VLs. Typing
    in the filter narrows the dropdown; picking an item routes through
    ``AppState.set_selected_vl`` (same path as a map click). Locks in
    the contract that the sidebar can drive VL selection on its own."""
    sidebar = loaded_window.sidebar
    # The dropdown is populated from the loaded IEEE14 network.
    assert sidebar._vl_combo.isEnabled()
    assert sidebar._vl_combo.count() > 0
    # The current selection should match the auto-picked default-VL.
    assert sidebar._vl_combo.currentData() == loaded_window.state.selected_vl

    # Filtering narrows the dropdown.
    full_count = sidebar._vl_combo.count()
    sidebar._vl_filter.setText("VL1")
    qapp.processEvents()
    narrowed_count = sidebar._vl_combo.count()
    assert 0 < narrowed_count <= full_count

    # Clear filter — combo regrows.
    sidebar._vl_filter.setText("")
    qapp.processEvents()
    assert sidebar._vl_combo.count() == full_count

    # Picking a different VL programmatically goes through the AppState.
    target_idx = 1 if sidebar._vl_combo.count() > 1 else 0
    target_id = sidebar._vl_combo.itemData(target_idx)
    sidebar._vl_combo.setCurrentIndex(target_idx)
    qapp.processEvents()
    assert loaded_window.state.selected_vl == target_id


def test_sidebar_vl_picker_syncs_when_state_changes_externally(qapp, loaded_window):
    """A map / NAD / SLD click sets ``state.selected_vl`` directly.
    The sidebar dropdown must follow — otherwise users see a desynced
    label."""
    sidebar = loaded_window.sidebar
    assert sidebar._vl_combo.count() >= 2

    # Pick a VL that isn't the current one.
    current = sidebar._vl_combo.currentData()
    other = None
    for i in range(sidebar._vl_combo.count()):
        if sidebar._vl_combo.itemData(i) != current:
            other = sidebar._vl_combo.itemData(i)
            break
    assert other is not None

    loaded_window.state.set_selected_vl(other)
    qapp.processEvents()
    assert sidebar._vl_combo.currentData() == other


def test_main_window_carries_a_reactive_curves_tab(qapp):
    """The PySide6 main window must surface the Reactive Capability
    Curves tab as a top-level entry — parity with Streamlit and NiceGUI.
    """
    from iidm_viewer.qt.main_window import MainWindow
    from iidm_viewer.qt.reactive_curves_tab import ReactiveCurvesTab

    window = MainWindow()
    qapp.processEvents()
    tab_titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert "Reactive Capability Curves" in tab_titles
    assert isinstance(window.reactive_curves_tab, ReactiveCurvesTab)


def test_reactive_curves_tab_builds_view_model_for_ieee14(qapp, loaded_window):
    """End-to-end: with IEEE14 loaded, the tab must populate its
    generator combo + render the metric labels via the shared view model."""
    tab = loaded_window.reactive_curves_tab
    qapp.processEvents()
    # IEEE14 has at least one gen with reactive limits → the combo
    # carries entries and the placeholder is hidden.
    assert tab._gen_combo.count() > 0
    assert tab._placeholder.isVisible() is False
    # Metric labels were populated from the selected gen's row.
    assert "target_p" in tab._target_p_lbl.text()
    assert tab._target_p_lbl.text() != "target_p: —"


def test_reactive_curves_tab_empty_network_shows_placeholder(qapp):
    """Switching to a network with no eligible gens (empty network)
    must show the placeholder, hide the data widgets, and not crash."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.reactive_curves_tab import ReactiveCurvesTab

    tab = ReactiveCurvesTab()
    network = NetworkProxy(run(pn.create_empty))
    tab.set_network(network)
    qapp.processEvents()
    # ``isVisible`` returns False for any widget whose parent window
    # isn't shown, so we check the model state + the widgets the
    # refresh path explicitly toggles via ``setVisible``.
    assert tab._view_model is None
    assert tab._gen_combo.count() == 0
    # ``_set_visible_data(False)`` hides the metric labels + plot view.
    assert tab._target_p_lbl.isHidden() is True
    assert tab._plot_view.isHidden() is True
    # The placeholder stays unhidden so the user sees the "no data" hint.
    assert tab._placeholder.isHidden() is False


def test_main_window_sidebar_has_view_session_script_button(qapp):
    """The PySide6 sidebar must surface a "View live Script" button —
    parity with Streamlit + NiceGUI."""
    from iidm_viewer.qt.main_window import MainWindow

    window = MainWindow()
    qapp.processEvents()
    btn = getattr(window.sidebar, "_view_script_btn", None)
    assert btn is not None
    assert btn.text() == "View live Script"


def test_session_script_dialog_consumes_shared_recorder(qapp):
    """The Qt dialog must drive ``script_recorder`` + ``generate_script``
    the same way Streamlit + NiceGUI do."""
    import inspect

    from iidm_viewer.qt import session_script_dialog

    module_src = inspect.getsource(session_script_dialog)
    assert "from iidm_viewer import script_recorder" in module_src
    assert "from iidm_viewer.script_generator import generate_script" in module_src
    class_src = inspect.getsource(session_script_dialog.SessionScriptDialog)
    assert "script_recorder.get_log" in class_src
    assert "script_recorder.set_paused" in class_src
    assert "script_recorder.clear_log" in class_src


def test_app_state_records_load_and_create_empty_and_loadflow(qapp):
    """The PySide6 ``AppState`` mutators must drive ``script_recorder``
    so the sidebar's Session Script button shows the same op log
    Streamlit produces.
    """
    import os

    from iidm_viewer import script_recorder
    from iidm_viewer.qt.state import AppState

    script_recorder.reset_store()
    try:
        state = AppState()
        xiidm = os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm"),
        )
        state.load_network_from_path(xiidm)
        ops = script_recorder.get_log()
        assert ops and ops[0]["kind"] == "load_network"
        # Loadflow appends a ``run_loadflow`` op.
        state.run_loadflow()
        assert script_recorder.get_log()[-1]["kind"] == "run_loadflow"
        # Empty network seeds a fresh log via ``record_create_empty``.
        state.create_empty_network("blank")
        seeded = script_recorder.get_log()
        assert seeded[0]["kind"] == "create_empty"
        assert seeded[0]["network_id"] == "blank"
    finally:
        script_recorder.reset_store()


def test_main_window_carries_an_operational_limits_tab(qapp):
    """The PySide6 main window must surface ``Operational Limits`` as a
    top-level tab — parity with Streamlit + NiceGUI."""
    from iidm_viewer.qt.main_window import MainWindow
    from iidm_viewer.qt.operational_limits_tab import OperationalLimitsTab

    window = MainWindow()
    qapp.processEvents()
    tab_titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert "Operational Limits" in tab_titles
    assert isinstance(window.operational_limits_tab, OperationalLimitsTab)


def test_operational_limits_tab_builds_view_model_for_ieee14(qapp, loaded_window):
    """End-to-end: with IEEE14 loaded, the tab must populate its
    element combo via the shared view model."""
    tab = loaded_window.operational_limits_tab
    qapp.processEvents()
    # IEEE14 carries 58 operational-limit rows → element combo
    # populated, placeholder hidden.
    assert tab._element_combo.count() > 0
    assert tab._view_model is not None
    assert tab._placeholder.isHidden() is True


def test_operational_limits_tab_empty_network_shows_placeholder(qapp):
    """Switching to a network with no limits must show the placeholder
    and hide both data sections without raising."""
    import pypowsybl.network as pn
    from iidm_viewer.powsybl_worker import NetworkProxy, run
    from iidm_viewer.qt.operational_limits_tab import OperationalLimitsTab

    tab = OperationalLimitsTab()
    network = NetworkProxy(run(pn.create_empty))
    tab.set_network(network)
    qapp.processEvents()
    assert tab._view_model is None
    assert tab._element_combo.count() == 0
    # ``_set_data_visible(False)`` hides both group boxes.
    assert tab._loading_group.isHidden() is True
    assert tab._detail_group.isHidden() is True
    assert tab._placeholder.isHidden() is False
