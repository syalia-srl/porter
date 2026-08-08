"""What examples/service-fastapi actually ships.

Asserted against the built .deb rather than against the stage: the stage is an
intermediate nobody installs, and every failure this file is about -- a unit
that never reached the package, an env.example the postinst will look for and
not find, an admin file shipped by accident -- is invisible until something
reads the artefact.

These need uv (to vendor) but not docker; the install-time behaviour is in
tests/test_service_e2e.py.
"""
import io
import subprocess
import tarfile
from pathlib import Path


def _contents(deb: Path) -> str:
    proc = subprocess.run(["dpkg-deb", "--contents", str(deb)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _ctrl_member(deb: Path, name: str) -> str:
    """One DEBIAN/ member, read in-process.

    Not `dpkg-deb --ctrl-tarfile | tar -xO`: a pipe hands the caller tar's exit
    code, so a dpkg-deb that failed reads as an empty file rather than as an
    error.
    """
    proc = subprocess.run(["dpkg-deb", "--ctrl-tarfile", str(deb)], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        return tar.extractfile(f"./{name}").read().decode()


def test_the_package_carries_the_unit_and_the_env_template(built_demo_deb):
    contents = _contents(built_demo_deb)
    assert "./usr/lib/systemd/system/demo-app.service" in contents, contents
    # postinst does `cp /usr/share/demo-app/env.example /etc/demo-app/env`. A
    # stage that forgets this file builds happily and fails on the client, at
    # install time, on a box with no operator watching.
    assert "./usr/share/demo-app/env.example" in contents, contents


def test_the_admin_file_is_not_in_the_package_and_defaults_is_a_conffile(built_demo_deb):
    assert "./etc/demo-app/env" not in _contents(built_demo_deb)
    assert _ctrl_member(built_demo_deb, "conffiles") == "/etc/demo-app/defaults\n"


def test_the_vendored_interpreter_is_the_payload_and_the_package_is_of_a_plausible_size(
        built_demo_deb):
    """Magnitude, per the gate rule. A vendored 3.12 plus FastAPI is ~150 MB
    uncompressed and the package is built -Znone; a .deb of a few hundred KB
    would mean the interpreter never got staged, and every path assertion above
    would still pass."""
    assert "./usr/lib/demo-app/python/bin/python3.12" in _contents(built_demo_deb)
    size = built_demo_deb.stat().st_size
    assert size > 50_000_000, f"{built_demo_deb} is {size} bytes -- too small to hold an interpreter"
