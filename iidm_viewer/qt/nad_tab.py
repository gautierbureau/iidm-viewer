"""Network Area Diagram tab — PySide6 host for the existing NAD JS bundle.

The NAD is centered on a voltage level and expanded outward by
``depth`` hops. The bundle already emits ``{type:'nad-vl-click', vl}``
when the user clicks a node (no JS rebuild needed). The owning
:class:`MainWindow` routes those clicks to the SLD tab via the
shared :class:`AppState`.

Caches ``(vl, depth) -> (svg, metadata)`` so re-rendering on tab focus
or after returning from the SLD is instant.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from iidm_viewer.cache_backend import NAD, CacheBackend, DictBackend
from iidm_viewer.diagram_services import generate_nad as _generate_nad
from iidm_viewer.powsybl_worker import NetworkProxy
from iidm_viewer.qt.web_view import PowsyblWebView
from iidm_viewer.variants import INITIAL_VARIANT_ID


_NAD_DIST = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "frontend", "nad_component", "dist",
)


class NadTab(QWidget):
    """Renders the NAD centered on the current selected VL.

    Emits :pyattr:`node_clicked` when the user clicks a node in the
    diagram — payload is the clicked node's voltage-level id.
    """

    node_clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._network: Optional[NetworkProxy] = None
        self._current_vl: Optional[str] = None
        self._depth = 1
        # ``(vl, depth) → (svg, metadata)`` lookup, stored in the
        # AppState's shared cache backend (see :meth:`set_cache_backend`).
        # Headless tests get an instance-private DictBackend, identical
        # to the old ``self._cache = {}`` behaviour.
        self._cache_backend: CacheBackend = DictBackend()
        self._ready = False

        self._status = QLabel("Load a network to see the Network Area Diagram.")
        self._status.setStyleSheet("padding: 6px 10px; color: #444;")

        depth_lbl = QLabel("Depth:")
        self._depth_spin = QSpinBox()
        self._depth_spin.setRange(0, 10)
        self._depth_spin.setValue(self._depth)
        self._depth_spin.valueChanged.connect(self._on_depth_changed)

        controls = QHBoxLayout()
        controls.setContentsMargins(10, 4, 10, 4)
        controls.addWidget(depth_lbl)
        controls.addWidget(self._depth_spin)
        controls.addStretch(1)

        self._view = PowsyblWebView(_NAD_DIST, self)
        self._view.value_received.connect(self._on_value)
        self._view.ready.connect(self._on_ready)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._status)
        layout.addLayout(controls)
        layout.addWidget(self._view, 1)

    @property
    def _cache(self) -> dict:
        """Live view of the NAD slot in the cache backend.

        Returned dict is the actual slot storage, so existing call sites
        (and tests) that mutate it via ``_cache[key] = ...`` keep
        working unchanged.
        """
        return self._cache_backend.setdefault(NAD, {})

    def set_cache_backend(self, backend: CacheBackend) -> None:
        """Plug in the shared AppState backend.

        Called once by the MainWindow after construction so the tab
        reads / writes the same :data:`cache_backend.NAD` slot the
        rest of the host invalidates.
        """
        self._cache_backend = backend

    def set_network(self, network: Optional[NetworkProxy]) -> None:
        self._network = network
        # Pop the NAD slot defensively in case the AppState hasn't
        # already done so (e.g. headless tests with a private backend).
        self._cache_backend.pop(NAD, None)
        self._current_vl = None
        self._status.setText(
            "Select a voltage level to display the NAD."
            if network is not None
            else "No network loaded."
        )
        # Wipe the previously-rendered SVG. Without this an empty-network
        # swap (or any swap that doesn't pick a default VL) would leave
        # the prior network's diagram on screen — ``_render`` short-
        # circuits on ``_current_vl is None`` so it would never overwrite.
        if self._ready:
            self._view.render_component(svg="", metadata="", height=700)

    def show_voltage_level(self, vl_id: str) -> None:
        """Re-center the NAD on ``vl_id`` (with the current depth)."""
        if not vl_id or self._network is None:
            return
        self._current_vl = vl_id
        self._ensure_cached()
        self._render()

    def _on_depth_changed(self, depth: int) -> None:
        self._depth = int(depth)
        if self._network is None or self._current_vl is None:
            return
        self._ensure_cached()
        self._render()

    def _on_ready(self) -> None:
        self._ready = True
        self._render()

    def _ensure_cached(self) -> None:
        """Generate (svg, metadata) for the current (vl, depth) if missing.

        Cache population is independent of the WebView readiness so the
        pypowsybl call runs eagerly (its result is needed before we can
        render anything anyway). Rendering itself is gated on
        ``_ready`` in :meth:`_render`.
        """
        if self._network is None or self._current_vl is None:
            return
        # Cache key is ``(vl_id, depth, variant_id)`` so the
        # InitialState and N-K NAD SVGs coexist in the same slot.
        # NAD doesn't have a Streamlit per-tab UI rollout yet — this
        # forward-compats the cache shape for when it does.
        cache = self._cache_backend.setdefault(NAD, {})
        key = (self._current_vl, self._depth, INITIAL_VARIANT_ID)
        if key in cache:
            return
        try:
            cache[key] = _generate_nad(self._network, self._current_vl, self._depth)
        except Exception as exc:
            self._status.setText(f"NAD failed for {self._current_vl}: {exc}")

    def _render(self) -> None:
        if not self._ready or self._current_vl is None:
            return
        cache = self._cache_backend.setdefault(NAD, {})
        entry = cache.get(
            (self._current_vl, self._depth, INITIAL_VARIANT_ID)
        )
        if entry is None:
            return
        svg, metadata = entry
        self._status.setText(
            f"Voltage level: {self._current_vl}  ·  depth {self._depth}  "
            f"(click any node to jump to its Single Line Diagram)"
        )
        self._view.render_component(svg=svg, metadata=metadata, height=700)

    def _on_value(self, value: dict) -> None:
        if value.get("type") == "nad-vl-click":
            vl = value.get("vl")
            if isinstance(vl, str) and vl:
                self.node_clicked.emit(vl)
