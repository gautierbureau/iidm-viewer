"""Framework-agnostic pypowsybl network loading + utilities.

Single source of truth for "load a network and pick a sensible default
voltage level". The Streamlit, PySide6 and NiceGUI front-ends all
funnel through this module:

* :func:`load_from_path` — used by the prototype CLIs.
* :func:`load_from_bytes` — used by Streamlit's
  ``streamlit.file_uploader`` integration (which only hands the
  application a buffer, not a path).
* :func:`pick_default_vl` — the "highest nominal V" pick reproduced
  by every front-end's first-open logic.
* :func:`get_import_extensions` / :func:`get_export_formats` —
  worker-routed wrappers around pypowsybl's runtime discovery.

All pypowsybl calls run on the worker thread
(``iidm_viewer.powsybl_worker.run``) so the GraalVM thread-affinity
rule from AGENTS.md §1 is preserved.
"""
from __future__ import annotations

from typing import Optional

from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_from_path(
    path: str,
    *,
    parameters: Optional[dict[str, str]] = None,
    post_processors: Optional[list[str]] = None,
) -> NetworkProxy:
    """Load a network file from disk and return a :class:`NetworkProxy`.

    ``parameters`` is forwarded to pypowsybl's import-parameters dict;
    ``post_processors`` is the list of post-processor names to apply
    after parsing.
    """
    params = parameters or {}
    pp = post_processors or []

    def _load():
        import pypowsybl.network as pn
        return pn.load(path, parameters=params, post_processors=pp)

    return NetworkProxy(run(_load))


def load_from_bytes(
    file_name: str,
    raw_bytes: bytes,
    *,
    parameters: Optional[dict[str, str]] = None,
    post_processors: Optional[list[str]] = None,
) -> NetworkProxy:
    """Load a network from an in-memory buffer.

    pypowsybl's ``load_from_binary_buffer`` expects either a real
    archive or a single-file payload. To keep one code path for both
    archives and bare XIIDM/XML files, plain files are wrapped in a
    transient in-memory ZIP — matching the long-standing Streamlit
    upload flow.
    """
    from io import BytesIO
    import zipfile

    if file_name.lower().endswith(".zip"):
        buf = BytesIO(raw_bytes)
    else:
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(file_name, raw_bytes)
        buf.seek(0)

    params = parameters or {}
    pp = post_processors or []

    def _load():
        import pypowsybl.network as pn
        return pn.load_from_binary_buffer(
            buf, parameters=params, post_processors=pp,
        )

    return NetworkProxy(run(_load))


def create_empty(network_id: str = "network") -> NetworkProxy:
    """Create a blank pypowsybl network on the worker thread."""
    nid = (network_id or "network").strip() or "network"

    def _create():
        import pypowsybl.network as pn
        return pn.create_empty(network_id=nid)

    return NetworkProxy(run(_create))


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------
def pick_default_vl(network: NetworkProxy) -> Optional[str]:
    """Pick the highest-nominal-V voltage level as the front-end's default.

    Mirrors the Streamlit ``vl_selector`` default (``components.py``)
    and gives the Qt / NiceGUI prototypes the same "open on the 400 kV"
    behaviour. Returns ``None`` for an empty network.
    """
    raw = object.__getattribute__(network, "_obj")

    def _pick():
        vls = raw.get_voltage_levels()
        if vls is None or vls.empty:
            return None
        if "nominal_v" in vls.columns:
            return str(vls["nominal_v"].idxmax())
        return str(vls.index[0])

    return run(_pick)


def list_voltage_levels_for_selector(network: NetworkProxy):
    """Return ``id``/``display``/``substation_id``/``nominal_v`` columns
    sorted by ``display``.

    Shared by Streamlit's ``vl_selector`` and the PySide6 + NiceGUI
    prototype sidebars. Falls back to an empty DataFrame for an empty
    network so callers don't have to special-case it.
    """
    import pandas as pd
    raw = object.__getattribute__(network, "_obj")

    def _fetch():
        vls = raw.get_voltage_levels(attributes=["name", "substation_id", "nominal_v"])
        if vls is None or vls.empty:
            return pd.DataFrame(
                columns=["id", "name", "substation_id", "nominal_v", "display"],
            )
        vls = vls.reset_index()
        vls["display"] = vls.apply(
            lambda r: r["name"] if r["name"] else r["id"], axis=1,
        )
        return vls.sort_values("display").reset_index(drop=True)

    return run(_fetch)


def filter_voltage_levels(vls_df, text: str):
    """Case-insensitive substring filter on the ``display`` column.

    Used by the VL picker's "Filter" text input. ``text`` empty → return
    the input unchanged.
    """
    if not text:
        return vls_df
    mask = vls_df["display"].str.contains(text, case=False, na=False, regex=False)
    return vls_df[mask]


def get_import_extensions() -> list[str]:
    """File extensions pypowsybl can import, deduplicated and with ``zip`` always included."""

    def _do():
        import pypowsybl.network as pn
        return pn.get_import_supported_extensions()

    raw = run(_do)
    seen: set[str] = set()
    out: list[str] = []
    for e in raw:
        lower = e.lower()
        if lower not in seen:
            seen.add(lower)
            out.append(lower)
    if "zip" not in seen:
        out.append("zip")
    return out


def get_export_formats() -> list[str]:
    """Export format names pypowsybl supports."""

    def _do():
        import pypowsybl.network as pn
        return pn.get_export_formats()

    return run(_do)


def export_network(
    network: NetworkProxy,
    format_name: str,
    parameters: Optional[dict] = None,
) -> tuple[bytes, str]:
    """Export the network; return ``(bytes, file_extension)``.

    pypowsybl wraps some formats (e.g. XIIDM) in a ZIP archive.
    Single-file ZIPs are unwrapped so the caller gets the real content
    and the correct extension. Multi-file ZIPs are served as-is with
    extension ``zip``. ``parameters`` is forwarded verbatim to
    ``save_to_binary_buffer`` so callers can pass format-specific
    options.

    Shared by Streamlit's ``Save network`` dialog and the PySide6 +
    NiceGUI prototypes' save buttons.
    """
    import io as _io
    import zipfile as _zf

    raw = object.__getattribute__(network, "_obj")
    params = parameters or {}

    def _export():
        return raw.save_to_binary_buffer(format_name, parameters=params).getvalue()

    data = run(_export)

    if data[:2] == b'PK':
        try:
            with _zf.ZipFile(_io.BytesIO(data)) as zf:
                names = zf.namelist()
                if len(names) == 1:
                    inner = zf.read(names[0])
                    ext = (
                        names[0].rsplit(".", 1)[-1]
                        if "." in names[0]
                        else format_name.lower()
                    )
                    return inner, ext
        except Exception:
            pass
        return data, "zip"

    return data, format_name.lower()


def guess_mime_for_export(data: bytes) -> str:
    """Best-effort MIME type for an exported network blob.

    Sniff the first byte: ``<`` (XML) → ``text/xml``, ``{`` (JSON) →
    ``application/json``, anything else → ``application/octet-stream``.
    Used by all three prototypes' download paths.
    """
    if data[:5] == b'<?xml':
        return "text/xml; charset=utf-8"
    if data[:1] == b'{':
        return "application/json"
    return "application/octet-stream"
