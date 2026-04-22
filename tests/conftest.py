import io
import os
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
XIIDM = ROOT / "test_ieee14.xiidm"


class FakeUploadedFile:
    """Minimal stand-in for streamlit.runtime.uploaded_file_manager.UploadedFile."""

    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data
        self.file_id = str(id(self))

    def getvalue(self) -> bytes:
        return self._data

    def getbuffer(self) -> memoryview:
        return memoryview(self._data)


@pytest.fixture(scope="session")
def xiidm_bytes() -> bytes:
    return XIIDM.read_bytes()


@pytest.fixture
def xiidm_upload(xiidm_bytes) -> FakeUploadedFile:
    return FakeUploadedFile("test_ieee14.xiidm", xiidm_bytes)


@pytest.fixture
def zip_upload(xiidm_bytes) -> FakeUploadedFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("test_ieee14.xiidm", xiidm_bytes)
    return FakeUploadedFile("network.zip", buf.getvalue())


@pytest.fixture(autouse=True)
def cwd_project_root(monkeypatch):
    monkeypatch.chdir(ROOT)


@pytest.fixture
def node_breaker_network():
    """A NetworkProxy wrapping a small node-breaker test network.

    Uses pypowsybl's ``create_four_substations_node_breaker_network`` because
    IEEE14 (the regular fixture) is bus-breaker and exercises a different
    branch of the feeder-bay helper.
    """
    from iidm_viewer.powsybl_worker import NetworkProxy, run

    def _make():
        import pypowsybl.network as pn
        return pn.create_four_substations_node_breaker_network()

    return NetworkProxy(run(_make))


@pytest.fixture
def blank_network():
    """A NetworkProxy wrapping a completely empty network (no substations, no VLs).

    Mirrors what the UI creates when the user clicks "Start from blank network".
    Used to verify that merge operations on empty DataFrames don't raise
    ValueError due to float64 vs object dtype mismatches on ID columns.
    """
    from iidm_viewer.powsybl_worker import NetworkProxy, run

    def _make():
        import pypowsybl.network as pn
        return pn.create_empty(network_id="blank")

    return NetworkProxy(run(_make))
