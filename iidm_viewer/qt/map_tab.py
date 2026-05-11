"""Network Map tab — PySide6 host for the existing map JS bundle.

Reuses the ``pypowsybl-jupyter`` map-data extraction (the same one the
Streamlit ``network_map`` tab calls) and feeds it to
``frontend/map_component/dist`` via :class:`PowsyblWebView`.

Emits :pyattr:`MapTab.substation_clicked` when the user clicks a
substation on the deck.gl layer — payload is the list of voltage-level
ids attached to that substation, ordered by descending nominal V.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from iidm_viewer.powsybl_worker import NetworkProxy, run
from iidm_viewer.qt.web_view import PowsyblWebView


_MAP_DIST = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "frontend", "map_component", "dist",
)


def _extract_map_data(network: NetworkProxy):
    """Return (substations, positions, lines, line_positions) or None.

    Same extraction path as ``iidm_viewer.network_map._extract_map_data``
    — kept inline here to decouple the Qt prototype from the Streamlit
    module's session-state caching.
    """
    raw = object.__getattribute__(network, "_obj")

    def _extract():
        from pypowsybl_jupyter.networkmapwidget import NetworkMapWidget

        class _Extractor(NetworkMapWidget):
            def __init__(self):  # skip widget init
                pass

            def __del__(self):   # suppress ipywidgets cleanup noise
                pass

        (lmap, lpos, smap, spos, _vl_subs, _sub_vls, _subs_ids, tlmap, hlmap) = (
            _Extractor().extract_map_data(raw, display_lines=True, use_line_geodata=False)
        )
        if not spos:
            return None
        return smap, spos, lmap + tlmap + hlmap, lpos

    return run(_extract)


class MapTab(QWidget):
    """Network map widget. Clicking a substation emits ``substation_clicked``."""

    substation_clicked = Signal(list)  # list[str] vl_ids, ordered by desc nominalV

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._version = 0
        self._ready = False
        self._pending: Optional[dict] = None

        self._view = PowsyblWebView(_MAP_DIST, self)
        self._view.value_received.connect(self._on_value)
        self._view.ready.connect(self._on_ready)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

    def set_network(self, network: Optional[NetworkProxy]) -> None:
        if network is None:
            self._pending = None
            return
        data = _extract_map_data(network)
        if data is None:
            # No geo data — show an empty map. Real prototype would also
            # surface a banner; out of scope for this two-tab demo.
            self._pending = {
                "substations": [],
                "substationPositions": [],
                "lines": [],
                "linePositions": [],
                "version": self._version + 1,
                "height": 670,
            }
        else:
            substations, positions, lines, line_positions = data
            self._pending = {
                "substations": substations,
                "substationPositions": positions,
                "lines": lines,
                "linePositions": line_positions or [],
                "version": self._version + 1,
                "height": 670,
            }
        self._version += 1
        self._flush()

    def _on_ready(self) -> None:
        self._ready = True
        self._flush()

    def _flush(self) -> None:
        if not self._ready or self._pending is None:
            return
        self._view.render_component(**self._pending)
        self._pending = None

    def _on_value(self, value: dict) -> None:
        if value.get("type") == "map-substation-click":
            vl_ids = value.get("vlIds") or []
            if vl_ids:
                self.substation_clicked.emit(list(vl_ids))
