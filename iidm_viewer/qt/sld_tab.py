"""Single Line Diagram tab — PySide6 host for the existing SLD JS bundle.

Generates the SVG via pypowsybl on the worker thread (per AGENTS.md §1)
and hands it to ``frontend/sld_component/dist`` via
:class:`PowsyblWebView`. Caches the (svg, metadata) pair per VL so
returning to a previously-viewed VL is instant.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from iidm_viewer.powsybl_worker import NetworkProxy, run
from iidm_viewer.qt.web_view import PowsyblWebView


_SLD_DIST = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "frontend", "sld_component", "dist",
)


def _generate_sld(network: NetworkProxy, vl_id: str):
    """Return ``(svg, metadata_json)`` for ``vl_id``. Worker-thread bound."""
    raw = object.__getattribute__(network, "_obj")

    def _do():
        from pypowsybl.network import SldParameters
        params = SldParameters(use_name=True, tooltip_enabled=True)
        sld = raw.get_single_line_diagram(vl_id, parameters=params)
        return sld.svg, sld.metadata

    return run(_do)


class SldTab(QWidget):
    """Renders the SLD for the currently selected voltage level."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._current_vl: Optional[str] = None
        self._cache: dict[str, tuple[str, str]] = {}
        self._ready = False

        self._status = QLabel("Select a substation on the Network Map.")
        self._status.setStyleSheet("padding: 6px 10px; color: #444;")
        self._view = PowsyblWebView(_SLD_DIST, self)
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
        )
