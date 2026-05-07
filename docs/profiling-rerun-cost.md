# Profiling per-rerun cost on large networks

Open investigation. The question: on a large network, VL-selector
navigation in the SLD/NAD tabs feels less than instant, while clicking
a breaker inside the SLD feels snappier — even though both trigger a
full Streamlit rerun. Where is the time actually going?

## What we already know

A static audit (see [worker-round-trips.md](worker-round-trips.md))
says every tab is **0 worker round-trips per warm rerun**. The tab
caches are real:

- SLD: `_sld_cache` keyed by `container_id` (`diagrams.py:348`).
- NAD: `_nad_cache` keyed by `(selected_vl, depth)` (`diagrams.py:194`).
- Busbar sections / bus-breaker topology: `_bbs_cache`, `_bbt_cache`.
- Overview: `_overview_cache` keyed by `(net_key, lf_gen)` —
  `_country_totals`, `_branch_losses_totals`, `_losses_by_country`,
  and the `COMPONENT_TYPES` count loop all sit inside this wrapper
  (`network_info.py:206`).
- Maps: `_map_data_cache`, `_voltage_map_cache`, `_injection_map_cache`.
- Data Explorer / Operational Limits / Pmax / Voltage Analysis /
  Reactive Curves / Extensions Explorer all delegate to `caches.py`.

So if there is a real cost on warm reruns, it's either:

1. A pypowsybl call we did not catch in the audit.
2. CPU-bound Python work (parsing, merging, rendering) that runs on
   every rerun even on a cache hit.
3. Streamlit / browser overhead unrelated to pypowsybl.

The point of profiling is to tell which.

## How to investigate

### 1. Instrument every worker round-trip

Patch `iidm_viewer/powsybl_worker.py` to log every `run()` call with
the caller, function name, and elapsed time. Keep this on a branch —
do not commit. Suggested patch:

```python
import time, traceback, threading, os
_LOG = open(os.environ.get("RT_LOG", "/tmp/rt.log"), "a", buffering=1)
_lock = threading.Lock()

def run(fn, *args, **kwargs):
    fn_name = getattr(fn, "__qualname__", repr(fn))
    # Caller frame: skip the wrapper, find the first frame outside this file.
    stack = traceback.extract_stack()
    caller = next(
        (f"{f.filename}:{f.lineno}" for f in reversed(stack[:-1])
         if "powsybl_worker.py" not in f.filename),
        "?",
    )
    t0 = time.perf_counter()
    try:
        return _executor.submit(fn, *args, **kwargs).result()
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000
        with _lock:
            _LOG.write(f"{dt_ms:7.2f}ms  {fn_name:40s}  {caller}\n")
```

Run the app, do a single VL switch, then `wc -l /tmp/rt.log` and inspect.

**Expectations on a warm cache hit for VL switch:**

- Sidebar `vl_selector` rerun: ~0 RT.
- SLD tab rerun on a previously-visited VL: ~0 RT.
- NAD tab rerun on a previously-visited `(vl, depth)`: ~0 RT.

Anything you see in `/tmp/rt.log` during a warm-cache VL switch is a
cache miss or an uncached path. Group the log by caller (`awk '{print
$NF}' /tmp/rt.log | sort | uniq -c`) — the top entries are the leak.

### 2. Time the rerun end-to-end from Python's side

Add a one-liner at the very top of `app.py`:

```python
import time, streamlit as st
_t0 = time.perf_counter()
st.session_state["_rerun_started_at"] = _t0
```

…and at the bottom of `app.py` before the script returns:

```python
import time
print(f"rerun: {(time.perf_counter() - st.session_state['_rerun_started_at'])*1000:.0f} ms")
```

Compare wall-clock time for:

- Switching to a **previously visited** VL (warm `_sld_cache`).
- Switching to a **never-visited** VL (cold `_sld_cache`).
- Toggling a breaker on the current VL (`_sld_cache` popped by
  `invalidate_on_topology_change`, regenerates from scratch).

If the warm-revisit case is still slow, time is going to non-pypowsybl
work. If only the cold case is slow, that is just the inherent SLD
generation cost and the only fix is precomputation (out of scope).

### 3. Instrument SLD / NAD render specifically

Wrap the cache lookup blocks in `diagrams.py` (`render_sld_tab` around
line 348, `render_nad_tab` around line 194) with timing:

```python
import time
t0 = time.perf_counter()
cached_sld = sld_cache.get(container_id)
hit = cached_sld is not None
# ... existing code ...
print(f"sld {container_id}: hit={hit} {(time.perf_counter()-t0)*1000:.0f} ms")
```

Do the same in `_render_bus_legend` (line 132) and
`_resolve_bus_colors` (line 68) — see option 3 below.

### 4. Profile a single rerun with cProfile

For a deeper view of CPU-bound work on cache hits:

```python
# top of app.py, behind an env flag
import cProfile, atexit, os
if os.environ.get("PROFILE_RERUN"):
    pr = cProfile.Profile(); pr.enable()
    atexit.register(lambda: pr.dump_stats("/tmp/rerun.prof"))
```

Then `PROFILE_RERUN=1 myenv/bin/streamlit run iidm_viewer/app.py`,
do one VL switch, kill the server, and inspect with `snakeviz
/tmp/rerun.prof` or `python -m pstats /tmp/rerun.prof`.

### 5. End-to-end with Playwright

Use the harness from `AGENTS.md` §2 to drive the page and record
`page.evaluate("() => performance.now()")` before/after the VL change.
That captures total user-visible latency including the Streamlit
WebSocket round-trip and the frontend re-render — neither of which is
visible from Python timings.

## Lead worth keeping: bus-legend SVG re-parse on cache hit

Even when `_sld_cache` hits, `render_sld_tab` still calls
`_render_bus_legend(network, selected_vl, svg)` (`diagrams.py:375`)
which calls `_resolve_bus_colors(network, selected_vl, svg)` →
`_parse_sld_palette(svg)` + `_parse_sld_busbar_indices(svg)` against
the **whole SVG string** on every rerun. On large substations the SVG
is hundreds of kB and the regex `finditer` walks it twice.

The SVG content is fixed for a given `container_id` — same as what is
cached in `_sld_cache[container_id]`. The parsed palette + busbar
mapping could be cached alongside the SVG, e.g.:

```python
sld_cache[container_id] = (svg, metadata, palette, busbars)
```

…and `_resolve_bus_colors` could accept the pre-parsed dicts instead
of re-running the regex. The same applies to `_get_busbar_sections` /
`_get_bbt_buses` lookups inside `_resolve_bus_colors` — those *are*
cached, but the loop building `bb_to_bus` runs on every rerun.

This is the single concrete win confirmed from static reading. Worth
implementing if profiling shows `_resolve_bus_colors` time is
material on large SVGs; otherwise leave it.

## Files referenced

- `iidm_viewer/powsybl_worker.py` — `run()` entry point to instrument.
- `iidm_viewer/diagrams.py` — SLD/NAD render + bus-legend hot path.
- `iidm_viewer/caches.py` — cache layer; check `_sld_cache`,
  `_nad_cache` invalidation if profiling shows unexpected cache misses.
- `docs/worker-round-trips.md` — the static audit the profiling
  should confirm or contradict.
