import subprocess
from pathlib import Path
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
