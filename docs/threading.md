# Threading — pypowsybl worker

> Read this before touching anything that calls pypowsybl.

## Why it exists

pypowsybl 1.14 is a GraalVM native-image library. Its isolate binds permanently
to whichever thread first calls it. Streamlit spawns a **new ScriptRunner thread
on every rerun**, so a `Network` loaded on rerun N segfaults on rerun N+1 with no
Python traceback — just `Segmentation fault (core dumped)` and the server dies.

## The mechanism — `powsybl_worker.py`

```
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pypowsybl")
```

One persistent thread. Every pypowsybl operation goes through it.

### `run(fn, *args, **kwargs)`

Submits `fn` to the executor and blocks until done. Returns the result.

```python
# correct
result = run(lambda: network.get_lines())

# wrong — this runs on the ScriptRunner thread
result = network.get_lines()
```

### `NetworkProxy`

Wraps a pypowsybl `Network` (or any pypowsybl object) so that every
attribute access and method call is automatically routed through `run()`.

```python
proxy = NetworkProxy(raw_network)
proxy.get_generators()   # runs on the worker thread
proxy.id                 # also runs on the worker thread
```

`_maybe_wrap` detects pypowsybl return values by checking
`type(value).__module__.startswith("pypowsybl")` and wraps them automatically.
This is how chained access like `svg.svg` stays safe.

## Rules for new code

| Situation | What to do |
|---|---|
| Need to call pypowsybl | Import inside function body, call via `run()` or through a `NetworkProxy` |
| Need a pypowsybl return value in UI code | Extract native Python values (str, DataFrame, bytes) **inside** `run()` |
| Have a `NetworkProxy` | Call methods directly — the proxy handles dispatch |
| Chaining `.attr` on a pypowsybl result | Must stay inside `run()` or be accessed via the proxy |

## Common mistakes

**Top-level import** — fails immediately or binds the wrong thread:
```python
# wrong
import pypowsybl.network as pn          # module-load thread
from pypowsybl.network import NadParameters  # same problem
```

`NadParameters` and `SldParameters` are plain Python data classes — they don't
bind the isolate. They are imported at the top of function bodies in `diagrams.py`
as a matter of convention, not strict necessity.

**Returning a raw pypowsybl handle** — segfaults on the next rerun:
```python
def _load():
    import pypowsybl.network as pn
    return pn.load_from_binary_buffer(buf)

network = run(_load)          # wrong — raw Network, not wrapped
network = NetworkProxy(run(_load))  # correct
```

**Nesting `run()` calls** — deadlocks the single-threaded executor:
```python
# wrong — called from inside another run() body
run(lambda: run(lambda: ...))
```
The proxy's `__getattr__` already calls `run()`. Do not wrap proxy attribute
accesses in another `run()`.

## Detecting regressions

```bash
grep -n "^import pypowsybl\|^from pypowsybl" iidm_viewer/*.py
```

Any match is a bug. All pypowsybl imports must be inside function bodies.

See also [AGENTS.md §4](../AGENTS.md#4-troubleshooting-segfaults) for the full
troubleshooting checklist.
