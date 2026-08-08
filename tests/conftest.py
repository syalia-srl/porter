import shutil
import pytest
from pathlib import Path
from porter.interpreter import vendor


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: needs a working docker daemon")
    config.addinivalue_line("markers", "nspawn: needs systemd-nspawn and root")


@pytest.fixture(scope="session")
def vendored(tmp_path_factory) -> Path:
    """One vendored interpreter for the whole session -- it is ~97 MB and a
    download, so building it per-test would make the suite unusable."""
    if not shutil.which("uv"):
        pytest.skip("uv not on PATH")
    dest = tmp_path_factory.mktemp("vendored")
    vendor(dest)
    return dest
