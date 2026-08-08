import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from porter.deb import build_deb

CONTROL = {"Package": "demo-app", "Version": "1.0", "Architecture": "amd64",
           "Maintainer": "porter <porter@example.com>", "Description": "demo"}


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "stage"
    (stage / "usr/lib/demo-app").mkdir(parents=True)
    (stage / "usr/lib/demo-app/app.txt").write_text("payload\n")
    (stage / "etc/demo-app").mkdir(parents=True)
    (stage / "etc/demo-app/defaults").write_text("PORT=9000\n")
    return stage


def _contents(deb: Path) -> str:
    """`dpkg-deb --contents`, read as an artefact assertion: it reports the
    payload's paths AND its ownership, both of which are decisions build_deb
    makes and neither of which its return value can evidence."""
    proc = subprocess.run(["dpkg-deb", "--contents", str(deb)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _control_tar(deb: Path) -> tarfile.TarFile:
    """The control member, opened in-process.

    Deliberately not `dpkg-deb --ctrl-tarfile | tar -t`: a pipe hands the
    caller tar's exit code, and the names alone cannot show a maintainer
    script's mode -- which is the thing that decides whether dpkg will run it.
    """
    proc = subprocess.run(["dpkg-deb", "--ctrl-tarfile", str(deb)], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    return tarfile.open(fileobj=io.BytesIO(proc.stdout))


def _ar_members(deb: Path) -> list[str]:
    """Member names of the .deb's outer ar archive.

    The data member is named for its compression -- `data.tar.xz`, `data.tar.gz`
    or, uncompressed, plain `data.tar` -- so this is how -Znone is observable in
    the artefact rather than merely in the argv. Parsed here rather than shelled
    out to `ar`, so the test does not silently depend on binutils.
    """
    blob = deb.read_bytes()
    assert blob[:8] == b"!<arch>\n", f"{deb} is not an ar archive"
    names, pos = [], 8
    while pos + 60 <= len(blob):
        header = blob[pos:pos + 60]
        names.append(header[:16].decode().strip().rstrip("/"))
        size = int(header[48:58].decode().strip())
        pos += 60 + size + (size % 2)  # ar pads members to an even offset
    return names


def test_builds_a_deb_with_the_declared_fields(tmp_path):
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path)
    assert deb.name == "demo-app_1.0_amd64.deb"
    fields = subprocess.run(["dpkg-deb", "--field", str(deb)],
                            capture_output=True, text=True, check=True).stdout
    assert "Package: demo-app" in fields
    assert "Version: 1.0" in fields
    assert "./usr/lib/demo-app/app.txt" in _contents(deb)


def test_payload_is_owned_by_root(tmp_path):
    """porter builds as an unprivileged user. Without --root-owner-group the
    payload ships owned by that user's uid -- measured on zion 2026-08-07:
    `apiad/apiad` in --contents -- and the client would install files it does
    not own. dpkg-deb reports rc=0 either way, so only the artefact tells."""
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path)
    for line in _contents(deb).splitlines():
        assert " root/root " in line, f"not root-owned: {line}"


def test_the_data_member_is_uncompressed(tmp_path):
    """-Znone. Payloads are model weights and compiled libs -- already
    high-entropy -- so xz burns minutes to save approximately nothing."""
    assert "data.tar" in _ar_members(build_deb(_stage(tmp_path), CONTROL, tmp_path))


def test_conffiles_are_registered(tmp_path):
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path, conffiles=["/etc/demo-app/defaults"])
    tar = _control_tar(deb)
    names = {n.lstrip("./") for n in tar.getnames()}
    assert "conffiles" in names
    body = tar.extractfile("./conffiles").read().decode()
    assert body.splitlines() == ["/etc/demo-app/defaults"]


def test_no_conffiles_member_when_none_are_declared(tmp_path):
    """An empty conffiles member would make dpkg treat nothing as a conffile
    while looking like the feature works."""
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path)
    assert "conffiles" not in {n.lstrip("./") for n in _control_tar(deb).getnames()}


def test_maintainer_scripts_are_shipped_executable(tmp_path):
    """dpkg refuses to run a maintainer script that is not executable, and the
    failure surfaces at the client, mid-install, on a box with no operator."""
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path,
                    scripts={"postinst": "#!/bin/sh\nexit 0\n"})
    tar = _control_tar(deb)
    assert "postinst" in {n.lstrip("./") for n in tar.getnames()}
    info = tar.getmember("./postinst")
    assert info.mode & 0o111, f"postinst is not executable: {info.mode:o}"
    assert tar.extractfile("./postinst").read().decode() == "#!/bin/sh\nexit 0\n"


def test_the_stage_is_left_without_the_build_scaffolding(tmp_path):
    """DEBIAN/ is build scaffolding, not payload. Leaving it behind makes a
    second build of the same stage lint a tree it did not stage."""
    stage = _stage(tmp_path)
    build_deb(stage, CONTROL, tmp_path)
    assert not (stage / "DEBIAN").exists()


def test_refuses_a_stage_that_writes_to_client_state(tmp_path):
    """/var/lib/<pkg> belongs to the client. A package that ships files there
    would overwrite state on upgrade -- the failure une-tools' _check-staged.sh
    exists to prevent."""
    stage = _stage(tmp_path)
    (stage / "var/lib/demo-app").mkdir(parents=True)
    (stage / "var/lib/demo-app/state.db").write_text("x")
    with pytest.raises(ValueError, match="client-owned"):
        build_deb(stage, CONTROL, tmp_path)


def test_refuses_a_stage_that_writes_to_client_logs(tmp_path):
    """/var/log/<pkg> is the same rule: the client's, not the package's."""
    stage = _stage(tmp_path)
    (stage / "var/log/demo-app").mkdir(parents=True)
    (stage / "var/log/demo-app/app.log").write_text("x")
    with pytest.raises(ValueError, match="client-owned"):
        build_deb(stage, CONTROL, tmp_path)


def test_refuses_a_stage_carrying_an_env_file(tmp_path):
    stage = _stage(tmp_path)
    (stage / "etc/demo-app/env").write_text("SECRET=1\n")
    with pytest.raises(ValueError, match="never shipped"):
        build_deb(stage, CONTROL, tmp_path)


@pytest.mark.parametrize("junk", [".venv", ".git", ".env"])
def test_refuses_a_stage_carrying_junk(tmp_path, junk):
    """A staged `.venv` is rule 1's failure shipped inside a .deb; a staged
    `.git` ships the repo's history to the client; a staged `.env` ships
    whatever secret the developer had locally."""
    stage = _stage(tmp_path)
    (stage / "usr/lib/demo-app" / junk).mkdir()
    with pytest.raises(ValueError, match=junk if junk != ".env" else r"\.env"):
        build_deb(stage, CONTROL, tmp_path)


def test_accepts_a_stage_carrying_pycache(tmp_path):
    """A vendored python-build-standalone tree ships __pycache__ -- 35
    directories in the 3.12 tree `interpreter.vendor()` materialises, counted
    on zion 2026-08-07. Treating it as junk would refuse every real porter
    stage, so this pins the decision not to."""
    stage = _stage(tmp_path)
    cache = stage / "usr/lib/demo-app/python/lib/python3.12/__pycache__"
    cache.mkdir(parents=True)
    (cache / "os.cpython-312.pyc").write_bytes(b"\x00")
    deb = build_deb(stage, CONTROL, tmp_path)
    assert "__pycache__/os.cpython-312.pyc" in _contents(deb)


def test_a_dpkg_deb_failure_raises_instead_of_returning_a_missing_path(tmp_path):
    """The silent-success failure mode: dpkg-deb refuses, and an unchecked
    returncode still returns a Path that reads like a built package. Uppercase
    is illegal in a Debian package name -- rc=2, verified on dpkg 1.23.7."""
    control = {**CONTROL, "Package": "Demo_App"}
    with pytest.raises(RuntimeError, match="dpkg-deb rc="):
        build_deb(_stage(tmp_path), control, tmp_path)
    assert not (tmp_path / "Demo_App_1.0_amd64.deb").exists()
