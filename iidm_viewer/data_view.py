"""Framework-agnostic helpers for the Data Explorer tab.

Hosts the things every host's data-table view needs:

* :data:`PRIORITY_COLUMNS` / :data:`PRIORITY_ANCHOR` + :func:`reorder_columns`
  — put the action-relevant columns (target_p, connected, …) next to
  the id/name so the table reads cleanly.
* :data:`FILTERS` + :func:`compute_filter_widget_spec` + :func:`apply_filter_specs`
  — the structured per-column filter registry plus pure-pandas
  helpers each host wraps in its own widget toolkit.
* :data:`VL_FILTERABLE` + :func:`filter_by_voltage_level` — the "filter
  by the currently-selected VL" path.
* :func:`build_vl_lookup` + :func:`enrich_with_joins` +
  :func:`get_enriched_dataframe` — the substation/country/nominal_v
  join that turns raw pypowsybl tables into something the FILTERS
  whitelist can target.
* :func:`dataframe_to_csv` — the CSV-export bytes the download
  button hands the user.

No streamlit / Qt / NiceGUI imports — the Streamlit
``caches.py`` / ``filters.py`` / ``data_explorer.py`` delegate here,
and the PySide6 + NiceGUI prototypes consume the same primitives.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from iidm_viewer.component_registry import COMPONENT_TYPES, get_dataframe
from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Column ordering
# ---------------------------------------------------------------------------
PRIORITY_COLUMNS: dict[str, list[str]] = {
    "Generators": [
        "target_p", "target_q", "target_v", "connected",
        "voltage_regulator_on", "p", "q", "regulated_element_id",
    ],
    "Loads": ["p0", "q0", "connected", "p", "q"],
    "Static VAR Compensators": [
        "regulation_mode", "voltage_setpoint",
        "reactive_power_setpoint", "connected", "regulated_element_id",
    ],
    "VSC Converter Stations": [
        "target_v", "target_q", "voltage_regulator_on",
        "connected", "regulated_element_id",
    ],
    "Lines": ["connected1", "connected2"],
    "2-Winding Transformers": ["connected1", "connected2"],
    "3-Winding Transformers": ["connected1", "connected2", "connected3"],
}

# Column the PRIORITY_COLUMNS get inserted *after*. Defaults to "name".
PRIORITY_ANCHOR: dict[str, str] = {
    "Lines": "i2",
    "2-Winding Transformers": "i2",
    "3-Winding Transformers": "i3",
}


def reorder_columns(df: pd.DataFrame, component: str) -> pd.DataFrame:
    """Move ``PRIORITY_COLUMNS[component]`` right after the anchor column.

    No-op when the priority list is empty or the anchor column isn't
    present. Returns ``df`` unchanged in that case.
    """
    priority = PRIORITY_COLUMNS.get(component)
    if not priority:
        return df
    anchor = PRIORITY_ANCHOR.get(component, "name")
    if anchor not in df.columns:
        return df
    present = [c for c in priority if c in df.columns]
    if not present:
        return df
    cols = list(df.columns)
    for c in present:
        cols.remove(c)
    insert_at = cols.index(anchor) + 1
    for i, c in enumerate(present):
        cols.insert(insert_at + i, c)
    return df[cols]


# ---------------------------------------------------------------------------
# Structured filters (column whitelist per component)
# ---------------------------------------------------------------------------
FILTERS: dict[str, list[str]] = {
    "Generators": [
        "nominal_v", "country", "energy_source",
        "min_p", "max_p", "target_p",
        "voltage_regulator_on", "connected",
    ],
    "Loads": ["nominal_v", "country", "type", "p0", "connected"],
    "Batteries": ["nominal_v", "country", "min_p", "max_p", "connected"],
    "Voltage Levels": ["nominal_v", "country", "topology_kind"],
    "Substations": ["country", "TSO"],
    "Buses": ["nominal_v", "v_mag", "connected_component"],
    "Busbar Sections": ["nominal_v", "connected"],
    "Lines": ["nominal_v1", "nominal_v2", "p1", "connected1", "connected2"],
    "2-Winding Transformers": ["nominal_v1", "nominal_v2", "rated_s"],
    "Shunt Compensators": ["nominal_v", "model_type", "connected"],
    "Static VAR Compensators": ["nominal_v", "connected"],
    "VSC Converter Stations": ["nominal_v", "connected"],
    "LCC Converter Stations": ["nominal_v", "connected"],
    "Switches": ["nominal_v", "kind", "open"],
    "Dangling Lines": ["nominal_v", "connected"],
}


def compute_filter_widget_spec(series: pd.Series) -> dict[str, Any]:
    """Inspect a column and return a widget shape descriptor.

    Returns a dict carrying enough information for each UI host to
    pick a widget:

    * ``{"kind": "bool"}``
    * ``{"kind": "range", "min": float, "max": float}``  (or ``"empty"``
      / ``"constant"`` for degenerate cases)
    * ``{"kind": "multiselect", "options": list[str]}``  (≤30 unique
      non-empty strings)
    * ``{"kind": "skip"}`` for high-cardinality object columns

    The exact widget is the host's call; the shape is shared.
    """
    if pd.api.types.is_bool_dtype(series.dtype):
        return {"kind": "bool"}

    if pd.api.types.is_numeric_dtype(series.dtype):
        clean = series.dropna()
        if clean.empty:
            return {"kind": "range", "min": None, "max": None, "state": "empty"}
        lo, hi = float(clean.min()), float(clean.max())
        if lo == hi:
            return {"kind": "range", "min": lo, "max": hi, "state": "constant"}
        return {"kind": "range", "min": lo, "max": hi}

    clean = series.dropna().astype(str)
    clean = clean[clean != ""]
    uniq = sorted(clean.unique())
    if not uniq or len(uniq) > 30:
        return {"kind": "skip"}
    return {"kind": "multiselect", "options": uniq}


def apply_filter_specs(
    df: pd.DataFrame,
    specs: dict[str, Any],
) -> pd.DataFrame:
    """Apply user-selected filter values to ``df``.

    ``specs`` maps column name -> active filter value:

    * bool column: ``"Any"`` / ``"True"`` / ``"False"`` (or a Python
      bool).
    * numeric range: ``(lo, hi)`` tuple.
    * multiselect: list of accepted values (strings).

    Any column missing from ``df`` or carrying ``None`` / empty value
    is treated as "no filter".
    """
    if df.empty or not specs:
        return df
    mask = pd.Series(True, index=df.index)
    for col, value in specs.items():
        if col not in df.columns or value is None:
            continue
        series = df[col]
        if pd.api.types.is_bool_dtype(series.dtype):
            if isinstance(value, bool):
                mask &= series.fillna(not value) == value
            elif value == "True":
                mask &= series.fillna(False) == True  # noqa: E712
            elif value == "False":
                mask &= series.fillna(True) == False  # noqa: E712
        elif pd.api.types.is_numeric_dtype(series.dtype):
            if isinstance(value, (tuple, list)) and len(value) == 2:
                lo, hi = value
                if lo is None and hi is None:
                    continue
                mask &= series.between(lo, hi)
        else:
            if isinstance(value, (list, set, tuple)) and value:
                mask &= series.astype(str).isin([str(v) for v in value])
    return df[mask]


# ---------------------------------------------------------------------------
# Filter-by-selected-VL
# ---------------------------------------------------------------------------
VL_FILTERABLE: frozenset[str] = frozenset({
    "Generators", "Loads", "Switches", "Shunt Compensators",
    "Batteries", "Busbar Sections", "Static VAR Compensators",
    "VSC Converter Stations", "LCC Converter Stations",
})


def filter_by_voltage_level(df: pd.DataFrame, vl_id: Optional[str]) -> pd.DataFrame:
    """Narrow ``df`` to the rows whose ``voltage_level_id`` matches ``vl_id``.

    No-op when ``vl_id`` is empty or the column isn't present.
    """
    if not vl_id or df.empty or "voltage_level_id" not in df.columns:
        return df
    return df[df["voltage_level_id"].astype(str) == str(vl_id)]


# ---------------------------------------------------------------------------
# Enriched join (substation_id / country / nominal_v)
# ---------------------------------------------------------------------------
def build_vl_lookup(network: NetworkProxy) -> pd.DataFrame:
    """Return ``(voltage_levels ⋈ substations)`` with ``country`` joined in.

    Worker-thread bound. Returns an empty four-column frame on any
    failure so callers can left-merge unconditionally.
    """
    raw = object.__getattribute__(network, "_obj")

    def _do() -> pd.DataFrame:
        try:
            vls = raw.get_voltage_levels().reset_index()
            subs = raw.get_substations().reset_index()
        except Exception:
            return pd.DataFrame(columns=["id", "substation_id", "nominal_v", "country"])
        if "substation_id" not in vls.columns or "id" not in subs.columns:
            return pd.DataFrame(columns=["id", "substation_id", "nominal_v", "country"])
        subs = subs.rename(columns={"id": "substation_id"})
        vls["substation_id"] = vls["substation_id"].astype(str)
        subs["substation_id"] = subs["substation_id"].astype(str)
        return vls.merge(subs, on="substation_id", how="left")

    return run(_do)


def enrich_with_joins(df: pd.DataFrame, vl_lookup: pd.DataFrame) -> pd.DataFrame:
    """Left-join VL/substation columns onto ``df``.

    Looks at ``df`` for ``substation_id``, ``voltage_level_id``, and
    ``voltage_level{1,2}_id`` columns and adds the corresponding
    ``country`` / ``nominal_v`` columns when they are missing. Returns
    a new DataFrame; the original index is preserved when possible.
    """
    idx_name = df.index.name
    out = df.reset_index() if idx_name else df.copy()

    if "substation_id" in out.columns and "country" not in out.columns and "country" in vl_lookup.columns:
        out = out.merge(
            vl_lookup[["substation_id", "country"]].drop_duplicates("substation_id"),
            on="substation_id",
            how="left",
        )

    if "voltage_level_id" in out.columns:
        missing = [c for c in ("nominal_v", "country")
                   if c not in out.columns and c in vl_lookup.columns]
        if missing:
            lookup = vl_lookup.rename(columns={"id": "voltage_level_id"})[
                ["voltage_level_id", *missing]
            ].copy()
            lookup["voltage_level_id"] = lookup["voltage_level_id"].astype(str)
            out["voltage_level_id"] = out["voltage_level_id"].astype(str)
            out = out.merge(lookup, on="voltage_level_id", how="left")

    for side in ("1", "2"):
        col = f"voltage_level{side}_id"
        if col not in out.columns:
            continue
        wanted = [f"nominal_v{side}", f"country{side}"]
        if all(w in out.columns for w in wanted):
            continue
        if "nominal_v" not in vl_lookup.columns or "country" not in vl_lookup.columns:
            continue
        lookup = vl_lookup.rename(
            columns={
                "id": col,
                "nominal_v": f"nominal_v{side}",
                "country": f"country{side}",
            }
        )[[col, f"nominal_v{side}", f"country{side}"]].copy()
        lookup[col] = lookup[col].astype(str)
        out[col] = out[col].astype(str)
        out = out.merge(lookup, on=col, how="left")

    if idx_name and idx_name in out.columns:
        out = out.set_index(idx_name)
    return out


def get_enriched_dataframe(
    network: NetworkProxy, component: str,
) -> pd.DataFrame:
    """Return the component's DataFrame enriched with VL-derived columns.

    Hosts that don't have their own cache layer can call this on every
    refresh; one worker round-trip per call (the registry's
    ``get_dataframe`` already runs on the worker, and
    :func:`build_vl_lookup` does too).
    """
    df = get_dataframe(network, component)
    if df.empty or component not in COMPONENT_TYPES:
        return df
    lookup = build_vl_lookup(network)
    return enrich_with_joins(df, lookup)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
def dataframe_to_csv(df: pd.DataFrame) -> bytes:
    """Encode ``df`` as UTF-8 CSV bytes (suitable for any host's download API)."""
    return df.to_csv(index=False).encode("utf-8")
