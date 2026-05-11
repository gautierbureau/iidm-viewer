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

from PySide6.QtWidgets import QApplication  # noqa: E402

from iidm_viewer.qt.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_main_window_loads_network_and_clicks_substation(qapp, tmp_path):
    """End-to-end:

      1. Boot MainWindow.
      2. Load test_ieee14.xiidm via the public ``open_file`` API.
      3. Synthesise the same ``substation_clicked`` signal the map JS
         would emit, with the IEEE14 first-substation VL.
      4. Assert: SLD tab is the current tab; AppState.selected_vl is
         set; SldTab cached a non-empty SVG for that VL.
    """
    window = MainWindow()
    window.show()
    qapp.processEvents()

    xiidm = os.path.join(os.path.dirname(__file__), os.pardir, "test_ieee14.xiidm")
    xiidm = os.path.abspath(xiidm)
    assert os.path.exists(xiidm)

    window.open_file(xiidm)
    assert window.state.network is not None

    # Pick an arbitrary real VL id from the IEEE14 fixture (VL1 .. VL14).
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
    assert isinstance(svg, str) and svg.lstrip().startswith("<?xml") or "<svg" in svg
    assert isinstance(metadata, str) and metadata.strip().startswith("{")

    window.close()
    qapp.processEvents()


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
