"""Single Line Diagram tab — PySide6 host for the existing SLD JS bundle.

Generates the SVG via pypowsybl on the worker thread (per AGENTS.md §1)
and hands it to ``frontend/sld_component/dist`` via
:class:`PowsyblWebView`. Caches the (svg, metadata) pair per VL so
returning to a previously-viewed VL is instant.

Pan/zoom continuity: every render passes ``preserveViewport=True`` so
the bundle's ``main.ts`` captures the current viewer's viewBox before
the unavoidable tear-down inside ``SingleLineDiagramViewer.init`` and
restores it on the new viewer. The Streamlit and NiceGUI hosts leave
the flag at its default ``False`` and keep the library's auto-fit on
every render.

A previous attempt routed the optimisation through
``SingleLineDiagramViewer.setSvgContent`` — but that method is a
one-line property setter in the upstream library, so it can't drive
the optimisation by itself. The viewBox round-trip is the only
contract the library actually exposes for this.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from iidm_viewer.diagram_services import generate_sld as _generate_sld
from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.qt.web_view import PowsyblWebView


_SLD_DIST = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "frontend", "sld_component", "dist",
)


class SldTab(QWidget):
    """Renders the SLD for the currently selected voltage level."""

    # Emitted when the user clicks a feeder's equipment glyph. Payload
    # is the parsed bundle event dict — the MainWindow resolves it to
    # a substation via ``navigation.resolve_feeder_substation`` and
    # focuses the Map tab on that substation.
    feeder_clicked = Signal(dict)

    # Emitted when the user clicks one of the per-feeder "→ next VL"
    # navigation arrows. Payload is the target voltage-level id. The
    # MainWindow routes this through ``AppState.set_selected_vl`` so
    # both diagram tabs follow — same path as a Map / NAD click.
    # Mirrors Streamlit's diagrams.render_sld_tab handler.
    vl_navigation_requested = Signal(str)

    # Emitted when the user clicks a switch / breaker. Payload is the
    # already-decoded pypowsybl switch id + the desired new ``open``
    # value (the JS library animates the symbol before firing, so the
    # value is the *target* state). Mirrors Streamlit's handler in
    # diagrams.render_sld_tab.
    breaker_toggled = Signal(str, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._current_vl: Optional[str] = None
        self._cache: dict[str, tuple[str, str]] = {}
        self._ready = False

        self._status = QLabel("Select a substation on the Network Map.")
        self._status.setStyleSheet("padding: 6px 10px; color: #444;")
        self._view = PowsyblWebView(_SLD_DIST, self)
        self._view.value_received.connect(self._on_value)
        self._view.ready.connect(self._on_ready)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._status)
        layout.addWidget(self._view, 1)

    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        self._cache.clear()
        self._current_vl = None
        self._status.setText(
            "Select a substation on the Network Map."
            if network is not None
            else "No network loaded."
        )

    def show_voltage_level(self, vl_id: str) -> None:
        if not vl_id or self._network is None:
            return
        self._current_vl = vl_id
        if vl_id in self._cache:
            svg, metadata = self._cache[vl_id]
        else:
            try:
                svg, metadata = _generate_sld(self._network, vl_id)
            except Exception as exc:  # pypowsybl can fail on exotic topologies
                self._status.setText(f"SLD failed for {vl_id}: {exc}")
                return
            self._cache[vl_id] = (svg, metadata)
        self._status.setText(f"Voltage level: {vl_id}")
        self._render()

    def _on_ready(self) -> None:
        self._ready = True
        self._render()

    def _on_value(self, value: dict) -> None:
        vtype = value.get("type")
        if vtype == "sld-vl-click":
            # The bundle's onNextVoltageCallback already returns the
            # target VL id; no decoding needed — pypowsybl VL ids never
            # contain the escapable characters the SLG renderer
            # transforms.
            vl = value.get("vl")
            if vl:
                self.vl_navigation_requested.emit(str(vl))
        elif vtype == "sld-breaker-click":
            # The breakerId in the payload is the *SVG-encoded* form
            # (``_45_`` for ``-``, etc.); decode back to the real
            # pypowsybl switch id before emitting so listeners can
            # route it straight to toggle_switch.
            from iidm_viewer.navigation import decode_svg_id
            encoded = str(value.get("breakerId", ""))
            if encoded:
                self.breaker_toggled.emit(
                    decode_svg_id(encoded),
                    bool(value.get("open", False)),
                )
        elif vtype == "sld-feeder-click":
            self.feeder_clicked.emit({
                "equipment_id": value.get("equipmentId"),
                "equipment_type": value.get("equipmentType"),
                "current_vl_id": self._current_vl,
            })

    def _render(self) -> None:
        if not self._ready or self._current_vl is None:
            return
        entry = self._cache.get(self._current_vl)
        if entry is None:
            return
        svg, metadata = entry
        self._view.render_component(
            svg=svg,
            metadata=metadata,
            height=700,
            svgType="voltage-level",
            # PySide6 desktop UX: keep pan/zoom continuous across VL
            # transitions and same-VL re-renders (e.g. after a switch
            # toggle or data edit). See module docstring for why this
            # goes through getViewBox / setViewBox rather than the
            # library's no-op setSvgContent.
            preserveViewport=True,
        )
