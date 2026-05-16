"""Framework-agnostic schema + helpers for creating IIDM extensions.

Each entry in :data:`CREATABLE_EXTENSIONS` declares the field shape
pypowsybl's ``create_extensions(name, df)`` expects, plus the per-
component dropdown the host shows to pick a target element.

Shared by the Streamlit ``_render_create_extension_form`` and by the
PySide6 + NiceGUI prototypes' "Attach extension" panels.

What lives here:

* :data:`CREATABLE_EXTENSIONS` — name → schema dict (label, detail,
  index col, target-component map, field list).
* :func:`list_extensions_for_component` — pure registry lookup.
* :func:`list_extension_candidates` — worker-routed target listing.
* :func:`validate_create_extension_fields` — pure validator.
* :func:`create_extension` — worker-routed dispatcher.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from iidm_viewer.powsybl_worker import NetworkProxy, run


# Per-kind coercion. Matches the ``kind`` declarations in the registry
# below — ``choice`` round-trips as ``str``.
_EXT_KIND_COERCIONS: dict[str, type] = {
    "float": float,
    "int": int,
    "bool": bool,
    "str": str,
}


CREATABLE_EXTENSIONS: dict[str, dict] = {
    "substationPosition": {
        "label": "Substation position (lat/long)",
        "detail": "Geographical coordinates used by the map tab.",
        "index": "id",
        "targets": {"Substations": "get_substations"},
        "fields": [
            {"name": "latitude", "kind": "float", "required": True, "default": 0.0,
             "help": "Decimal degrees."},
            {"name": "longitude", "kind": "float", "required": True, "default": 0.0,
             "help": "Decimal degrees."},
        ],
    },
    "entsoeArea": {
        "label": "ENTSO-E area code",
        "detail": "Two-letter ENTSO-E country / area code (e.g. FR, DE).",
        "index": "id",
        "targets": {"Substations": "get_substations"},
        "fields": [
            {"name": "code", "kind": "str", "required": True, "default": "",
             "help": "ENTSO-E area code."},
        ],
    },
    "busbarSectionPosition": {
        "label": "Busbar section position (row / section)",
        "detail": "Drives the SLD layout: which busbar row and section the BBS sits on.",
        "index": "id",
        "targets": {"Busbar Sections": "get_busbar_sections"},
        "fields": [
            {"name": "busbar_index", "kind": "int", "required": True, "default": 1,
             "help": "1-based row index."},
            {"name": "section_index", "kind": "int", "required": True, "default": 1,
             "help": "1-based section index along the row."},
        ],
    },
    "position": {
        "label": "Connectable position (order / feeder name)",
        "detail": "Feeder order / direction used by the SLD; injections only in this phase.",
        "index": "id",
        "targets": {
            "Generators": "get_generators",
            "Loads": "get_loads",
            "Batteries": "get_batteries",
            "Shunt Compensators": "get_shunt_compensators",
            "Static VAR Compensators": "get_static_var_compensators",
            "LCC Converter Stations": "get_lcc_converter_stations",
            "VSC Converter Stations": "get_vsc_converter_stations",
        },
        "fields": [
            {"name": "order", "kind": "int", "required": True, "default": 10,
             "help": "Feeder order on the busbar."},
            {"name": "feeder_name", "kind": "str", "required": False, "default": "",
             "optional_fill": True, "help": "Feeder label (defaults to element id)."},
            {"name": "direction", "kind": "choice", "required": True, "default": "TOP",
             "options": ["TOP", "BOTTOM", "UNDEFINED"],
             "help": "Feeder direction on the SLD."},
            {"name": "side", "kind": "str", "required": False, "default": "",
             "optional_fill": True,
             "help": "Leave blank for injections. For branches use ONE or TWO."},
        ],
    },
    "slackTerminal": {
        "label": "Slack terminal",
        "detail": "Overrides the slack bus chosen by the load flow.",
        "index": "voltage_level_id",
        "targets": {"Voltage Levels": "get_voltage_levels"},
        "fields": [
            {"name": "bus_id", "kind": "str", "required": False, "default": "",
             "optional_fill": True,
             "help": "Bus view bus id. Fill either bus_id OR element_id, not both."},
            {"name": "element_id", "kind": "str", "required": False, "default": "",
             "optional_fill": True,
             "help": "Connectable id (e.g. a generator). Fill either this OR bus_id."},
        ],
    },
    "activePowerControl": {
        "label": "Active power control (participation / droop)",
        "detail": "Controls how the element participates in load-flow balancing.",
        "index": "id",
        "targets": {
            "Generators": "get_generators",
            "Batteries": "get_batteries",
        },
        "fields": [
            {"name": "participate", "kind": "bool", "required": True, "default": True,
             "help": "Whether the unit participates in balancing."},
            {"name": "droop", "kind": "float", "required": True, "default": 4.0,
             "help": "Droop coefficient (% or MW/Hz depending on provider)."},
            {"name": "participation_factor", "kind": "float", "required": False,
             "default": 1.0, "optional_fill": True,
             "help": "Proportional participation weight."},
            {"name": "min_target_p", "kind": "float", "required": False,
             "default": None, "optional_fill": True,
             "help": "Lower bound applied during balancing."},
            {"name": "max_target_p", "kind": "float", "required": False,
             "default": None, "optional_fill": True,
             "help": "Upper bound applied during balancing."},
        ],
    },
    "voltageRegulation": {
        "label": "Voltage regulation (battery)",
        "detail": "Enables voltage regulation on a battery (regulated element can be any bus).",
        "index": "id",
        "targets": {"Batteries": "get_batteries"},
        "fields": [
            {"name": "voltage_regulator_on", "kind": "bool", "required": True,
             "default": True, "help": "Whether regulation is active."},
            {"name": "target_v", "kind": "float", "required": True, "default": 0.0,
             "help": "Target voltage (kV)."},
            {"name": "regulated_element_id", "kind": "str", "required": False,
             "default": "", "optional_fill": True,
             "help": "Regulated terminal element (defaults to self)."},
        ],
    },
    "voltagePerReactivePowerControl": {
        "label": "V/Q slope (SVC)",
        "detail": "Slope (kV/MVar) applied by the SVC voltage regulator.",
        "index": "id",
        "targets": {"Static VAR Compensators": "get_static_var_compensators"},
        "fields": [
            {"name": "slope", "kind": "float", "required": True, "default": 0.01,
             "help": "Slope in kV/MVar."},
        ],
    },
    "standbyAutomaton": {
        "label": "Standby automaton (SVC)",
        "detail": "Standby mode and low/high voltage thresholds + setpoints.",
        "index": "id",
        "targets": {"Static VAR Compensators": "get_static_var_compensators"},
        "fields": [
            {"name": "standby", "kind": "bool", "required": True, "default": False,
             "help": "Whether the SVC is in standby."},
            {"name": "b0", "kind": "float", "required": True, "default": 0.0,
             "help": "Susceptance in standby mode (S)."},
            {"name": "low_voltage_threshold", "kind": "float", "required": True,
             "default": 0.0, "help": "Low voltage threshold (kV)."},
            {"name": "low_voltage_setpoint", "kind": "float", "required": True,
             "default": 0.0, "help": "Low voltage setpoint (kV)."},
            {"name": "high_voltage_threshold", "kind": "float", "required": True,
             "default": 0.0, "help": "High voltage threshold (kV)."},
            {"name": "high_voltage_setpoint", "kind": "float", "required": True,
             "default": 0.0, "help": "High voltage setpoint (kV)."},
        ],
    },
    "hvdcAngleDroopActivePowerControl": {
        "label": "HVDC AC emulation (angle droop)",
        "detail": "Active power set from an offset plus droop * (theta1 - theta2).",
        "index": "id",
        "targets": {"HVDC Lines": "get_hvdc_lines"},
        "fields": [
            {"name": "droop", "kind": "float", "required": True, "default": 0.0,
             "help": "Droop (MW/deg)."},
            {"name": "p0", "kind": "float", "required": True, "default": 0.0,
             "help": "Base active power offset (MW)."},
            {"name": "enabled", "kind": "bool", "required": True, "default": True,
             "help": "Whether AC emulation is active."},
        ],
    },
    "hvdcOperatorActivePowerRange": {
        "label": "HVDC operator active power range",
        "detail": "Operator-imposed power-flow bounds in each direction.",
        "index": "id",
        "targets": {"HVDC Lines": "get_hvdc_lines"},
        "fields": [
            {"name": "opr_from_cs1_to_cs2", "kind": "float", "required": True,
             "default": 0.0, "help": "Max flow from side 1 to side 2 (MW)."},
            {"name": "opr_from_cs2_to_cs1", "kind": "float", "required": True,
             "default": 0.0, "help": "Max flow from side 2 to side 1 (MW)."},
        ],
    },
    "entsoeCategory": {
        "label": "ENTSO-E category code (generator)",
        "detail": "Integer ENTSO-E category code.",
        "index": "id",
        "targets": {"Generators": "get_generators"},
        "fields": [
            {"name": "code", "kind": "int", "required": True, "default": 0,
             "help": "ENTSO-E category code."},
        ],
    },
}


# ---------------------------------------------------------------------------
# Pure registry helpers
# ---------------------------------------------------------------------------
def list_extensions_for_component(component: str) -> list[str]:
    """Return extension names whose ``targets`` mapping includes ``component``."""
    return [
        name for name, schema in CREATABLE_EXTENSIONS.items()
        if component in schema["targets"]
    ]


def list_extension_candidates(
    network: NetworkProxy, extension_name: str, component: str,
) -> list[str]:
    """Return ids of existing elements eligible to carry the extension.

    Looks up the per-component getter (``get_substations`` /
    ``get_generators`` / …) on ``NetworkProxy``, which auto-routes the
    call onto the pypowsybl worker thread.
    """
    schema = CREATABLE_EXTENSIONS.get(extension_name)
    if not schema:
        return []
    getter = schema["targets"].get(component)
    if not getter:
        return []
    try:
        df = getattr(network, getter)()
    except Exception:
        return []
    return sorted(df.index.tolist())


def validate_create_extension_fields(
    extension_name: str, fields: dict,
) -> list[str]:
    """Pure validator — returns list of human-readable errors.

    Required-fields check + the per-extension cross-field rules the
    Streamlit dialog used inline. Empty list means the payload is
    safe to dispatch.
    """
    schema = CREATABLE_EXTENSIONS.get(extension_name)
    if not schema:
        return [f"Unknown extension: {extension_name!r}"]
    errors: list[str] = []
    for fdef in schema["fields"]:
        name = fdef["name"]
        if fdef.get("required") and fields.get(name) in (None, ""):
            errors.append(f"{name} is required.")
    if extension_name == "slackTerminal":
        bus = fields.get("bus_id") or ""
        elem = fields.get("element_id") or ""
        if bool(bus) == bool(elem):
            errors.append(
                "Exactly one of bus_id or element_id must be filled."
            )
    if extension_name == "busbarSectionPosition":
        for k in ("busbar_index", "section_index"):
            v = fields.get(k)
            if v is not None and int(v) < 0:
                errors.append(f"{k} must be >= 0.")
    if extension_name == "activePowerControl":
        mn = fields.get("min_target_p")
        mx = fields.get("max_target_p")
        if mn is not None and mx is not None and mx < mn:
            errors.append("max_target_p must be >= min_target_p.")
    return errors


def create_extension(
    network: NetworkProxy,
    extension_name: str,
    target_id: str,
    fields: dict,
) -> None:
    """Attach a single extension row to an existing element.

    Validates against the registry, coerces types, drops optional
    columns when left blank, and routes the pypowsybl
    ``create_extensions`` call through the worker thread.
    """
    schema = CREATABLE_EXTENSIONS.get(extension_name)
    if not schema:
        raise ValueError(f"Unknown extension: {extension_name!r}")
    if not target_id:
        raise ValueError("Target id is required.")
    errors = validate_create_extension_fields(extension_name, fields)
    if errors:
        raise ValueError("; ".join(errors))

    row: dict[str, Any] = {}
    for fdef in schema["fields"]:
        name = fdef["name"]
        raw_val = fields.get(name)
        if raw_val in (None, "") and fdef.get("optional_fill"):
            continue
        coercer = _EXT_KIND_COERCIONS.get(fdef["kind"])
        if fdef["kind"] == "choice":
            row[name] = str(raw_val)
        elif coercer is bool:
            row[name] = bool(raw_val)
        elif coercer is not None and raw_val not in (None, ""):
            row[name] = coercer(raw_val)
        else:
            row[name] = raw_val

    index_col = schema["index"]
    df = pd.DataFrame(
        {k: [v] for k, v in row.items()},
        index=pd.Index([target_id], name=index_col),
    )

    raw = object.__getattribute__(network, "_obj")

    def _do_create():
        raw.create_extensions(extension_name, df)

    run(_do_create)
