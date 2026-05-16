"""PySide6 desktop prototype of iidm-viewer.

A second front-end exploring an alternative to Streamlit. Reuses the
existing ``powsybl_worker`` (thread-affinity rule), the built JS
viewers in ``iidm_viewer/frontend/*/dist/``, and the map-data extraction
in ``network_map``. Launch via the ``iidm-viewer-pyside`` console
script (see ``cli.py``) or ``python -m iidm_viewer.qt``.

The prototype ships only two tabs — Network Map and Single Line
Diagram — to validate the killer interaction: click a substation on
the map and land on its SLD in a single, instant transition.
"""
