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
    merged = vls.merge(subs, on="substation_id", how="left")
    return merged.rename(columns={"id": "voltage_level_id"})[
        ["voltage_level_id", "country"]
    ]


def _country_totals(network) -> pd.DataFrame:
    """Return per-country totals of generation and consumption in MW.

    Columns: ``country``, ``generation_mw``, ``consumption_mw``. Generation
    uses generator ``target_p`` and consumption uses load ``p0``. Voltage
    levels whose substation has no country fall back to ``"—"``.
    """
    vl_country = _build_vl_country_map(network)
    if vl_country.empty:
        return pd.DataFrame(columns=["country", "generation_mw", "consumption_mw"])

    def _aggregate(df: pd.DataFrame, value_col: str) -> pd.Series:
        if df.empty or value_col not in df.columns:
            return pd.Series(dtype=float)
        merged = df.reset_index().merge(vl_country, on="voltage_level_id", how="left")
        merged["country"] = merged["country"].fillna("—").replace("", "—")
        return merged.groupby("country")[value_col].sum()

    try:
        gens = network.get_generators(attributes=["voltage_level_id", "target_p"])
    except Exception:
        gens = pd.DataFrame()
    try:
        loads = network.get_loads(attributes=["voltage_level_id", "p0"])
    except Exception:
        loads = pd.DataFrame()

    gen_by_country = _aggregate(gens, "target_p")
    load_by_country = _aggregate(loads, "p0")

    countries = sorted(set(gen_by_country.index) | set(load_by_country.index))
    if not countries:
        return pd.DataFrame(columns=["country", "generation_mw", "consumption_mw"])

    out = pd.DataFrame({
        "country": countries,
        "generation_mw": [float(gen_by_country.get(c, 0.0)) for c in countries],
        "consumption_mw": [float(load_by_country.get(c, 0.0)) for c in countries],
    })
    return out


def render_overview(network):
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Network ID", network.id)
    col2.metric("Name", network.name or "-")
    col3.metric("Format", network.source_format)
    col4.metric("Case Date", str(network.case_date.date()) if network.case_date else "-")

    st.subheader("Element Counts")
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
        counts_df = pd.DataFrame(
            {"Component": counts.keys(), "Count": counts.values()}
        )
        cols = st.columns(4)
        for i, (label, count) in enumerate(counts.items()):
            cols[i % 4].metric(label, count)

    st.subheader("Network Losses")
    losses = _branch_losses_totals(network)
    if not losses.get("_has_data"):
        st.info("No loss data available (run a load flow first).")
    else:
        lc1, lc2, lc3 = st.columns(3)
        lc1.metric("Total losses", f"{losses['total']:.2f} MW")
        lc2.metric("Line losses", f"{losses['lines']:.2f} MW")
        lc3.metric("Transformer losses", f"{losses['transformers']:.2f} MW")

    st.subheader("Generation and Consumption by Country")
    country_df = _country_totals(network)
    if country_df.empty:
        st.info("No generation or consumption data available.")
    else:
        display = country_df.copy()
        display["generation_mw"] = display["generation_mw"].round(2)
        display["consumption_mw"] = display["consumption_mw"].round(2)
        display["balance_mw"] = (
            display["generation_mw"] - display["consumption_mw"]
        ).round(2)
        display.columns = ["Country", "Generation (MW)", "Consumption (MW)",
                           "Balance (MW)"]
        st.dataframe(display, use_container_width=True, hide_index=True)
