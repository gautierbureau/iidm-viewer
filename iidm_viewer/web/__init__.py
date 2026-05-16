"""NiceGUI desktop/web prototype of iidm-viewer.

A third front-end exploring an alternative to Streamlit and a lighter
alternative to the PySide6 prototype in ``iidm_viewer.qt``. Reuses
the existing ``powsybl_worker`` (thread-affinity rule), the built JS
viewers in ``iidm_viewer/frontend/*/dist/``, and the map-data
extraction in ``network_map``. Launch via the ``iidm-viewer-nicegui``
console script or ``python -m iidm_viewer.web``.

The prototype ships only two tabs — Network Map and Single Line
Diagram — to validate the killer interaction: click a substation on
the map and land on its SLD with no script rerun.
"""
