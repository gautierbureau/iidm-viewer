import streamlit as st

from iidm_viewer.powsybl_worker import run


def _get_provider_params_info():
    """Return the provider parameters DataFrame (cached)."""
    cache = st.session_state.setdefault("_lf_provider_info", {})
    if "df" not in cache:
        def _fetch():
            import pypowsybl.loadflow as lf
            return lf.get_provider_parameters()
        cache["df"] = run(_fetch)
    return cache["df"]


def get_lf_parameters():
    """Return the current (generic_params_dict, provider_params_dict) from session state."""
    return (
        st.session_state.get("_lf_generic_params", {}),
        st.session_state.get("_lf_provider_params", {}),
    )


# Generic parameter definitions: (name, type, default, description, options)
_GENERIC_PARAMS = [
    ("voltage_init_mode", "enum", "UNIFORM_VALUES",
     "Voltage initialization mode",
     ["UNIFORM_VALUES", "PREVIOUS_VALUES", "DC_VALUES"]),
    ("transformer_voltage_control_on", "bool", False,
     "Enable transformer voltage control"),
    ("phase_shifter_regulation_on", "bool", False,
     "Enable phase-shifter regulation"),
    ("use_reactive_limits", "bool", True,
     "Use generator reactive limits"),
    ("shunt_compensator_voltage_control_on", "bool", False,
     "Enable shunt compensator voltage control"),
    ("distributed_slack", "bool", True,
     "Distribute slack on generators"),
    ("balance_type", "enum", "PROPORTIONAL_TO_GENERATION_P_MAX",
     "Active power balance type",
     ["PROPORTIONAL_TO_GENERATION_P_MAX", "PROPORTIONAL_TO_GENERATION_P",
      "PROPORTIONAL_TO_GENERATION_REMAINING_MARGIN",
      "PROPORTIONAL_TO_GENERATION_PARTICIPATION_FACTOR",
      "PROPORTIONAL_TO_LOAD", "PROPORTIONAL_TO_CONFORM_LOAD"]),
    ("dc_use_transformer_ratio", "bool", True,
     "Use transformer ratio in DC mode"),
    ("hvdc_ac_emulation", "bool", True,
     "Enable HVDC AC emulation"),
    ("read_slack_bus", "bool", True,
     "Read slack bus from network"),
    ("write_slack_bus", "bool", True,
     "Write slack bus to network"),
    ("dc_power_factor", "float", 1.0,
     "Power factor for DC load flow"),
]


def _render_generic_tab():
    """Render widgets for generic LF parameters. Returns dict of changed values."""
    current = st.session_state.get("_lf_generic_params", {})
    new_values = {}

    for param_def in _GENERIC_PARAMS:
        name = param_def[0]
        ptype = param_def[1]
        default = param_def[2]
        desc = param_def[3]

        val = current.get(name, default)
        key = f"lf_gen_{name}"

        if ptype == "bool":
            new_values[name] = st.checkbox(desc, value=bool(val), key=key)
        elif ptype == "enum":
            options = param_def[4]
            idx = options.index(str(val)) if str(val) in options else 0
            new_values[name] = st.selectbox(desc, options=options, index=idx, key=key)
        elif ptype == "float":
            new_values[name] = st.number_input(desc, value=float(val), format="%g", key=key)

    return new_values


def _render_provider_tab():
    """Render widgets for OpenLoadFlow provider parameters. Returns dict of changed values."""
    info_df = _get_provider_params_info()
    current = st.session_state.get("_lf_provider_params", {})
    new_values = {}

    categories = info_df["category_key"].unique().tolist()
    for category in sorted(categories):
        cat_params = info_df[info_df["category_key"] == category]
        with st.expander(category, expanded=False):
            for name, row in cat_params.iterrows():
                ptype = row["type"]
                default = row["default"]
                desc = row["description"]
                possible = row.get("possible_values", "")
                val = current.get(name, default)
                key = f"lf_prov_{name}"

                if ptype == "BOOLEAN":
                    bool_val = str(val).lower() == "true"
                    new_values[name] = st.checkbox(
                        f"{name}", value=bool_val, key=key,
                        help=desc,
                    )
                elif ptype == "INTEGER":
                    try:
                        int_val = int(val)
                    except (ValueError, TypeError):
                        int_val = 0
                    new_values[name] = st.number_input(
                        f"{name}", value=int_val, step=1, key=key,
                        help=desc,
                    )
                elif ptype == "DOUBLE":
                    try:
                        float_val = float(val)
                    except (ValueError, TypeError):
                        float_val = 0.0
                    new_values[name] = st.number_input(
                        f"{name}", value=float_val, format="%g", key=key,
                        help=desc,
                    )
                elif ptype == "STRING" and possible is not None:
                    if isinstance(possible, str) and possible.startswith("[") and possible.endswith("]"):
                        # pypowsybl returns "[VAL1, VAL2]" as a string
                        options = [v.strip() for v in possible[1:-1].split(",") if v.strip()]
                    elif hasattr(possible, '__iter__') and not isinstance(possible, str):
                        try:
                            options = list(possible)
                        except (TypeError, ValueError):
                            options = []
                    else:
                        options = []
                    if options:
                        idx = options.index(str(val)) if str(val) in options else 0
                        new_values[name] = st.selectbox(
                            f"{name}", options=options, index=idx, key=key,
                            help=desc,
                        )
                    else:
                        new_values[name] = st.text_input(
                            f"{name}", value=str(val) if val else "", key=key,
                            help=desc,
                        )
                else:
                    new_values[name] = st.text_input(
                        f"{name}", value=str(val) if val else "", key=key,
                        help=desc,
                    )

    return new_values


@st.dialog("Load Flow Parameters", width="large")
def show_lf_parameters_dialog():
    tab_generic, tab_provider = st.tabs(["Generic Parameters", "OpenLoadFlow Parameters"])

    with tab_generic:
        generic_values = _render_generic_tab()

    with tab_provider:
        provider_values = _render_provider_tab()

    if st.button("Save", key="lf_params_save"):
        st.session_state["_lf_generic_params"] = generic_values
        # Only store provider params that differ from defaults
        info_df = _get_provider_params_info()
        changed_provider = {}
        for name, val in provider_values.items():
            if name in info_df.index:
                default = info_df.at[name, "default"]
                if str(val).lower() != str(default).lower():
                    changed_provider[name] = val
        st.session_state["_lf_provider_params"] = changed_provider
        st.rerun()
