"""Framework-agnostic pypowsybl diagram + map-data helpers.

Single source of truth for the three "generate a viewable artefact"
operations the Streamlit, PySide6 and NiceGUI front-ends all need:

* :func:`generate_sld` — pypowsybl Single Line Diagram, returns
  ``(svg, metadata_json)``.
* :func:`generate_nad` — pypowsybl Network Area Diagram, returns
  ``(svg, metadata_json)``.
* :func:`extract_map_data` — geographical map data lifted via
  ``pypowsybl_jupyter.NetworkMapWidget.extract_map_data``, returns
  ``(substations, positions, lines, line_positions)`` or ``None``.

All three run on the pypowsybl worker thread
(``iidm_viewer.powsybl_worker.run``) so the GraalVM thread-affinity
rule from AGENTS.md §1 holds for every front-end identically.

No streamlit / Qt / NiceGUI imports here — this module is safe to
pull into any UI host without a transitive UI dependency.
"""
from __future__ import annotations

from typing import Optional

from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Single Line Diagram
# ---------------------------------------------------------------------------
def generate_sld(
    network: NetworkProxy,
    container_id: str,
    *,
    use_name: bool = True,
    tooltip_enabled: bool = True,
    variant_id: Optional[str] = None,
) -> tuple[str, str]:
    """Generate an SLD SVG for a voltage-level or substation ``container_id``.

    Returns ``(svg, metadata_json)``. The whole call (variant switch
    if any + param construction + pypowsybl call + result attribute
    access) runs in a single worker hop.

    ``variant_id`` (kw-only): when set to a non-InitialState variant,
    the diagram reflects that variant's connection / flow state. The
    switch + render + restore is atomic; the working variant is
    always restored before the function returns.
    """
    from iidm_viewer.variants import with_variant

    raw = object.__getattribute__(network, "_obj")

    def _do():
        from pypowsybl.network import SldParameters
        params = SldParameters(
            use_name=use_name,
            tooltip_enabled=tooltip_enabled,
        )
        with with_variant(raw, variant_id):
            sld = raw.get_single_line_diagram(container_id, parameters=params)
            return sld.svg, sld.metadata

    return run(_do)


# ---------------------------------------------------------------------------
# Network Area Diagram
# ---------------------------------------------------------------------------
def generate_nad(
    network: NetworkProxy,
    vl_id: str,
    depth: int = 1,
    *,
    edge_name_displayed: bool = True,
    power_value_precision: int = 1,
) -> tuple[str, str]:
    """Generate a NAD SVG centered on ``vl_id`` expanded ``depth`` hops."""
    raw = object.__getattribute__(network, "_obj")

    def _do():
        from pypowsybl.network import NadParameters
        params = NadParameters(
            edge_name_displayed=edge_name_displayed,
            power_value_precision=power_value_precision,
        )
        nad = raw.get_network_area_diagram(
            voltage_level_ids=[vl_id],
            depth=int(depth),
            nad_parameters=params,
        )
        return nad.svg, nad.metadata

    return run(_do)


# ---------------------------------------------------------------------------
# Geographical map data
# ---------------------------------------------------------------------------
def extract_map_data(network: NetworkProxy):
    """Lift map data (substations, lines, geometry) via pypowsybl-jupyter.

    Returns ``(substations, substation_positions, lines, line_positions)``
    where each entry is a list of plain JSON-able dicts (the
    pypowsybl-jupyter widget extractor's own shape), or ``None`` if the
    network carries no substation positions.

    Includes tie lines and HVDC lines in the ``lines`` array to match
    the pypowsybl widget's default.
    """
    raw = object.__getattribute__(network, "_obj")

    def _do():
        from pypowsybl_jupyter.networkmapwidget import NetworkMapWidget

        # extract_map_data only uses ``self`` for stateless helpers, so
        # a throwaway subclass that skips the widget __init__ is fine.
        class _Extractor(NetworkMapWidget):
            def __init__(self):  # skip widget init
                pass

            def __del__(self):   # suppress ipywidgets cleanup noise
                pass

        (
            lmap, lpos, smap, spos,
            _vl_subs, _sub_vls, _subs_ids,
            tlmap, hlmap,
        ) = _Extractor().extract_map_data(
            raw, display_lines=True, use_line_geodata=False
        )
        if not spos:
            return None
        return smap, spos, lmap + tlmap + hlmap, lpos

    return run(_do)
