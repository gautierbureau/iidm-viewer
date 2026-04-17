"""Unit tests for the bidirectional NAD custom component wrapper."""
import importlib
import os
from unittest import mock


def test_component_path_points_to_shipped_index_html():
    import iidm_viewer.nad_component as m

    assert os.path.isdir(m._COMPONENT_DIR)
    assert os.path.isfile(os.path.join(m._COMPONENT_DIR, "index.html"))


def test_declare_component_registered_with_expected_name_and_path():
    with mock.patch(
        "streamlit.components.v1.declare_component"
    ) as declare:
        import iidm_viewer.nad_component as m
        importlib.reload(m)

    assert declare.call_count == 1
    args, kwargs = declare.call_args
    assert args[0] == "iidm_nad"
    path = kwargs.get("path") or (args[1] if len(args) > 1 else None)
    assert path is not None
    assert os.path.isfile(os.path.join(path, "index.html"))


def test_render_interactive_nad_forwards_args_and_returns_component_value():
    import iidm_viewer.nad_component as m
    importlib.reload(m)  # undo any prior patching

    with mock.patch.object(m, "_component") as comp:
        comp.return_value = {"type": "nad-vl-click", "vl": "VL1", "ts": 1}
        out = m.render_interactive_nad(
            "<svg/>", '{"nodes":[]}', height=500, key="k"
        )
    assert out == {"type": "nad-vl-click", "vl": "VL1", "ts": 1}
    kwargs = comp.call_args.kwargs
    assert kwargs["svg"] == "<svg/>"
    assert kwargs["metadata"] == '{"nodes":[]}'
    assert kwargs["height"] == 500
    assert kwargs["key"] == "k"
    assert kwargs["default"] is None


def test_index_html_exposes_vl_and_edge_click_payload_types():
    """Sanity-check the frontend contract we rely on from Python."""
    import iidm_viewer.nad_component as m
    path = os.path.join(m._COMPONENT_DIR, "index.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    assert "streamlit:componentReady" in html
    assert "streamlit:render" in html
    assert "streamlit:setComponentValue" in html
    assert "streamlit:setFrameHeight" in html
    assert "nad-vl-click" in html
    assert "nad-edge-click" in html
    # The click handlers must target the real pypowsybl NAD SVG classes.
    assert ".nad-vl-nodes > g" in html
    assert ".nad-branch-edges > g" in html
