"""Guards the thread-affinity contract documented in AGENTS.md section 1.

These tests assert that every pypowsybl call funnels through a single
persistent worker thread, because letting the ScriptRunner thread touch
pypowsybl directly segfaults the whole process.
"""
import threading

import pandas as pd
import pytest

from iidm_viewer.powsybl_worker import NetworkProxy, _maybe_wrap, run


def _thread_name():
    return threading.current_thread().name


def test_run_executes_on_dedicated_worker_thread():
    caller = threading.current_thread().name
    worker = run(_thread_name)
    assert worker != caller
    assert worker.startswith("pypowsybl")


def test_run_is_single_threaded_across_calls():
    names = {run(_thread_name) for _ in range(10)}
    assert len(names) == 1, f"worker pool must be single-threaded, got {names}"


def test_run_propagates_return_value():
    assert run(lambda: 2 + 2) == 4


def test_run_propagates_exceptions():
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        run(lambda: (_ for _ in ()).throw(Boom("nope")))


def test_run_forwards_args_and_kwargs():
    assert run(lambda a, b, c=0: (a, b, c), 1, 2, c=3) == (1, 2, 3)


class _Fake:
    __module__ = "pypowsybl.fake"

    def __init__(self):
        self.value = 42
        self.touched_on = None

    def compute(self, x):
        self.touched_on = threading.current_thread().name
        return x * 2


class _Native:
    __module__ = "builtins"


def test_maybe_wrap_wraps_pypowsybl_modules():
    wrapped = _maybe_wrap(_Fake())
    assert isinstance(wrapped, NetworkProxy)


def test_maybe_wrap_passes_through_native_values():
    assert _maybe_wrap(42) == 42
    assert _maybe_wrap("hi") == "hi"
    df = pd.DataFrame({"a": [1]})
    assert _maybe_wrap(df) is df
    assert _maybe_wrap(_Native()) is not None  # not wrapped


def test_network_proxy_reads_attribute_on_worker():
    fake = _Fake()
    proxy = NetworkProxy(fake)
    assert proxy.value == 42


def test_network_proxy_dispatches_method_to_worker():
    fake = _Fake()
    proxy = NetworkProxy(fake)
    result = proxy.compute(21)
    assert result == 42
    assert fake.touched_on is not None
    assert fake.touched_on != threading.current_thread().name
    assert fake.touched_on.startswith("pypowsybl")


def test_network_proxy_wraps_pypowsybl_return_values():
    class Owner:
        __module__ = "pypowsybl.owner"

        def child(self):
            return _Fake()

    proxy = NetworkProxy(Owner())
    child_proxy = proxy.child()
    assert isinstance(child_proxy, NetworkProxy)


def test_network_proxy_repr_includes_wrapped_object():
    proxy = NetworkProxy(_Fake())
    assert "NetworkProxy" in repr(proxy)


def test_maybe_wrap_none_is_not_wrapped():
    assert _maybe_wrap(None) is None


def test_maybe_wrap_native_container_passes_through():
    payload = [1, 2, 3]
    assert _maybe_wrap(payload) is payload
    mapping = {"a": 1}
    assert _maybe_wrap(mapping) is mapping


def test_maybe_wrap_submodule_of_pypowsybl_is_wrapped():
    class _DeepFake:
        __module__ = "pypowsybl.network.sub.nested"

    assert isinstance(_maybe_wrap(_DeepFake()), NetworkProxy)


def test_network_proxy_does_not_rebind_on_setattr_via_object():
    """__slots__ = ('_obj',) means arbitrary attribute writes must fail."""
    proxy = NetworkProxy(_Fake())
    with pytest.raises(AttributeError):
        proxy.extra = "nope"


def test_network_proxy_chain_stays_on_worker():
    """A pypowsybl method returning another pypowsybl object must chain safely."""
    class Child:
        __module__ = "pypowsybl.child"

        def compute(self):
            return threading.current_thread().name

    class Parent:
        __module__ = "pypowsybl.parent"

        def make(self):
            return Child()

    proxy = NetworkProxy(Parent())
    child_proxy = proxy.make()
    assert isinstance(child_proxy, NetworkProxy)
    worker_name = child_proxy.compute()
    assert worker_name.startswith("pypowsybl")
