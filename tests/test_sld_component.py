"""Unit tests for the interactive SLD custom component wrapper."""
import importlib
import os
from unittest import mock


def test_component_path_points_to_built_dist_with_index_html():
    import iidm_viewer.sld_component as m

    assert os.path.isdir(m._COMPONENT_DIR), (
        f"{m._COMPONENT_DIR} not found — run "
        "`npm install && npm run build` in iidm_viewer/frontend/sld_component/."
    )
    assert os.path.isfile(os.path.join(m._COMPONENT_DIR, "index.html"))


def test_built_index_html_loads_bundled_asset():
    """The Vite-built index.html must reference the bundled JS under assets/."""
    import iidm_viewer.sld_component as m
    index = os.path.join(m._COMPONENT_DIR, "index.html")
    with open(index, "r", encoding="utf-8") as f:
        html = f.read()
    assert 'type="module"' in html
    assert "assets/sld-component.js" in html
    # Container div the TS entry point looks up by id.
    assert 'id="sld"' in html


def test_declare_component_registered_with_expected_name_and_path():
    with mock.patch(
        "streamlit.components.v1.declare_component"
    ) as declare:
        import iidm_viewer.sld_component as m
        importlib.reload(m)

    assert declare.call_count == 1
    args, kwargs = declare.call_args
    assert args[0] == "iidm_sld"
    path = kwargs.get("path") or (args[1] if len(args) > 1 else None)
    assert path is not None
    assert path.endswith(os.path.join("frontend", "sld_component", "dist"))
    assert os.path.isfile(os.path.join(path, "index.html"))


def test_render_interactive_sld_forwards_args_and_returns_component_value():
    import iidm_viewer.sld_component as m
    importlib.reload(m)  # undo any prior patching

    with mock.patch.object(m, "_component") as comp:
        comp.return_value = {"type": "sld-vl-click", "vl": "VL2", "ts": 7}
        out = m.render_interactive_sld(
            "<svg/>", '{"nodes":[]}', height=500, key="k"
        )
    assert out == {"type": "sld-vl-click", "vl": "VL2", "ts": 7}
    kwargs = comp.call_args.kwargs
    assert kwargs["svg"] == "<svg/>"
    assert kwargs["metadata"] == '{"nodes":[]}'
    assert kwargs["height"] == 500
    assert kwargs["key"] == "k"
    assert kwargs["default"] is None


def test_bundle_wires_streamlit_protocol_and_library_callback():
    """Smoke-check that the shipped JS bundle carries the contract strings.

    The Streamlit message names, the `sld-vl-click` payload type, the
    `voltage-level` svgType literal, and the library's
    `onNextVoltageCallback` hook (preserved as a property access even
    under minification) must all survive the bundler. If any go
    missing, Python ↔ JS communication or the navigation-arrow click
    wiring silently breaks.
    """
    import iidm_viewer.sld_component as m
    bundle = os.path.join(m._COMPONENT_DIR, "assets", "sld-component.js")
    assert os.path.isfile(bundle), (
        f"Bundle missing at {bundle} — rebuild the frontend."
    )
    with open(bundle, "r", encoding="utf-8") as f:
        js = f.read()
    for needle in (
        "streamlit:componentReady",
        "streamlit:render",
        "streamlit:setComponentValue",
        "streamlit:setFrameHeight",
        "sld-vl-click",
        "voltage-level",
        "onNextVoltageCallback",
    ):
        assert needle in js, f"missing from bundle: {needle!r}"
