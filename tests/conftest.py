import pytest

from launcher import gpu


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    """Keep tests independent of the machine: no aria2c and no nvidia-smi."""
    monkeypatch.setenv("AIMODELKI_DOWNLOADER", "python")
    monkeypatch.delenv("AIMODELKI_CUDA_VARIANT", raising=False)
    monkeypatch.setattr(gpu, "query_devices", lambda *args, **kwargs: None)
    gpu.reset_cache()
    yield
    gpu.reset_cache()
