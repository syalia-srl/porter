import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from porter import interpreter
from porter.interpreter import vendor, install


def test_vendored_root_is_a_real_directory_not_a_symlink(vendored: Path):
    """uv's managed python dir IS a symlink; cp -a would copy the link and
    vendor nothing. This is the P1b bug, as a regression test."""
    assert (vendored / "python").is_dir()
    assert not (vendored / "python").is_symlink()


def test_vendored_tree_is_substantial(vendored: Path):
    """A magnitude check: a link-copy produces 2 entries, a real one ~3000."""
    assert len(list((vendored / "python").rglob("*"))) > 1000


def test_externally_managed_marker_removed(vendored: Path):
    assert not (vendored / "python/lib/python3.12/EXTERNALLY-MANAGED").exists()


def test_interpreter_runs(vendored: Path):
    out = subprocess.run(
        [str(vendored / "python/bin/python3.12"), "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True, text=True, check=True,
    )
    assert "(3, 12)" in out.stdout


def test_install_puts_packages_in_the_vendored_site_packages(vendored: Path):
    install(vendored / "python/bin/python3.12", ["idna"])
    hits = list((vendored / "python/lib/python3.12/site-packages").glob("idna"))
    assert hits, "idna did not land in the vendored site-packages"


def _fake_venv(root: Path, version: str = "3.12") -> Path:
    """The shape `uv venv` produces, reduced to what vendor() looks at.

    Note it satisfies the naive checks: `bin/python<version>` and
    `lib/python<version>/` both exist, and the binary is a symlink to a real
    interpreter, so even the post-copy `binary.exists()` passes on this host.
    Its tells are `pyvenv.cfg` and the absent stdlib.
    """
    (root / "bin").mkdir(parents=True)
    (root / f"lib/python{version}/site-packages").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text(f"home = /elsewhere/bin\nversion_info = {version}\n")
    binary = root / "bin" / f"python{version}"
    binary.symlink_to(sys.executable)
    return binary


def test_vendor_refuses_a_virtualenv_masquerading_as_an_interpreter_root(tmp_path, monkeypatch):
    """`uv python find <v>` answers with a project virtualenv when its version
    matches, and two directories up from that is the venv, not a
    python-build-standalone root. Copying it would ship a bin/python that is an
    absolute symlink into the build host -- a wrong package, produced silently."""
    found = _fake_venv(tmp_path / "project" / ".venv")
    monkeypatch.setattr(
        interpreter,
        "_run",
        lambda cmd: str(found) if cmd[:3] == ["uv", "python", "find"] else "",
    )

    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError, match="virtualenv"):
        vendor(dest)
    assert not (dest / "python").exists(), "vendor() copied the venv before refusing it"


def test_vendor_refuses_a_root_with_no_interpreter_tree(tmp_path, monkeypatch):
    """The same derivation can land on an ordinary source directory -- porter's
    own repo root, say. Nothing about it is an interpreter; say so and stop."""
    repo = tmp_path / "repo"
    (repo / "src" / "porter").mkdir(parents=True)
    (repo / ".git").mkdir()
    found = repo / "bin" / "python3.12"  # never created
    monkeypatch.setattr(
        interpreter,
        "_run",
        lambda cmd: str(found) if cmd[:3] == ["uv", "python", "find"] else "",
    )

    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError, match="not a python-build-standalone tree"):
        vendor(dest)
    assert not (dest / "python").exists()


def test_vendor_ignores_a_matching_virtualenv_in_the_cwd(tmp_path, monkeypatch):
    """The same hazard end to end, against the real `uv python find`: uv
    discovers a venv from the cwd alone, no $VIRTUAL_ENV needed. This is what
    the --system --managed-python flags are for."""
    if not shutil.which("uv"):
        pytest.skip("uv not on PATH")
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["uv", "venv", "--python", "3.12", str(work / ".venv")], check=True,
                   capture_output=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("VIRTUAL_ENV", str(work / ".venv"))

    binary = vendor(tmp_path / "dest")

    # The stdlib is the discriminator: a venv's lib/python3.12/ holds only
    # site-packages, so this is absent unless a real standalone tree was copied.
    assert (binary.parent.parent / "lib/python3.12/os.py").is_file()
    assert not (binary.parent.parent / "pyvenv.cfg").exists()
