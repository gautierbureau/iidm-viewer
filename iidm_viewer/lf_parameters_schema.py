"""Framework-agnostic helpers for the "Load Flow Parameters" dialog.

Three pieces live here:

* :func:`coerce_generic_value` / :func:`coerce_provider_value` — turn
  raw widget output (often a string) into the right Python type for
  pypowsybl, per the parameter's declared schema.
* :func:`parse_provider_options` — pypowsybl exposes a provider
  parameter's ``possible_values`` as either a Python iterable *or* a
  string like ``"[VAL1, VAL2]"``; normalise to ``list[str]``.
* :func:`group_provider_params_by_category` /
  :func:`filter_changed_provider_params` — render-time + save-time
  helpers the dialogs share.

Streamlit's existing dialog used these inline; the PySide6 and NiceGUI
prototypes now reuse them.
"""
from __future__ import annotations

from typing import Any, Iterable

import pandas as pd

from iidm_viewer.loadflow import GENERIC_PARAMETERS


# ---------------------------------------------------------------------------
# Generic parameters
# ---------------------------------------------------------------------------
def coerce_generic_value(param_def: tuple, raw: Any) -> Any:
    """Convert a widget value to the right type for a generic param.

    ``param_def`` is one of the tuples in
    :data:`iidm_viewer.loadflow.GENERIC_PARAMETERS`. Unknown types
    fall through to ``raw`` unchanged.
    """
    ptype = param_def[1]
    if ptype == "bool":
        if isinstance(raw, str):
            return raw.strip().lower() in ("true", "1", "yes", "on")
        return bool(raw)
    if ptype == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(param_def[2])
    if ptype == "enum":
        options = param_def[4]
        if raw in options:
            return raw
        return param_def[2]
    return raw


def filter_changed_generic_params(values: dict) -> dict:
    """Return only generic-param entries that differ from their default.

    Hosts can pass the trimmed dict straight to ``run_ac`` — pypowsybl
    keeps its own default for missing keys.
    """
    out: dict[str, Any] = {}
    for param_def in GENERIC_PARAMETERS:
        name, _, default, *_ = param_def
        if name not in values:
            continue
        v = coerce_generic_value(param_def, values[name])
        if v != default:
            out[name] = v
    return out


# ---------------------------------------------------------------------------
# Provider parameters
# ---------------------------------------------------------------------------
def parse_provider_options(possible: Any) -> list[str]:
    """Normalise pypowsybl's ``possible_values`` column to ``list[str]``.

    Empty / NaN / single-item lists are returned as-is. The
    ``"[VAL1, VAL2]"`` string shape gets split + stripped.
    """
    if possible is None:
        return []
    if isinstance(possible, str):
        s = possible.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1]
            return [v.strip() for v in inner.split(",") if v.strip()]
        # Plain string → single option.
        return [s]
    if isinstance(possible, Iterable):
        try:
            return [str(v) for v in possible]
        except (TypeError, ValueError):
            return []
    return []


def coerce_provider_value(ptype: str, raw: Any, default: Any = None) -> Any:
    """Cast a raw widget value per pypowsybl's per-param type.

    ``ptype`` is one of ``BOOLEAN`` / ``INTEGER`` / ``DOUBLE`` /
    ``STRING``. Hosts get the typed Python value back; ``run_ac``
    will stringify provider params before forwarding to pypowsybl.
    """
    if ptype == "BOOLEAN":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "1", "yes", "on")
    if ptype == "INTEGER":
        try:
            return int(raw)
        except (TypeError, ValueError):
            try:
                return int(default)
            except (TypeError, ValueError):
                return 0
    if ptype == "DOUBLE":
        try:
            return float(raw)
        except (TypeError, ValueError):
            try:
                return float(default)
            except (TypeError, ValueError):
                return 0.0
    return "" if raw is None else str(raw)


def group_provider_params_by_category(
    info_df: pd.DataFrame,
) -> list[tuple[str, pd.DataFrame]]:
    """Return ``[(category, rows_df), …]`` sorted by category name.

    Both Streamlit and the PySide6 / NiceGUI dialogs render one
    collapsible section per category; centralising the grouping
    keeps that contract consistent.
    """
    if info_df is None or info_df.empty:
        return []
    if "category_key" not in info_df.columns:
        return [("Parameters", info_df)]
    out: list[tuple[str, pd.DataFrame]] = []
    for category in sorted(info_df["category_key"].dropna().unique().tolist()):
        out.append((category, info_df[info_df["category_key"] == category]))
    return out


def filter_changed_provider_params(
    values: dict, info_df: pd.DataFrame,
) -> dict:
    """Keep only provider-param values that differ from their default.

    Comparison is case-insensitive string-based — matches what
    pypowsybl expects when round-tripping through
    ``Parameters.provider_parameters``.
    """
    if info_df is None or info_df.empty:
        return {}
    out: dict[str, Any] = {}
    for name, raw in values.items():
        if name not in info_df.index:
            continue
        default = info_df.at[name, "default"]
        if str(raw).strip().lower() != str(default).strip().lower():
            out[name] = raw
    return out
