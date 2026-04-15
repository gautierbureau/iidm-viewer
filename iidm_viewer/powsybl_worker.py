"""Single-threaded executor that owns every pypowsybl call.

pypowsybl 1.14 is a GraalVM native-image library whose isolate binds to the
thread that first touches it. Streamlit spawns a fresh ScriptRunner thread
per rerun, so a Network loaded on one rerun segfaults when used on the next.
Routing every pypowsybl operation through one persistent worker thread keeps
the isolate/thread affinity stable for the lifetime of the process.
"""
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pypowsybl")


def run(fn, *args, **kwargs):
    """Execute ``fn(*args, **kwargs)`` on the pypowsybl worker thread."""
    return _executor.submit(fn, *args, **kwargs).result()


class NetworkProxy:
    """Wraps a pypowsybl Network so every attribute/method runs on the worker.

    Any callable result also wraps its return value if it looks like another
    pypowsybl object (e.g. SldResult, NadResult) so chained ``.svg`` access
    stays on the worker thread too.
    """

    __slots__ = ("_obj",)

    def __init__(self, obj):
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name):
        obj = object.__getattribute__(self, "_obj")
        attr = run(getattr, obj, name)
        if callable(attr):
            @wraps(attr)
            def wrapper(*args, **kwargs):
                return _maybe_wrap(run(attr, *args, **kwargs))
            return wrapper
        return _maybe_wrap(attr)

    def __repr__(self):
        obj = object.__getattribute__(self, "_obj")
        return f"NetworkProxy({obj!r})"


def _maybe_wrap(value):
    module = type(value).__module__ or ""
    if module.startswith("pypowsybl"):
        return NetworkProxy(value)
    return value
