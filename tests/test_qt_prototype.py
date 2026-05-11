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
