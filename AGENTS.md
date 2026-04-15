# AGENTS.md

Streamlit + pypowsybl IIDM viewer. Read section 1 before changing anything
that touches pypowsybl; read section 2 before claiming any such change
works.

## 1. The one rule you cannot forget

**Never import `pypowsybl` at module top. Never call pypowsybl directly
from a Streamlit ScriptRunner thread. Route every pypowsybl call through
`iidm_viewer/powsybl_worker.py`.**

pypowsybl 1.14 is a GraalVM native-image library. Its isolate binds to
the first thread that touches it. Streamlit spawns a fresh ScriptRunner
thread per rerun, so a Network loaded on one rerun segfaults when used
on the next — no Python traceback, just `Segmentation fault (core dumped)`
and the whole server dies.

The existing mitigation:

- `powsybl_worker.py` owns a module-level `ThreadPoolExecutor(max_workers=1)`.
- `run(fn, *args, **kwargs)` executes `fn` on that worker.
- `NetworkProxy` wraps a Network and dispatches every attribute/method
  access to the worker. It auto-wraps pypowsybl return values (e.g.
  `SldResult`, `NadResult`) so chained access like `svg.svg` stays on
  the worker.
- `state.load_network` loads through the worker and returns a
  `NetworkProxy`.

When adding pypowsybl-touching code:

1. Import pypowsybl only inside function bodies, never at module top.
2. Call pypowsybl through a `NetworkProxy` or explicitly via `run(...)`.
3. Do not return raw pypowsybl handles to the Streamlit thread — either
   wrap with `NetworkProxy`, or extract native Python values (str,
   DataFrame, bytes) inside the worker call.

## 2. Testing the Streamlit app

### Unit tests (necessary but not sufficient)

```
myenv/bin/python -m pytest tests/
```

These exercise `load_network` and a Streamlit `AppTest` rerun. They
**do not** catch the thread-affinity segfault because `AppTest` runs the
script inline on the main thread. Every pass here means nothing about
whether the real app crashes.

### End-to-end run (required for any pypowsybl-touching change)

The only way to catch segfaults is a real Streamlit server + a real
browser. Playwright is installed in `myenv`. The canonical recipe:

```
# 1. start streamlit in the background
myenv/bin/streamlit run iidm_viewer/app.py \
  --server.headless=true --server.port=8777 \
  --server.fileWatcherType=none > /tmp/streamlit.log 2>&1 &
SPID=$!
sleep 5

# 2. drive it with playwright
myenv/bin/python - <<'PY'
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page()
        await page.goto("http://localhost:8777/")
        await page.wait_for_load_state("networkidle", timeout=15000)
        await page.wait_for_timeout(2000)

        # upload
        fi = await page.locator('input[type="file"]').all()
        await fi[0].set_input_files("test_ieee14.xiidm")
        await page.wait_for_timeout(8000)

        # switch voltage level — forces a post-upload rerun on a new thread
        sel = page.locator('div[data-baseweb="select"]').first
        await sel.click()
        await page.locator('li[role="option"]').nth(4).click()
        await page.wait_for_timeout(4000)

        # diagrams
        await page.locator('[role="tab"]', has_text="Network Area Diagram").first.click()
        await page.wait_for_timeout(5000)
        await page.locator('[role="tab"]', has_text="Single Line Diagram").first.click()
        await page.wait_for_timeout(5000)

        await b.close()

asyncio.run(main())
PY

# 3. the server MUST still be alive. Exit 139 = SIGSEGV = regression.
ps -p $SPID -o pid,stat,comm
kill $SPID
```

Minimum steps every end-to-end run must perform:

- Upload `test_ieee14.xiidm`.
- **Select a different voltage level** (this is the operation that
  forces a post-upload ScriptRunner rerun on a fresh thread and trips
  the thread-affinity bug).
- Open the NAD tab and the SLD tab.
- Switch at least one Data Explorer component type.
- Confirm the streamlit process is still alive. If `ps` shows it gone,
  or bash printed `Segmentation fault (core dumped)`, the
  thread-affinity fix has regressed — see section 1.

## 3. Architecture

- `app.py` — entry point: sidebar (file uploader + VL selector) and four
  tabs (Overview, NAD, SLD, Data Explorer).
- `state.py` — session state, `load_network` (wraps result in
  `NetworkProxy`), voltage-level dataframe helpers.
- `powsybl_worker.py` — the thread-isolation mechanism. Touch
  carefully.
- `components.py` — `vl_selector`, `render_svg`.
- `network_info.py` — Overview tab and the `COMPONENT_TYPES` registry.
- `diagrams.py` — NAD and SLD tabs. Imports `NadParameters` /
  `SldParameters` lazily inside the render functions (they are plain
  Python classes, but keeping pypowsybl imports lazy is the repo rule).
- `data_explorer.py` — tabular view with VL filter and ID substring
  filter.

Everything pypowsybl-facing flows: UI code → `NetworkProxy` → worker
thread → pypowsybl → result wrapped and handed back.

## 4. Troubleshooting segfaults

Symptom: `Segmentation fault (core dumped)` during normal app use.

Checks, in order:

1. `grep -n "^import pypowsybl\|^from pypowsybl" iidm_viewer/*.py` —
   any match is the bug.
2. Any new helper calling pypowsybl directly without going through
   `run(...)` or a `NetworkProxy`?
3. Any code reading an attribute of an `SldResult` / `NadResult` /
   other pypowsybl object outside the worker? It must come back
   wrapped by `NetworkProxy` or be pulled out inside `run(...)`.

Things that look like fixes but aren't:

- Zipping uploads and using `load_from_binary_buffer` — this was an
  earlier workaround that did not address thread affinity. The
  zip-packaging stays only as a convenience for `.zip` uploads.
- `st.cache_resource` — the cache still hands the object out to
  whichever thread hits the cache next.
- Eagerly calling `pn.create_empty()` at module top to "warm up" the
  isolate — it binds to the module-load thread, not the ScriptRunner
  thread that will do the real work later.

## 5. Commands cheatsheet

```
# venv
source myenv/bin/activate

# tests
myenv/bin/python -m pytest tests/ -x

# dev run
myenv/bin/streamlit run iidm_viewer/app.py

# end-to-end (see section 2 for the full recipe)
```
