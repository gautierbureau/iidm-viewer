"""Exploratory: turn a pypowsybl NAD SVG into a clickable diagram.

Strategy
--------
pypowsybl emits a `NadResult` with two fields: `.svg` (raw SVG string) and
`.metadata` (JSON describing the diagram). The SVG wraps each voltage level
as `<g class="nad-vl-nodes"><g transform="..." id="<svgId>" class="nad-vlXtoY">...</g>...</g>`
and each branch edge as `<g class="nad-branch-edges"><g id="<svgId>">...</g>...</g>`.
The mapping from those integer svg ids to equipment ids lives in the metadata.

`make_interactive_nad_svg` parses the metadata, builds the id→equipment
maps, and injects:

1. a small `<style>` that puts a pointer cursor on VL nodes and edges;
2. a `<script>` that wires click handlers and posts a `nad-vl-click` /
   `nad-edge-click` message via `window.parent.postMessage` on the agreed
   `iidm-viewer` channel.

The Python side does not yet consume these messages. `st.components.v1.html`
is a one-way iframe, so to turn the post into a `st.session_state` update
plus a rerun we need a custom Streamlit component (declare_component) that
can respond with `Streamlit.setComponentValue`. See
`docs/future-interactive-viewer.md` for the plan.
"""
from __future__ import annotations

import json
from typing import Any


def _vl_node_map(metadata: dict[str, Any]) -> dict[str, str]:
    """{svg element id (string) -> VL equipment id}."""
    out: dict[str, str] = {}
    for node in metadata.get("nodes", []):
        svg_id = node.get("svgId")
        vl = node.get("equipmentId")
        if svg_id is not None and vl:
            out[str(svg_id)] = vl
    return out


def _edge_vl_map(metadata: dict[str, Any]) -> dict[str, dict[str, str]]:
    """{edge svg id -> {node1 svgId, node2 svgId, equipmentId}}."""
    out: dict[str, dict[str, str]] = {}
    for edge in metadata.get("edges", []):
        svg_id = edge.get("svgId")
        if svg_id is None:
            continue
        out[str(svg_id)] = {
            "node1": str(edge.get("node1", "")),
            "node2": str(edge.get("node2", "")),
            "equipmentId": edge.get("equipmentId", ""),
        }
    return out


_INJECTION_TEMPLATE = """
<style>
  .nad-vl-nodes > g {{ cursor: pointer; }}
  .nad-vl-nodes > g:hover {{ filter: brightness(1.15); }}
  .nad-branch-edges > g {{ cursor: pointer; }}
</style>
<script>
(function() {{
  var VL_NODES = {vl_nodes_json};
  var EDGES = {edges_json};

  function notify(payload) {{
    try {{
      window.parent.postMessage(Object.assign({{channel: 'iidm-viewer'}}, payload), '*');
    }} catch (e) {{}}
  }}

  function onVlClick(evt) {{
    var g = evt.currentTarget;
    var vl = VL_NODES[g.getAttribute('id')];
    if (!vl) return;
    notify({{type: 'nad-vl-click', vl: vl}});
    evt.stopPropagation();
  }}

  function onEdgeClick(evt) {{
    var g = evt.currentTarget;
    var info = EDGES[g.getAttribute('id')];
    if (!info) return;
    // Walk to the other end: caller may decide which side is "the other".
    notify({{type: 'nad-edge-click', edge: info}});
    evt.stopPropagation();
  }}

  var svg = document.currentScript && document.currentScript.ownerSVGElement;
  var root = svg || document;
  var vlGroups = root.querySelectorAll('.nad-vl-nodes > g');
  vlGroups.forEach(function(g) {{ g.addEventListener('click', onVlClick); }});
  var edgeGroups = root.querySelectorAll('.nad-branch-edges > g');
  edgeGroups.forEach(function(g) {{ g.addEventListener('click', onEdgeClick); }});
}})();
</script>
"""


def make_interactive_nad_svg(nad_result) -> str:
    """Return the NAD SVG with click handlers injected.

    `nad_result` is expected to expose `.svg` (str) and `.metadata` (JSON
    string), matching pypowsybl's `NadResult`.
    """
    metadata = json.loads(nad_result.metadata)
    vl_nodes = _vl_node_map(metadata)
    edges = _edge_vl_map(metadata)

    injection = _INJECTION_TEMPLATE.format(
        vl_nodes_json=json.dumps(vl_nodes),
        edges_json=json.dumps(edges),
    )

    svg = nad_result.svg
    # Insert the <style>+<script> just before the closing </svg>. Placing it
    # inside the SVG keeps the HTML fragment self-contained for st.components.
    close = svg.rfind("</svg>")
    if close == -1:
        return svg + injection
    return svg[:close] + injection + svg[close:]
