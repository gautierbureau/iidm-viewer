"""Framework-agnostic helpers for cross-tab navigation.

The "click X → focus its substation on the Map" pattern needs to
resolve, from a clicked equipment on the SLD, which substation the
Map should fly to. For a branch-style equipment (a Line / 2-Winding
Transformer / Tie Line) that's the substation on the other side; for
a local injection (a Load / Generator / …) it's the substation
containing the current voltage level.

All pypowsybl access runs on the worker thread per the AGENTS.md §1
thread-affinity rule.
"""
from __future__ import annotations

from typing import Optional

from iidm_viewer.powsybl_worker import NetworkProxy, run


# Equipment types that pypowsybl emits as ``equipmentType`` strings on
# the SLD's onFeederCallback. Names match its Java enum.
_BRANCH_TYPES_2VL: frozenset[str] = frozenset({
    "LINE",
    "TIE_LINE",
    "TWO_WINDINGS_TRANSFORMER",
})

_HVDC_LINE_TYPE = "HVDC_LINE"

_CONVERTER_STATION_TYPES: frozenset[str] = frozenset({
    "VSC_CONVERTER_STATION",
    "LCC_CONVERTER_STATION",
})

# Types whose feeder lives at a single VL (no "other side") — clicking
# them focuses the substation already containing the current VL.
_LOCAL_INJECTION_TYPES: frozenset[str] = frozenset({
    "LOAD", "GENERATOR", "BATTERY",
    "SHUNT_COMPENSATOR", "STATIC_VAR_COMPENSATOR",
    "DANGLING_LINE",  # has only one terminal connected to a VL
    "BUSBAR_SECTION",
})


def _vl_to_substation(raw, vl_id: str) -> Optional[str]:
    try:
        vls = raw.get_voltage_levels()
    except Exception:
        return None
    if vls is None or vls.empty or vl_id not in vls.index:
        return None
    if "substation_id" not in vls.columns:
        return None
    sub_id = vls.at[vl_id, "substation_id"]
    return str(sub_id) if sub_id else None


def _other_vl_of_branch(
    raw, getter_name: str, equipment_id: str, current_vl_id: str,
) -> Optional[str]:
    """Find the VL on the other side of a branch. Returns the current
    VL when the branch happens to be a loop, and ``None`` when the
    branch can't be found or its endpoint columns are missing."""
    try:
        df = getattr(raw, getter_name)()
    except Exception:
        return None
    if df is None or df.empty or equipment_id not in df.index:
        return None
    if "voltage_level1_id" not in df.columns or "voltage_level2_id" not in df.columns:
        return None
    vl1 = str(df.at[equipment_id, "voltage_level1_id"])
    vl2 = str(df.at[equipment_id, "voltage_level2_id"])
    if vl1 == current_vl_id:
        return vl2
    if vl2 == current_vl_id:
        return vl1
    # Neither side matches the displayed VL — should be rare in
    # practice; pick side 2 as a best-effort answer.
    return vl2


def _hvdc_other_vl(
    raw, hvdc_line_id: str, current_vl_id: str,
) -> Optional[str]:
    """Walk an HVDC line via its two converter stations to their VLs."""
    try:
        hvdc_df = raw.get_hvdc_lines()
        if hvdc_line_id not in hvdc_df.index:
            return None
        cs1 = str(hvdc_df.at[hvdc_line_id, "converter_station1_id"])
        cs2 = str(hvdc_df.at[hvdc_line_id, "converter_station2_id"])
    except Exception:
        return None

    def _station_vl(station_id: str) -> Optional[str]:
        for getter in ("get_vsc_converter_stations", "get_lcc_converter_stations"):
            try:
                df = getattr(raw, getter)()
            except Exception:
                continue
            if df is not None and station_id in df.index and "voltage_level_id" in df.columns:
                return str(df.at[station_id, "voltage_level_id"])
        return None

    vl1 = _station_vl(cs1)
    vl2 = _station_vl(cs2)
    if vl1 == current_vl_id:
        return vl2
    if vl2 == current_vl_id:
        return vl1
    return vl1 or vl2


def _converter_station_other_vl(
    raw, station_id: str, current_vl_id: str,
) -> Optional[str]:
    """For VSC/LCC clicks: find the HVDC line attached, then the *other*
    station's VL."""
    try:
        hvdc_df = raw.get_hvdc_lines()
    except Exception:
        return None
    if hvdc_df is None or hvdc_df.empty:
        return None
    for hvdc_id in hvdc_df.index:
        cs1 = str(hvdc_df.at[hvdc_id, "converter_station1_id"])
        cs2 = str(hvdc_df.at[hvdc_id, "converter_station2_id"])
        if station_id in (cs1, cs2):
            return _hvdc_other_vl(raw, hvdc_id, current_vl_id)
    return None


def resolve_feeder_substation(
    network: NetworkProxy,
    current_vl_id: str,
    equipment_id: str,
    equipment_type: Optional[str],
) -> Optional[str]:
    """Return the substation id the Map should focus on, or ``None``.

    The contract is "best effort, fail to ``None``":

    * For a Line / Tie Line / 2-Winding Transformer click, walk the
      branch and return the *other* VL's substation.
    * For an HVDC line click, walk via its converter stations.
    * For a VSC / LCC converter station click, walk the HVDC line and
      pick the opposite station's VL's substation.
    * For local injections (Load, Generator, …), return the
      substation of the *current* VL.
    * On anything missing (no geo data, unknown type, equipment not
      found), return ``None`` and let the caller stay where it is.

    Worker-thread bound.
    """
    if not current_vl_id or not equipment_id:
        return None
    raw = object.__getattribute__(network, "_obj")
    et = (equipment_type or "").upper()

    def _do() -> Optional[str]:
        target_vl: Optional[str] = None
        if et in _BRANCH_TYPES_2VL:
            getter = {
                "LINE": "get_lines",
                "TIE_LINE": "get_tie_lines",
                "TWO_WINDINGS_TRANSFORMER": "get_2_windings_transformers",
            }[et]
            target_vl = _other_vl_of_branch(raw, getter, equipment_id, current_vl_id)
        elif et == _HVDC_LINE_TYPE:
            target_vl = _hvdc_other_vl(raw, equipment_id, current_vl_id)
        elif et in _CONVERTER_STATION_TYPES:
            target_vl = _converter_station_other_vl(raw, equipment_id, current_vl_id)
        # Local injections fall through to ``target_vl = None`` ->
        # we use the current VL below.
        if not target_vl:
            target_vl = current_vl_id
        return _vl_to_substation(raw, target_vl)

    return run(_do)
