import streamlit as st
import pandas as pd


COMPONENT_TYPES = {
    "Substations": "get_substations",
    "Voltage Levels": "get_voltage_levels",
    "Buses": "get_buses",
    "Busbar Sections": "get_busbar_sections",
    "Generators": "get_generators",
    "Loads": "get_loads",
    "Lines": "get_lines",
    "2-Winding Transformers": "get_2_windings_transformers",
    "3-Winding Transformers": "get_3_windings_transformers",
    "Switches": "get_switches",
    "Shunt Compensators": "get_shunt_compensators",
    "Static VAR Compensators": "get_static_var_compensators",
    "HVDC Lines": "get_hvdc_lines",
    "VSC Converter Stations": "get_vsc_converter_stations",
    "LCC Converter Stations": "get_lcc_converter_stations",
    "Batteries": "get_batteries",
    "Dangling Lines": "get_dangling_lines",
    "Tie Lines": "get_tie_lines",
}


def _branch_losses_totals(network) -> dict[str, float]:
    """Return active-power losses summed over lines and 2-winding transformers.

    Uses p1 + p2 (pypowsybl sign convention). Keys: ``lines``, ``transformers``,
    ``total``. Entries whose p1 or p2 is NaN (i.e. no load flow) are skipped so
    the totals only reflect branches with a solved flow.
    """
    out: dict[str, float] = {"lines": 0.0, "transformers": 0.0, "total": 0.0}
    any_valid = False
    for key, method in [
        ("lines", "get_lines"),
        ("transformers", "get_2_windings_transformers"),
    ]:
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
    out["_has_data"] = any_valid  # type: ignore[assignment]
    return out


def _build_vl_country_map(network) -> pd.DataFrame:
    """VL id → country, built without session-state caching (test-friendly)."""
    try:
        vls = network.get_voltage_levels(
            attributes=["substation_id"]
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


def _losses_by_country(network) -> pd.Series:
    """Return per-country active-power losses (MW) for lines + 2WT.

    A branch contributes its full ``p1 + p2`` to its country when both ends
    live in the same country; cross-border branches are split 50/50 between
    the two countries. VLs without a country fall back to ``"—"``.
    Returns an empty Series when no branch has finite p1/p2 (no load flow).
    """
    vl_country = _build_vl_country_map(network)
    if vl_country.empty:
        return pd.Series(dtype=float)
    country_by_vl = dict(zip(vl_country["voltage_level_id"], vl_country["country"]))

    def _country(vl_id: str) -> str:
        c = country_by_vl.get(vl_id)
        if c is None or (isinstance(c, float) and pd.isna(c)) or c == "":
            return "—"
        return c

    totals: dict[str, float] = {}
    for method in ("get_lines", "get_2_windings_transformers"):
        try:
            df = getattr(network, method)(
                attributes=["voltage_level1_id", "voltage_level2_id", "p1", "p2"]
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


def _country_totals(network) -> pd.DataFrame:
    """Return per-country target and actual generation/consumption in MW.

    Columns: ``country``, ``generation_target_mw``, ``generation_actual_mw``,
    ``consumption_target_mw``, ``consumption_actual_mw``. Target values come
    from generator ``target_p`` and load ``p0`` (always populated). Actual
    values come from generator ``-p`` and load ``p`` (NaN before any load
    flow). Voltage levels whose substation has no country fall back to ``"—"``.
    """
    vl_country = _build_vl_country_map(network)
    cols = ["country", "generation_target_mw", "generation_actual_mw",
            "consumption_target_mw", "consumption_actual_mw"]
    if vl_country.empty:
        return pd.DataFrame(columns=cols)

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
            attributes=["voltage_level_id", "target_p", "p"]
        )
    except Exception:
        gens = pd.DataFrame()
    try:
        loads = network.get_loads(
            attributes=["voltage_level_id", "p0", "p"]
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
        return pd.DataFrame(columns=cols)

    def _pick(series: pd.Series, c: str):
        if c in series.index:
            return float(series.loc[c])
        return float("nan")

    out = pd.DataFrame({
        "country": countries,
        "generation_target_mw": [_pick(gen_target, c) for c in countries],
        "generation_actual_mw": [_pick(gen_actual, c) for c in countries],
        "consumption_target_mw": [_pick(cons_target, c) for c in countries],
        "consumption_actual_mw": [_pick(cons_actual, c) for c in countries],
    })
    return out


def render_overview(network):
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Network ID", network.id)
    col2.metric("Name", network.name or "-")
    col3.metric("Format", network.source_format)
    col4.metric("Case Date", str(network.case_date.date()) if network.case_date else "-")

    st.subheader("Generation and Consumption by Country")
    country_df = _country_totals(network)
    if country_df.empty:
        st.info("No generation or consumption data available.")
    else:
        display = country_df.copy()
        for col in ("generation_target_mw", "generation_actual_mw",
                    "consumption_target_mw", "consumption_actual_mw"):
            display[col] = display[col].round(2)
        display.columns = [
            "Country",
            "Gen target (MW)", "Gen actual (MW)",
            "Load target (MW)", "Load actual (MW)",
        ]
        if display[["Gen actual (MW)", "Load actual (MW)"]].isna().all(axis=None):
            st.caption("Actual values populate once a load flow has run.")
        st.dataframe(display, use_container_width=True, hide_index=True)

    st.subheader("Network Losses")
    losses = _branch_losses_totals(network)
    if not losses.get("_has_data"):
        st.info("No loss data available (run a load flow first).")
    else:
        lc1, lc2, lc3 = st.columns(3)
        lc1.metric("Total losses", f"{losses['total']:.2f} MW")
        lc2.metric("Line losses", f"{losses['lines']:.2f} MW")
        lc3.metric("Transformer losses", f"{losses['transformers']:.2f} MW")

        by_country = _losses_by_country(network)
        if not by_country.empty:
            losses_df = by_country.round(2).reset_index()
            losses_df.columns = ["Country", "Losses (MW)"]
            st.caption("Losses by country — cross-border branches split 50/50.")
            st.dataframe(losses_df, use_container_width=True, hide_index=True)

    st.subheader("Component Statistics")
    with st.expander("Component statistics", expanded=False):
        counts = {}
        for label, method in COMPONENT_TYPES.items():
            try:
                df = getattr(network, method)()
                count = len(df)
                if count > 0:
                    counts[label] = count
            except Exception:
                pass

        if counts:
            cols = st.columns(4)
            for i, (label, count) in enumerate(counts.items()):
                cols[i % 4].metric(label, count)
        else:
            st.info("No components found in this network.")
