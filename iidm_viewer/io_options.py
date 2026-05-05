"""Helpers for rendering pypowsybl import/export parameter forms in Streamlit."""
import pandas as pd
import streamlit as st

from iidm_viewer.powsybl_worker import run

_EXT_TO_FORMAT = {
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

_FALLBACK_IMPORT_FORMATS = [
    "XIIDM", "CGMES", "UCTE", "PSS/E", "MATPOWER",
    "IEEE-CDF", "JIIDM", "BIIDM", "POWER-FACTORY",
]


def ext_to_format(extension: str) -> str | None:
    """Return a best-guess import format name for a file extension, or None."""
    return _EXT_TO_FORMAT.get(extension.lower().lstrip("."))


def get_import_formats() -> list[str]:
    """Return import format names supported by pypowsybl (cached per session)."""
    if "_import_formats" not in st.session_state:
        def _get():
            import pypowsybl.network as pn
            try:
                return list(pn.get_import_formats())
            except AttributeError:
                return _FALLBACK_IMPORT_FORMATS
        st.session_state["_import_formats"] = run(_get)
    return st.session_state["_import_formats"]


def get_format_parameters(which: str, fmt: str) -> pd.DataFrame:
    """Fetch import or export parameters for *fmt* (cached per session).

    *which* must be ``'import'`` or ``'export'``.
    Returns an empty DataFrame when the format has no parameters.
    """
    cache_key = f"_fmt_params_{which}_{fmt}"
    if cache_key not in st.session_state:
        def _get():
            import pypowsybl.network as pn
            try:
                if which == "import":
                    return pn.get_import_parameters(fmt)
                else:
                    return pn.get_export_parameters(fmt)
            except Exception:
                return pd.DataFrame()
        st.session_state[cache_key] = run(_get)
    return st.session_state[cache_key]


def render_parameters_form(
    params_df: pd.DataFrame,
    session_key_prefix: str,
) -> dict[str, str]:
    """Render Streamlit widgets for a pypowsybl parameters DataFrame.

    Each row in *params_df* becomes one widget.  The row index is the
    parameter name; expected columns are ``description``, ``type``,
    ``default``, and optionally ``possible_values``.

    Returns a ``dict[str, str]`` ready to pass as the ``parameters``
    argument of :func:`load_from_binary_buffer` or
    :meth:`save_to_binary_buffer`.  Only parameters whose widget value
    differs from the pypowsybl default are included in the dict, so
    callers that pass the result verbatim don't over-specify options.
    """
    if params_df is None or params_df.empty:
        st.caption("No configurable options for this format.")
        return {}

    values: dict[str, str] = {}

    for name, row in params_df.iterrows():
        desc = str(row.get("description") or name)
        ptype = str(row.get("type") or "STRING").upper()
        default = str(row.get("default") or "")
        possible_values_raw = str(row.get("possible_values") or "").strip()

        widget_key = f"{session_key_prefix}__{name}"

        # Parse enum option list: '[val1, val2, ...]'
        options: list[str] | None = None
        if possible_values_raw.startswith("[") and possible_values_raw.endswith("]"):
            opts = [o.strip() for o in possible_values_raw[1:-1].split(",") if o.strip()]
            if len(opts) > 1:
                options = opts

        if options:
            current = st.session_state.get(widget_key, default)
            idx = options.index(str(current)) if str(current) in options else 0
            val = st.selectbox(desc, options=options, index=idx, key=widget_key)
            values[name] = str(val)

        elif ptype == "BOOLEAN":
            current_bool = str(st.session_state.get(widget_key, default)).upper() in ("TRUE", "1", "YES")
            val = st.checkbox(desc, value=current_bool, key=widget_key)
            values[name] = str(val).lower()

        elif ptype in ("DOUBLE", "FLOAT"):
            try:
                float_default = float(default) if default else 0.0
            except ValueError:
                float_default = 0.0
            current_float = float(st.session_state.get(widget_key, float_default))
            val = st.number_input(desc, value=current_float, format="%g", key=widget_key)
            values[name] = str(val)

        elif ptype == "INTEGER":
            try:
                int_default = int(float(default)) if default else 0
            except ValueError:
                int_default = 0
            current_int = int(st.session_state.get(widget_key, int_default))
            val = st.number_input(desc, value=current_int, step=1, key=widget_key)
            values[name] = str(int(val))

        else:
            current_str = str(st.session_state.get(widget_key, default))
            val = st.text_input(desc, value=current_str, key=widget_key)
            values[name] = str(val)

    # Only return params that differ from their defaults so we don't send
    # redundant overrides; pypowsybl treats missing keys as "use default".
    non_default: dict[str, str] = {}
    for name, row in params_df.iterrows():
        default = str(row.get("default") or "")
        v = values.get(str(name))
        if v is not None and v != default:
            non_default[str(name)] = v
    return non_default
