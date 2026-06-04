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
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from iidm_viewer.cache_backend import SLD, CacheBackend, DictBackend
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
        # Container-id → (svg, metadata) lookup, stored in the
        # AppState's cache backend so :func:`cache_backend.invalidate_*`
        # hooks clear it without the tab having to listen for events.
        # Each instance owns a private DictBackend until the MainWindow
        # injects the shared one via :meth:`set_cache_backend` — this
        # keeps headless tests independent.
        self._cache_backend: CacheBackend = DictBackend()
        self._ready = False
        self._show_substation = False

        self._status = QLabel("Select a substation on the Network Map.")
        self._status.setStyleSheet("padding: 6px 10px; color: #444;")
        self._expand_btn = QPushButton("Expand to substation")
        self._expand_btn.setVisible(False)
        self._expand_btn.clicked.connect(self._on_expand_toggle)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self._status)
        header.addWidget(self._expand_btn)
        header.addStretch(1)

        self._view = PowsyblWebView(_SLD_DIST, self)
        self._view.value_received.connect(self._on_value)
        self._view.ready.connect(self._on_ready)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(self._view, 1)

    @property
    def _cache(self) -> dict:
        """Live view of the SLD slot in the cache backend.

        Returned dict is the actual slot storage, so existing call sites
        (and tests) that mutate it via ``_cache[key] = ...`` keep
        working unchanged.
        """
        return self._cache_backend.setdefault(SLD, {})

    def set_cache_backend(self, backend: CacheBackend) -> None:
        """Plug in the shared AppState backend.

        Called once by the MainWindow after construction so the tab
        reads / writes the same :data:`cache_backend.SLD` slot the
        rest of the host invalidates. Has no effect on tests that
        never call it — the private DictBackend created in ``__init__``
        gives the same behaviour the previous ``self._cache`` dict had.
        """
        self._cache_backend = backend

    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        # Pop the SLD slot defensively in case the AppState hasn't
        # already done so (e.g. headless tests with a private backend).
        self._cache_backend.pop(SLD, None)
        self._current_vl = None
        self._show_substation = False
        self._expand_btn.setVisible(False)
        self._status.setText(
            "Select a substation on the Network Map."
            if network is not None
            else "No network loaded."
        )
        # Wipe the previously-rendered SVG. Without this an empty-network
        # swap (or any swap that doesn't pick a default VL) would leave
        # the prior network's diagram on screen — ``_render`` short-
        # circuits on ``_current_vl is None`` so it would never overwrite.
        if self._ready:
            self._view.render_component(
                svg="", metadata="", height=700, svgType="voltage-level",
            )

    def show_voltage_level(self, vl_id: str) -> None:
        if not vl_id or self._network is None:
            return
        self._current_vl = vl_id

        # Resolve substation for expand/collapse affordance.
        sid, multi_vl = self._get_substation_for_vl(vl_id)
        if sid and multi_vl:
            self._expand_btn.setVisible(True)
            self._expand_btn.setText(
                "Collapse to voltage level"
                if self._show_substation
                else "Expand to substation"
            )
        else:
            self._expand_btn.setVisible(False)
            self._show_substation = False

        if self._show_substation and sid:
            container_id = sid
            svg_type = "substation"
        else:
            container_id = vl_id
            svg_type = "voltage-level"

        cache = self._cache_backend.setdefault(SLD, {})
        if container_id in cache:
            svg, metadata = cache[container_id]
        else:
            try:
                svg, metadata = _generate_sld(self._network, container_id)
            except Exception as exc:
                self._status.setText(f"SLD failed for {container_id}: {exc}")
                return
            cache[container_id] = (svg, metadata)
        if self._show_substation and sid:
            self._status.setText(f"Substation: {sid}")
        else:
            self._status.setText(f"Voltage level: {vl_id}")
        self._svg_type = svg_type
        self._render()

    def _get_substation_for_vl(self, vl_id: str):
        """Return ``(substation_id, multi_vl)`` for *vl_id*."""
        if self._network is None:
            return None, False
        try:
            vls = self._network.get_voltage_levels(all_attributes=True)
            if vls.empty or "substation_id" not in vls.columns:
                return None, False
            if vl_id not in vls.index:
                return None, False
            row = vls.loc[vl_id]
            sid = str(row["substation_id"]) if row.get("substation_id") else None
            if sid is None:
                return None, False
            multi = int((vls["substation_id"] == sid).sum()) > 1
            return sid, multi
        except Exception:
            return None, False

    def _on_expand_toggle(self) -> None:
        self._show_substation = not self._show_substation
        if self._current_vl:
            self.show_voltage_level(self._current_vl)

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
        svg_type = getattr(self, "_svg_type", "voltage-level")
        # When showing the substation, the cache key is the substation id.
        sid, _ = self._get_substation_for_vl(self._current_vl)
        cache_key = sid if (self._show_substation and sid) else self._current_vl
        cache = self._cache_backend.setdefault(SLD, {})
        entry = cache.get(cache_key)
        if entry is None:
            return
        svg, metadata = entry
        self._view.render_component(
            svg=svg,
            metadata=metadata,
            height=700,
            svgType=svg_type,
            # PySide6 desktop UX: keep pan/zoom continuous across VL
            # transitions and same-VL re-renders (e.g. after a switch
            # toggle or data edit). See module docstring for why this
            # goes through getViewBox / setViewBox rather than the
            # library's no-op setSvgContent.
            preserveViewport=True,
        )
