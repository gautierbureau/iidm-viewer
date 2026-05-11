"""Shared QWebEngineView host for the Streamlit-protocol JS bundles.

Loads any ``frontend/<component>/dist/index.html`` unchanged and
bridges its postMessage wire-protocol to Python via QWebChannel. See
``bridge.js`` for the JS side of the protocol.

A consumer typically:

    view = PowsyblWebView(component_dir=".../map_component/dist")
    view.value_received.connect(self._on_component_value)
    view.ready.connect(lambda: view.render(substations=..., ...))
"""
from __future__ import annotations

import json
import os
from typing import Any

from PySide6.QtCore import (
    QFile,
    QIODevice,
    QObject,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView


_BRIDGE_JS_PATH = os.path.join(os.path.dirname(__file__), "bridge.js")


def _read_qrc_text(qrc_path: str) -> str:
    f = QFile(qrc_path)
    if not f.open(QIODevice.ReadOnly | QIODevice.Text):
        raise RuntimeError(f"could not open {qrc_path!r}")
    try:
        return bytes(f.readAll()).decode("utf-8")
    finally:
        f.close()


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class _Bridge(QObject):
    """QObject exposed to JS as ``iidm_bridge``.

    JS calls ``onComponentValue(json)`` on every Streamlit-protocol
    ``setComponentValue``; we surface that as a Qt signal carrying the
    parsed dict back to the owning widget.
    """

    value_received = Signal(dict)
    ready = Signal()

    @Slot()
    def onReady(self) -> None:
        self.ready.emit()

    @Slot(str)
    def onComponentValue(self, json_text: str) -> None:
        try:
            value = json.loads(json_text)
        except json.JSONDecodeError:
            return
        if isinstance(value, dict):
            self.value_received.emit(value)


class PowsyblWebView(QWebEngineView):
    """Embeds a built JS component bundle and brokers its events.

    Parameters
    ----------
    component_dir : str
        Absolute path to a ``frontend/<component>/dist`` directory.
        Must contain an ``index.html`` and the corresponding
        ``assets/`` tree.
    """

    value_received = Signal(dict)
    ready = Signal()

    def __init__(self, component_dir: str, parent=None) -> None:
        super().__init__(parent)
        if not os.path.isfile(os.path.join(component_dir, "index.html")):
            raise FileNotFoundError(
                f"no index.html under {component_dir!r}; did you run "
                f"`npm run build` for this component?"
            )
        self._component_dir = component_dir
        self._bridge = _Bridge(self)
        self._bridge.value_received.connect(self.value_received)
        self._bridge.ready.connect(self.ready)
        self._channel = QWebChannel(self)
        self._channel.registerObject("iidm_bridge", self._bridge)

        page = QWebEnginePage(self)
        page.setWebChannel(self._channel)
        # qwebchannel.js is shipped inside the QtWebChannel resource
        # bundle. Concatenate it with the local bridge.js and inject as a
        # single user script at DocumentCreation so ``window.iidmRender``
        # is defined before the bundle's deferred module script runs.
        qwebchannel_js = _read_qrc_text(":/qtwebchannel/qwebchannel.js")
        bridge_js = _read_text(_BRIDGE_JS_PATH)
        script = QWebEngineScript()
        script.setName("iidm-bridge")
        script.setSourceCode(qwebchannel_js + "\n" + bridge_js)
        script.setInjectionPoint(QWebEngineScript.DocumentCreation)
        script.setRunsOnSubFrames(False)
        script.setWorldId(QWebEngineScript.MainWorld)
        page.scripts().insert(script)
        self.setPage(page)

        index_url = QUrl.fromLocalFile(os.path.join(component_dir, "index.html"))
        self.setUrl(index_url)

    def render_component(self, **args: Any) -> None:
        """Dispatch a render to the underlying bundle.

        Equivalent to Streamlit's ``component(**args)`` call. JSON-encodes
        ``args`` once on the Python side; the bundle picks them up via
        its existing ``streamlit:render`` listener.
        """
        try:
            payload = json.dumps(args)
        except (TypeError, ValueError):
            payload = json.dumps({})
        # The bundle's listener is sync and idempotent; safe to call any
        # number of times after page load. Before load, runJavaScript
        # queues until the page is ready.
        self.page().runJavaScript(f"window.iidmRender({payload});")
