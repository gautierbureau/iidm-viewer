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
