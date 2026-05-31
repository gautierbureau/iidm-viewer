"""Framework-agnostic core for the Overview tab.

Shared by the Streamlit :mod:`iidm_viewer.network_info`, the PySide6
:mod:`iidm_viewer.qt.overview_tab` and the NiceGUI ``_build_overview``
in :mod:`iidm_viewer.web.app`. No streamlit / Qt / NiceGUI imports here.

Public API:

* :func:`compute_overview_data` — single worker hop that returns an
  :class:`OverviewData` bundle (metadata + country totals + losses +
  component counts).
* Pure helpers operating on raw pypowsybl frames + numbers:
  :func:`build_metadata`, :func:`country_totals`,
  :func:`build_country_totals_display`,
  :func:`branch_losses_totals`, :func:`losses_by_country`,
  :func:`build_losses_by_country_display`,
  :func:`build_component_counts`,
  :func:`build_vl_country_map`.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from iidm_viewer.component_registry import COMPONENT_TYPES
from iidm_viewer.powsybl_worker import NetworkProxy, run


# ---------------------------------------------------------------------------
# Display schemas (shared across hosts)
# ---------------------------------------------------------------------------
COUNTRY_TOTALS_RAW_COLUMNS: list[str] = [
    "country",
    "generation_target_mw", "generation_actual_mw",
    "consumption_target_mw", "consumption_actual_mw",
]
COUNTRY_TOTALS_DISPLAY_COLUMNS: list[str] = [
    "Country",
    "Gen target (MW)", "Gen actual (MW)",
    "Load target (MW)", "Load actual (MW)",
]
LOSSES_BY_COUNTRY_COLUMNS: list[str] = ["Country", "Losses (MW)"]


# ---------------------------------------------------------------------------
# Data bundle
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OverviewMetadata:
    network_id: str
    name: str
    source_format: str
    case_date: str  # ISO date string, empty when not set


@dataclass(frozen=True)
class OverviewData:
    """Bundle returned by :func:`compute_overview_data`.

    * ``metadata`` — string-only network identification (no NaN handling
      needed downstream).
    * ``country_totals`` — DataFrame matching
      :data:`COUNTRY_TOTALS_RAW_COLUMNS`. Empty when no generators or
      loads are present.
    * ``losses`` — ``{"lines": float, "transformers": float,
      "total": float, "has_data": bool}``. ``has_data`` is False when no
      branch has a finite p1/p2 (no load flow).
    * ``losses_by_country`` — ``country → MW`` Series. Empty when no
      load flow has run.
    * ``component_counts`` — ``label → int`` (only entries with non-zero
      counts, preserving :data:`COMPONENT_TYPES` order).
    """
    metadata: OverviewMetadata
    country_totals: pd.DataFrame
    losses: dict
    losses_by_country: pd.Series
    component_counts: dict[str, int]


# ---------------------------------------------------------------------------
# Pure helpers (operate on a NetworkProxy — every getattr already hops
# through the worker via ``NetworkProxy.__getattr__``).
# ---------------------------------------------------------------------------
def branch_losses_totals(network: NetworkProxy) -> dict:
    """Return active-power losses summed over lines and 2WT.

    Uses ``p1 + p2`` (pypowsybl sign convention). Keys: ``lines``,
    ``transformers``, ``total``, ``has_data``. Entries whose p1 or p2
    is NaN (no load flow) are skipped so totals only reflect branches
    with a solved flow.
    """
    out: dict = {"lines": 0.0, "transformers": 0.0, "total": 0.0}
    any_valid = False
    for key, method in (
        ("lines", "get_lines"),
        ("transformers", "get_2_windings_transformers"),
    ):
        try:
            df = getattr(network, method)(attributes=["p1", "p2"])
        except Exception:
            continue
        if df.empty:
            continue
        losses = (df["p1"] + df["p2"]).dropna()
        if losses.empty:
            continue
        any_valid = True
        out[key] = float(losses.sum())
    out["total"] = out["lines"] + out["transformers"]
    out["has_data"] = any_valid
    # Back-compat: the pre-refactor dict used a leading underscore.
    # Existing callers (Streamlit + tests) read this key.
    out["_has_data"] = any_valid
    return out


def build_vl_country_map(network: NetworkProxy) -> pd.DataFrame:
    """VL id → country lookup. Empty when either ``get_voltage_levels``
    or ``get_substations`` is empty / raises."""
    try:
        vls = network.get_voltage_levels(
            attributes=["substation_id"],
        ).reset_index()
    except Exception:
        return pd.DataFrame(columns=["voltage_level_id", "country"])
    try:
        subs = network.get_substations(attributes=["country"]).reset_index()
    except Exception:
        return pd.DataFrame(columns=["voltage_level_id", "country"])
    subs = subs.rename(columns={"id": "substation_id"})
    vls["substation_id"] = vls["substation_id"].astype(str)
    vls["id"] = vls["id"].astype(str)
    subs["substation_id"] = subs["substation_id"].astype(str)
    merged = vls.merge(subs, on="substation_id", how="left")
    return merged.rename(columns={"id": "voltage_level_id"})[
        ["voltage_level_id", "country"]
    ]


def losses_by_country(network: NetworkProxy) -> pd.Series:
    """Per-country active-power losses (MW) for lines + 2WT.

    Cross-border branches are split 50/50 between the two endpoint
    countries. VLs without a country fall back to ``"—"``. Empty
    Series when no branch has a finite p1/p2.
    """
    vl_country = build_vl_country_map(network)
    if vl_country.empty:
        return pd.Series(dtype=float)
    country_by_vl = dict(
        zip(vl_country["voltage_level_id"], vl_country["country"]),
    )

    def _country(vl_id) -> str:
        c = country_by_vl.get(vl_id)
        if c is None or (isinstance(c, float) and pd.isna(c)) or c == "":
            return "—"
        return c

    totals: dict[str, float] = {}
    for method in ("get_lines", "get_2_windings_transformers"):
        try:
            df = getattr(network, method)(
                attributes=["voltage_level1_id", "voltage_level2_id", "p1", "p2"],
            )
        except Exception:
            continue
        if df.empty:
            continue
        for _, row in df.iterrows():
            p1, p2 = row["p1"], row["p2"]
            if pd.isna(p1) or pd.isna(p2):
                continue
            loss = float(p1) + float(p2)
            c1 = _country(row["voltage_level1_id"])
            c2 = _country(row["voltage_level2_id"])
            if c1 == c2:
                totals[c1] = totals.get(c1, 0.0) + loss
            else:
                half = loss / 2.0
                totals[c1] = totals.get(c1, 0.0) + half
                totals[c2] = totals.get(c2, 0.0) + half

    if not totals:
        return pd.Series(dtype=float)
    return pd.Series(totals).sort_index()


def country_totals(network: NetworkProxy) -> pd.DataFrame:
    """Per-country target and actual generation/consumption (MW).

    Columns: :data:`COUNTRY_TOTALS_RAW_COLUMNS`. Target values
    (``target_p`` / ``p0``) are always populated; actuals (``-p`` for
    generators, ``p`` for loads) are NaN before any load flow.
    """
    vl_country = build_vl_country_map(network)
    if vl_country.empty:
        return pd.DataFrame(columns=COUNTRY_TOTALS_RAW_COLUMNS)

    def _aggregate(df: pd.DataFrame, value_col: str) -> pd.Series:
        if df.empty or value_col not in df.columns:
            return pd.Series(dtype=float)
        df2 = df.reset_index()
        df2["voltage_level_id"] = df2["voltage_level_id"].astype(str)
        vl_c = vl_country.copy()
        vl_c["voltage_level_id"] = vl_c["voltage_level_id"].astype(str)
        merged = df2.merge(vl_c, on="voltage_level_id", how="left")
        merged["country"] = merged["country"].fillna("—").replace("", "—")
        series = merged[value_col].dropna()
        if series.empty:
            return pd.Series(dtype=float)
        return merged.loc[series.index].groupby("country")[value_col].sum()

    try:
        gens = network.get_generators(
            attributes=["voltage_level_id", "target_p", "p"],
        )
    except Exception:
        gens = pd.DataFrame()
    try:
        loads = network.get_loads(
            attributes=["voltage_level_id", "p0", "p"],
        )
    except Exception:
        loads = pd.DataFrame()

    # Actual generation is ``-p`` (pypowsybl load-convention sign).
    gens_actual = gens.copy()
    if not gens_actual.empty and "p" in gens_actual.columns:
        gens_actual["p"] = -gens_actual["p"]

    gen_target = _aggregate(gens, "target_p")
    gen_actual = _aggregate(gens_actual, "p")
    cons_target = _aggregate(loads, "p0")
    cons_actual = _aggregate(loads, "p")

    countries = sorted(
        set(gen_target.index)
        | set(gen_actual.index)
        | set(cons_target.index)
        | set(cons_actual.index)
    )
    if not countries:
        return pd.DataFrame(columns=COUNTRY_TOTALS_RAW_COLUMNS)

    def _pick(series: pd.Series, c: str):
        if c in series.index:
            return float(series.loc[c])
        return float("nan")

    return pd.DataFrame({
        "country": countries,
        "generation_target_mw": [_pick(gen_target, c) for c in countries],
        "generation_actual_mw": [_pick(gen_actual, c) for c in countries],
        "consumption_target_mw": [_pick(cons_target, c) for c in countries],
        "consumption_actual_mw": [_pick(cons_actual, c) for c in countries],
    })


def build_component_counts(network: NetworkProxy) -> dict[str, int]:
    """Iterate :data:`COMPONENT_TYPES` and return ``{label → count}``,
    skipping components that don't exist in this network or whose
    count is 0."""
    counts: dict[str, int] = {}
    for label, method in COMPONENT_TYPES.items():
        try:
            df = getattr(network, method)()
        except Exception:
            continue
        count = len(df)
        if count > 0:
            counts[label] = count
    return counts


def build_metadata(network: NetworkProxy) -> OverviewMetadata:
    """Snapshot network identification — ``id``, ``name``, ``source_format``,
    ``case_date`` (ISO date string, empty when absent)."""
    network_id = str(network.id or "")
    name = str(network.name or "")
    source_format = str(network.source_format or "")
    case_date_obj = network.case_date
    case_date = ""
    if case_date_obj is not None:
        try:
            case_date = str(case_date_obj.date())
        except AttributeError:
            case_date = str(case_date_obj)
    return OverviewMetadata(
        network_id=network_id,
        name=name,
        source_format=source_format,
        case_date=case_date,
    )


# ---------------------------------------------------------------------------
# Single worker hop (non-Streamlit hosts use this)
# ---------------------------------------------------------------------------
def compute_overview_data(network: NetworkProxy) -> OverviewData:
    """Compute every Overview section in a single worker hop.

    Streamlit keeps its per-session ``_overview_cache`` keyed by
    ``(net_key, lf_gen)``; PySide6 and NiceGUI call this directly and
    re-fetch on network / load-flow changes (cheap on IEEE14, scales
    with components elsewhere).

    Implementation: unwrap the underlying pypowsybl Network and pass
    it (not the proxy) to the helpers inside ``run``. The helpers'
    ``getattr(network, method)(...)`` pattern works identically on
    both — but going through the proxy would re-submit to the same
    single-thread executor and deadlock.
    """
    raw = object.__getattribute__(network, "_obj")

    def _gather():
        return (
            build_metadata(raw),
            country_totals(raw),
            branch_losses_totals(raw),
            losses_by_country(raw),
            build_component_counts(raw),
        )

    metadata, cdf, losses, by_country, counts = run(_gather)
    return OverviewData(
        metadata=metadata,
        country_totals=cdf,
        losses=losses,
        losses_by_country=by_country,
        component_counts=counts,
    )


# ---------------------------------------------------------------------------
# Display helpers (rename + round)
# ---------------------------------------------------------------------------
def build_country_totals_display(df: pd.DataFrame) -> pd.DataFrame:
    """Rename + round country totals into the per-host display table.

    Returns an empty DataFrame with :data:`COUNTRY_TOTALS_DISPLAY_COLUMNS`
    headers when *df* is empty."""
    if df.empty:
        return pd.DataFrame(columns=COUNTRY_TOTALS_DISPLAY_COLUMNS)
    display = df.copy()
    for col in (
        "generation_target_mw", "generation_actual_mw",
        "consumption_target_mw", "consumption_actual_mw",
    ):
        display[col] = display[col].round(2)
    display.columns = COUNTRY_TOTALS_DISPLAY_COLUMNS
    return display


def country_totals_has_lf(df: pd.DataFrame) -> bool:
    """True when at least one ``*_actual_mw`` cell carries a value
    (i.e. an LF has run). Empty input → False."""
    if df.empty:
        return False
    actual = df[["generation_actual_mw", "consumption_actual_mw"]]
    return bool(actual.notna().any().any())


def build_losses_by_country_display(series: pd.Series) -> pd.DataFrame:
    """Return the per-country losses DataFrame ready for display
    (``Country`` + ``Losses (MW)``, MW rounded to 2 decimals)."""
    if series.empty:
        return pd.DataFrame(columns=LOSSES_BY_COUNTRY_COLUMNS)
    df = series.round(2).reset_index()
    df.columns = LOSSES_BY_COUNTRY_COLUMNS
    return df
