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
