import os
import shutil
import pytest
from pathlib import Path
from porter.interpreter import vendor


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: needs a working docker daemon")
    config.addinivalue_line("markers", "nspawn: needs systemd-nspawn and root")


def _require_uv() -> None:
    """Absence of uv must not be able to report success.

    Most of this suite needs a real uv, and `pytest.skip` is the right local
    behaviour on a laptop that hasn't got one. On a gate it is the failure mode
    AGENTS.md exists to prevent: pytest exits 0 with essentially nothing
    verified. Set PORTER_REQUIRE_UV=1 wherever the run is meant to be evidence
    and the absence becomes loud instead.
    """
    if shutil.which("uv"):
        return
    if os.environ.get("PORTER_REQUIRE_UV", "") not in ("", "0"):
        pytest.fail(
            "uv is not on PATH and PORTER_REQUIRE_UV is set: this run would "
            "have skipped the tests it exists to perform.",
            pytrace=False,
        )
    pytest.skip("uv not on PATH")


@pytest.fixture
def require_uv() -> None:
    """For tests that shell out to uv without going through `vendored`."""
    _require_uv()


@pytest.fixture(scope="session")
def vendored(tmp_path_factory) -> Path:
    """One vendored interpreter for the whole session -- it is ~97 MB and a
    download, so building it per-test would make the suite unusable.

    Shared and mutable: `test_install_puts_packages_in_the_vendored_site_packages`
    installs into it. Anything that installs, or that alters the tree, should
    ask for `vendored_copy` instead.
    """
    _require_uv()
    dest = tmp_path_factory.mktemp("vendored")
    vendor(dest)
    return dest


@pytest.fixture
def vendored_copy(vendored, tmp_path) -> Path:
    """A throwaway copy of the session tree, for tests that mutate it."""
    shutil.copytree(vendored / "python", tmp_path / "python", symlinks=True)
    return tmp_path
