"""Framework-agnostic pypowsybl import / export options helpers.

What lives here:

* :data:`EXT_TO_FORMAT` + :func:`ext_to_format` — best-guess import
  format from a filename extension.
* :data:`FALLBACK_IMPORT_FORMATS` for pypowsybl builds that lack
  ``get_import_formats``.
* Worker-routed pypowsybl wrappers used by all three prototypes:
  :func:`get_import_formats`, :func:`get_import_post_processors`,
  :func:`get_format_parameters`. None of these cache — each host
  layers its own cache (Streamlit uses ``st.session_state``; the
  PySide6 / NiceGUI prototypes use module-level dicts).
* Pure parameter helpers: :func:`parse_possible_values`,
  :func:`csv_split`, :func:`coerce_param_value`,
  :func:`filter_changed_params`.

The Streamlit ``iidm_viewer.io_options`` module keeps its widget
renderers but delegates every non-UI piece here.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from iidm_viewer.powsybl_worker import run


EXT_TO_FORMAT: dict[str, str] = {
    "xiidm": "XIIDM",
    "iidm": "XIIDM",
    "xml": "XIIDM",
    "uct": "UCTE",
    "ucte": "UCTE",
    "raw": "PSS/E",
    "rawx": "PSS/E",
    "m": "MATPOWER",
    "mat": "MATPOWER",
    "json": "JIIDM",
    "bin": "BIIDM",
    "dgs": "POWER-FACTORY",
    "cdf": "IEEE-CDF",
    "dat": "IEEE-CDF",
}


FALLBACK_IMPORT_FORMATS: list[str] = [
    "XIIDM", "CGMES", "UCTE", "PSS/E", "MATPOWER",
    "IEEE-CDF", "JIIDM", "BIIDM", "POWER-FACTORY",
]


# ---------------------------------------------------------------------------
# Format probing (worker-routed; no caching here)
# ---------------------------------------------------------------------------
def ext_to_format(extension: str) -> str | None:
    """Return a best-guess import format name for a file extension, or
    ``None`` when the extension isn't in the mapping."""
    return EXT_TO_FORMAT.get(extension.lower().lstrip("."))


def get_import_formats() -> list[str]:
    """Pypowsybl's list of import-supported format names.

    Falls back to :data:`FALLBACK_IMPORT_FORMATS` on builds that don't
    yet expose ``get_import_formats`` so the dialogs still show a
    sensible selector.
    """
    def _get():
        import pypowsybl.network as pn
        try:
            return list(pn.get_import_formats())
        except AttributeError:
            return list(FALLBACK_IMPORT_FORMATS)
        except Exception:
            return list(FALLBACK_IMPORT_FORMATS)

    return run(_get)


def get_import_post_processors() -> list[str]:
    """Return the list of available import post-processor names, or
    empty when pypowsybl can't report any."""
    def _get():
        import pypowsybl.network as pn
        try:
            return list(pn.get_import_post_processors())
        except Exception:
            return []

    return run(_get)


def get_format_parameters(which: str, fmt: str) -> pd.DataFrame:
    """Fetch the parameters DataFrame for ``which`` ``'import'`` /
    ``'export'`` and ``fmt`` (e.g. ``'XIIDM'``).

    Each row's index is the parameter name; columns include
    ``description`` / ``type`` / ``default`` / ``possible_values``.
    Returns an empty DataFrame for formats with no configurable
    parameters or when pypowsybl raises.
    """
    def _get():
        import pypowsybl.network as pn
        try:
            if which == "import":
                return pn.get_import_parameters(fmt)
            return pn.get_export_parameters(fmt)
        except Exception:
            return pd.DataFrame()

    return run(_get)


# ---------------------------------------------------------------------------
# Pure parameter helpers
# ---------------------------------------------------------------------------
def parse_possible_values(raw: Any) -> list[str]:
    """Normalise pypowsybl's ``possible_values`` to ``list[str]``.

    Handles both Python iterables and the ``"[A, B, C]"`` string shape
    pypowsybl sometimes uses. Empty / ``None`` → ``[]``.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1]
            return [v.strip() for v in inner.split(",") if v.strip()]
        return [s]
    try:
        return [str(v) for v in raw]
    except (TypeError, ValueError):
        return []


def csv_split(value: Any) -> list[str]:
    """Split a comma-separated value (the STRING_LIST wire format)
    back into a list, stripping whitespace and dropping empties."""
    if value is None:
        return []
    return [s.strip() for s in str(value).split(",") if s.strip()]


def coerce_param_value(ptype: str, raw: Any, default: Any = None) -> str:
    """Cast a widget value back to the string shape pypowsybl expects.

    pypowsybl's import/export ``parameters`` dict is ``dict[str, str]``:
    booleans become ``'true'`` / ``'false'``, numbers stringify. This
    helper centralises the conversion so each host's dialog can keep
    the user input typed but ship the right wire format.
    """
    ptype_upper = (ptype or "STRING").upper()
    if ptype_upper == "BOOLEAN":
        if isinstance(raw, bool):
            return "true" if raw else "false"
        return "true" if str(raw).strip().lower() in ("true", "1", "yes", "on") else "false"
    if ptype_upper == "INTEGER":
        try:
            return str(int(float(raw)))
        except (TypeError, ValueError):
            return "" if default is None else str(default)
    if ptype_upper in ("DOUBLE", "FLOAT"):
        try:
            return str(float(raw))
        except (TypeError, ValueError):
            return "" if default is None else str(default)
    return "" if raw is None else str(raw)


def filter_changed_params(values: dict, params_df: pd.DataFrame) -> dict:
    """Drop entries whose value equals pypowsybl's default.

    Comparison is string-based on the raw default column so the result
    can be passed straight to ``load_from_binary_buffer`` /
    ``save_to_binary_buffer`` without sending redundant overrides.
    """
    if params_df is None or params_df.empty:
        return {}
    out: dict[str, str] = {}
    for name, raw in values.items():
        if name not in params_df.index:
            continue
        default = params_df.at[name, "default"] if "default" in params_df.columns else ""
        if str(raw) != str(default or ""):
            out[str(name)] = str(raw)
    return out
