"""Unit tests for the interactive geographical map custom component."""
import importlib
import os
from unittest import mock


def test_component_path_points_to_built_dist_with_index_html():
    import iidm_viewer.map_component as m

    assert os.path.isdir(m._COMPONENT_DIR), (
        f"{m._COMPONENT_DIR} not found — run "
        "`npm install && npm run build` in iidm_viewer/frontend/map_component/."
    )
    assert os.path.isfile(os.path.join(m._COMPONENT_DIR, "index.html"))


def test_built_index_html_loads_bundled_asset():
    """The Vite-built index.html must reference the bundled JS under assets/."""
    import iidm_viewer.map_component as m
    index = os.path.join(m._COMPONENT_DIR, "index.html")
    with open(index, "r", encoding="utf-8") as f:
        html = f.read()
    assert 'type="module"' in html
    assert "assets/map-component.js" in html
    # Container div the TS entry point looks up by id.
    assert 'id="map"' in html


def test_declare_component_registered_with_expected_name_and_path():
    with mock.patch(
        "streamlit.components.v1.declare_component"
    ) as declare:
        import iidm_viewer.map_component as m
        importlib.reload(m)

    assert declare.call_count == 1
    args, kwargs = declare.call_args
    assert args[0] == "iidm_map"
    path = kwargs.get("path") or (args[1] if len(args) > 1 else None)
    assert path is not None
    assert path.endswith(os.path.join("frontend", "map_component", "dist"))
    assert os.path.isfile(os.path.join(path, "index.html"))


def test_render_interactive_map_forwards_args():
    import iidm_viewer.map_component as m
    importlib.reload(m)  # undo any prior patching

    subs = [{"id": "S1", "name": "S1", "voltageLevels": []}]
    pos = [{"id": "S1", "coordinate": {"lon": 2.0, "lat": 48.0}}]
    lines = [{"id": "L1", "voltageLevelId1": "VL1", "voltageLevelId2": "VL2"}]

    with mock.patch.object(m, "_component") as comp:
        comp.return_value = None
        out = m.render_interactive_map(subs, pos, lines, height=500, key="k")
    assert out is None
    kwargs = comp.call_args.kwargs
    assert kwargs["substations"] is subs
    assert kwargs["substationPositions"] is pos
    assert kwargs["lines"] is lines
    assert kwargs["height"] == 500
    assert kwargs["key"] == "k"
    assert kwargs["default"] is None


def test_bundle_wires_streamlit_protocol_and_library():
    """Smoke-check that the shipped JS bundle carries the contract strings.

    The Streamlit message names, the OSM tile source URL, and the
    MapLibre attribution constant must survive minification. We also
    check that key `@powsybl/network-map-layers` state tokens
    (`nominalV`, `voltageLevels`) appear in the bundle — they are
    string keys used on the data objects we pass in, so they can't be
    renamed by the bundler.
    """
    import iidm_viewer.map_component as m
    bundle = os.path.join(m._COMPONENT_DIR, "assets", "map-component.js")
    assert os.path.isfile(bundle), (
        f"Bundle missing at {bundle} — rebuild the frontend."
    )
    with open(bundle, "r", encoding="utf-8") as f:
        js = f.read()
    for needle in (
        "streamlit:componentReady",
        "streamlit:render",
        "streamlit:setFrameHeight",
        "tile.openstreetmap.org",
        "OpenStreetMap",
        "nominalV",
        "voltageLevels",
    ):
        assert needle in js, f"missing from bundle: {needle!r}"
