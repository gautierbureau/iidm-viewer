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


def get_import_post_processors() -> list[str]:
    """Return the available import post-processor names (cached per session).

    Post-processors are opt-in transformations applied after the network is
    parsed.  They are passed as a list of strings to ``load_from_binary_buffer``
    (``post_processors`` argument).  Typical values include
    ``'loadflowResultsCompletion'``, ``'geoJsonImporter'``, and
    ``'replaceTieLinesByLines'``.
    """
    if "_import_post_processors" not in st.session_state:
        def _get():
            import pypowsybl.network as pn
            try:
                return list(pn.get_import_post_processors())
            except Exception:
                return []
        st.session_state["_import_post_processors"] = run(_get)
    return st.session_state["_import_post_processors"]


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


def _parse_possible_values(raw: str) -> list[str] | None:
    """Parse a `[a, b, c]`-style possible_values string into a list, or None."""
    s = (raw or "").strip()
    if not (s.startswith("[") and s.endswith("]")):
        return None
    opts = [o.strip() for o in s[1:-1].split(",") if o.strip()]
    return opts if len(opts) > 1 else None


def _csv_split(value: str) -> list[str]:
    """Split a pypowsybl STRING_LIST value (no spaces) back into items."""
    return [s.strip() for s in (value or "").split(",") if s.strip()]


def _render_paired_extension_lists(
    rows: list[tuple[str, pd.Series]],
    session_key_prefix: str,
) -> dict[str, str]:
    """Render two STRING_LIST params sharing the same possible_values as one table.

    Each option in the shared list gets a 3-state selector — *Default* (the
    extension is in neither list), or one of the two parameter-specific
    states.  This makes it impossible to put the same item in both lists,
    which pypowsybl rejects.

    Returns ``{param_name: comma_joined_value}`` for both rows.
    """
    name_a, row_a = rows[0]
    name_b, row_b = rows[1]
    desc_a = str(row_a.get("description") or name_a)
    desc_b = str(row_b.get("description") or name_b)

    options = _parse_possible_values(str(row_a.get("possible_values") or "")) or []

    # Short, table-friendly state labels; long descriptions go in the caption.
    label_default = "Default"
    label_a = "Include only"
    label_b = "Exclude"
    states = [label_default, label_a, label_b]

    key_a = f"{session_key_prefix}__{name_a}"
    key_b = f"{session_key_prefix}__{name_b}"
    sel_a = set(_csv_split(st.session_state.get(key_a, "")))
    sel_b = set(_csv_split(st.session_state.get(key_b, "")))
    # If a stale session put the same item in both, prefer "Exclude" to keep
    # the editor consistent — the table can't represent the conflict.
    sel_a -= sel_b

    st.caption(
        f"For each extension, pick **{label_default}** (pypowsybl decides), "
        f"**{label_a}** ({desc_a}), or **{label_b}** ({desc_b}). "
        "Each extension can be in at most one list."
    )

    initial = pd.DataFrame(
        {
            "Extension": options,
            "State": [
                label_a if x in sel_a else (label_b if x in sel_b else label_default)
                for x in options
            ],
        }
    ).set_index("Extension")

    edited = st.data_editor(
        initial,
        use_container_width=True,
        column_config={
            "State": st.column_config.SelectboxColumn(
                "State", options=states, required=True,
            ),
        },
        key=f"{session_key_prefix}__paired_{name_a}_{name_b}",
    )

    new_a = ",".join(x for x in options if edited.at[x, "State"] == label_a)
    new_b = ",".join(x for x in options if edited.at[x, "State"] == label_b)
    # Mirror into the per-param session keys so subsequent renders pre-load the
    # same selection even if the editor state is dropped.
    st.session_state[key_a] = new_a
    st.session_state[key_b] = new_b
    return {name_a: new_a, name_b: new_b}


def _render_single_string_list(
    name: str, row: pd.Series, options: list[str], session_key_prefix: str,
) -> str:
    """Render one STRING_LIST parameter as an `st.multiselect`."""
    desc = str(row.get("description") or name)
    widget_key = f"{session_key_prefix}__{name}"
    prev_csv = str(st.session_state.get(widget_key, row.get("default") or ""))
    prev = [x for x in _csv_split(prev_csv) if x in options]
    sel = st.multiselect(desc, options=options, default=prev, key=widget_key)
    return ",".join(sel)


def render_parameters_form(
    params_df: pd.DataFrame,
    session_key_prefix: str,
) -> dict[str, str]:
    """Render Streamlit widgets for a pypowsybl parameters DataFrame.

    Each row in *params_df* becomes one widget.  The row index is the
    parameter name; expected columns are ``description``, ``type``,
    ``default``, and optionally ``possible_values``.

    STRING_LIST parameters that share the same ``possible_values`` (e.g.
    ``iidm.import.xml.included.extensions`` and the matching ``excluded``
    one) are rendered jointly as a single state-per-item table so the
    user cannot put the same value in both lists.

    Returns a ``dict[str, str]`` ready to pass as the ``parameters``
    argument of :func:`load_from_binary_buffer` or
    :meth:`save_to_binary_buffer`.  Only parameters whose widget value
    differs from the pypowsybl default are included in the dict, so
    callers that pass the result verbatim don't over-specify options.
    """
    if params_df is None or params_df.empty:
        st.caption("No configurable options for this format.")
        return {}

    # Group STRING_LIST rows by their possible_values list so that pairs
    # (typical case: included/excluded extensions) can be rendered jointly.
    paired: dict[tuple[str, ...], list[tuple[str, pd.Series]]] = {}
    for name, row in params_df.iterrows():
        if str(row.get("type") or "").upper() != "STRING_LIST":
            continue
        opts = _parse_possible_values(str(row.get("possible_values") or ""))
        if not opts:
            continue
        paired.setdefault(tuple(opts), []).append((str(name), row))
    paired_names: set[str] = set()
    paired_results: dict[str, str] = {}
    for items in paired.values():
        if len(items) >= 2:
            paired_results.update(
                _render_paired_extension_lists(items[:2], session_key_prefix)
            )
            paired_names.update(n for n, _ in items[:2])

    values: dict[str, str] = dict(paired_results)

    for name, row in params_df.iterrows():
        if str(name) in paired_names:
            continue
        desc = str(row.get("description") or name)
        ptype = str(row.get("type") or "STRING").upper()
        default = str(row.get("default") or "")
        options = _parse_possible_values(str(row.get("possible_values") or ""))

        widget_key = f"{session_key_prefix}__{name}"

        if ptype == "STRING_LIST" and options:
            values[name] = _render_single_string_list(
                str(name), row, options, session_key_prefix,
            )

        elif options:
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
