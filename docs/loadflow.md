# Load Flow

## Entry points

| Location | Trigger |
|---|---|
| `app.py` sidebar | "Run AC Load Flow" button |
| `data_explorer.py` | "Apply N changes & Run Load Flow" button |

## Execution — `state.run_loadflow(network)`

```python
def run_loadflow(network):
    raw = object.__getattribute__(network, "_obj")   # unwrap proxy
    generic, provider = get_lf_parameters()           # read session state on main thread

    def _run_ac():
        import pypowsybl.loadflow as lf
        params = lf.Parameters(**generic)
        if provider:
            params.provider_parameters = {k: str(v) for k, v in provider.items()}
        return lf.run_ac(raw, parameters=params)

    results = run(_run_ac)
    st.session_state.pop("_vl_lookup_cache", None)   # invalidate caches
    return results
```

Parameters are read on the main thread **before** entering `run()` because
`st.session_state` is not safe to access from the worker thread.

`results` is a list of `ComponentResult`-like objects with a `.status` field.
`results[0].status.name` gives a string such as `"CONVERGED"`.

## Parameters — `lf_parameters.py`

Stored in two session-state keys:

| Key | Content |
|---|---|
| `_lf_generic_params` | `dict` of `lf.Parameters` kwargs (empty = all defaults) |
| `_lf_provider_params` | `dict` of provider-specific overrides, strings only (empty = all defaults) |

`get_lf_parameters()` returns `(generic, provider)` from those keys.

The parameter dialog (`show_lf_parameters_dialog`) is a `@st.dialog` that renders
two tabs:
- **Generic Parameters** — 11 well-known `lf.Parameters` fields via `_GENERIC_PARAMS`
- **OpenLoadFlow Parameters** — fetched from `lf.get_provider_parameters()`, grouped by
  `category_key`, rendered as checkboxes / number inputs / selectboxes

Only parameters that differ from their defaults are stored in `_lf_provider_params`.

## Status display pattern

The sidebar button shows status directly — no rerun follows:
```python
if st.button("Run AC Load Flow"):
    results = run_loadflow(network)
    status = results[0].status.name if results else "UNKNOWN"
    if status == "CONVERGED":
        st.success(f"Load flow: {status}")
    else:
        st.warning(f"Load flow: {status}")
```

The Data Explorer button must call `st.rerun()` afterward to refresh the table,
which would erase any `st.success/warning` rendered in the same pass. The
workaround: store status in session state before the rerun, pop and display it
at the top of `render_data_explorer` on the next pass:

```python
# Before rerun:
st.session_state["_lf_status_message"] = (f"Load flow: {status}", status == "CONVERGED")
st.rerun()

# At top of render_data_explorer:
lf_status = st.session_state.pop("_lf_status_message", None)
if lf_status:
    status_text, is_success = lf_status
    if is_success:
        st.success(status_text)
    else:
        st.warning(status_text)
```

## Session-state keys summary

| Key | Set by | Read by |
|---|---|---|
| `_lf_generic_params` | `show_lf_parameters_dialog` | `get_lf_parameters` |
| `_lf_provider_params` | `show_lf_parameters_dialog` | `get_lf_parameters` |
| `_lf_provider_info` | `_get_provider_params_info` | same |
| `_lf_status_message` | `data_explorer.py` (after apply+LF) | `render_data_explorer` (next rerun) |
| `_vl_lookup_cache` | `build_vl_lookup` | cleared by `run_loadflow` / `update_components` |
