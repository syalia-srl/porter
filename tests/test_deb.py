import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from porter.deb import build_deb

CONTROL = {"Package": "demo-app", "Version": "1.0", "Architecture": "amd64",
           "Maintainer": "porter <porter@example.com>", "Description": "demo"}

# The stage ships /etc/demo-app/defaults, so every build of it must declare
# that file: shipped as ordinary payload, dpkg replaces the admin's edited copy
# on upgrade with no .dpkg-dist, no prompt and no record.
CONFFILES = ["/etc/demo-app/defaults"]


def _stage(tmp_path: Path, *, etc: bool = True) -> Path:
    stage = tmp_path / "stage"
    (stage / "usr/lib/demo-app").mkdir(parents=True)
    (stage / "usr/lib/demo-app/app.txt").write_text("payload\n")
    if etc:
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


def _field(deb: Path, name: str) -> str:
    """One control field, read back off the artefact.

    `dpkg-deb --field <deb> <Field>` exits 0 and prints nothing for a field
    that is absent, so an assertion on emptiness needs a positive control --
    `test_a_multi_line_value_is_folded_not_injected` carries one.
    """
    proc = subprocess.run(["dpkg-deb", "--field", str(deb), name],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_builds_a_deb_with_the_declared_fields(tmp_path):
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path, conffiles=CONFFILES)
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
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path, conffiles=CONFFILES)
    for line in _contents(deb).splitlines():
        assert " root/root " in line, f"not root-owned: {line}"


def test_the_data_member_is_uncompressed(tmp_path):
    """-Znone. Payloads are model weights and compiled libs -- already
    high-entropy -- so xz burns minutes to save approximately nothing."""
    assert "data.tar" in _ar_members(
        build_deb(_stage(tmp_path), CONTROL, tmp_path, conffiles=CONFFILES))


def test_conffiles_are_registered(tmp_path):
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path, conffiles=CONFFILES)
    tar = _control_tar(deb)
    names = {n.lstrip("./") for n in tar.getnames()}
    assert "conffiles" in names
    body = tar.extractfile("./conffiles").read().decode()
    assert body.splitlines() == ["/etc/demo-app/defaults"]


def test_no_conffiles_member_when_none_are_declared(tmp_path):
    """An empty conffiles member would make dpkg treat nothing as a conffile
    while looking like the feature works."""
    deb = build_deb(_stage(tmp_path, etc=False), CONTROL, tmp_path)
    assert "conffiles" not in {n.lstrip("./") for n in _control_tar(deb).getnames()}


def test_maintainer_scripts_are_shipped_executable(tmp_path):
    """dpkg refuses to run a maintainer script that is not executable, and the
    failure surfaces at the client, mid-install, on a box with no operator.

    Which half bites, so the next reader does not weaken the wrong one: the
    `chmod(0o755)` guard IS covered -- delete it and this test goes red -- but
    via the `RuntimeError`, not via the mode assertion below. dpkg-deb's own
    check is a bitmask (`mode & 07557 != 0555`) demanding r+x for user, group
    and other, so it rejects 0664 at build time even though that is numerically
    inside the `>=0555 and <=0775` its error message quotes. No mode that
    reaches the artefact can therefore lack the exec bits, and `info.mode &
    0o111` can never be the line that fails today. Kept because a future dpkg
    that relaxes the build-time check hands the assertion its job back."""
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path, conffiles=CONFFILES,
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
    build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)
    assert not (stage / "DEBIAN").exists()


def test_refuses_a_stage_that_writes_to_client_state(tmp_path):
    """/var/lib/<pkg> belongs to the client. A package that ships files there
    would overwrite state on upgrade -- the failure une-tools' _check-staged.sh
    exists to prevent."""
    stage = _stage(tmp_path)
    (stage / "var/lib/demo-app").mkdir(parents=True)
    (stage / "var/lib/demo-app/state.db").write_text("x")
    with pytest.raises(ValueError, match="client-owned"):
        build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)


def test_refuses_a_stage_that_writes_to_client_logs(tmp_path):
    """/var/log/<pkg> is the same rule: the client's, not the package's."""
    stage = _stage(tmp_path)
    (stage / "var/log/demo-app").mkdir(parents=True)
    (stage / "var/log/demo-app/app.log").write_text("x")
    with pytest.raises(ValueError, match="client-owned"):
        build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)


def test_refuses_a_stage_carrying_an_env_file(tmp_path):
    stage = _stage(tmp_path)
    (stage / "etc/demo-app/env").write_text("SECRET=1\n")
    with pytest.raises(ValueError, match="never shipped"):
        build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)


@pytest.mark.parametrize("junk", [".venv", ".git", ".env"])
def test_refuses_a_stage_carrying_junk(tmp_path, junk):
    """A staged `.venv` is rule 1's failure shipped inside a .deb; a staged
    `.git` ships the repo's history to the client; a staged `.env` ships
    whatever secret the developer had locally."""
    stage = _stage(tmp_path)
    (stage / "usr/lib/demo-app" / junk).mkdir()
    with pytest.raises(ValueError, match=junk if junk != ".env" else r"\.env"):
        build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)


def test_accepts_a_stage_carrying_pycache(tmp_path):
    """A vendored python-build-standalone tree ships __pycache__ -- 35
    directories in the 3.12 tree `interpreter.vendor()` materialises, counted
    on zion 2026-08-07. Treating it as junk would refuse every real porter
    stage, so this pins the decision not to."""
    stage = _stage(tmp_path)
    cache = stage / "usr/lib/demo-app/python/lib/python3.12/__pycache__"
    cache.mkdir(parents=True)
    (cache / "os.cpython-312.pyc").write_bytes(b"\x00")
    deb = build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)
    assert "__pycache__/os.cpython-312.pyc" in _contents(deb)


def test_a_dpkg_deb_failure_raises_instead_of_returning_a_missing_path(tmp_path):
    """The silent-success failure mode: dpkg-deb refuses, and an unchecked
    returncode still returns a Path that reads like a built package. Uppercase
    is illegal in a Debian package name -- rc=2, verified on dpkg 1.23.7."""
    control = {**CONTROL, "Package": "Demo_App"}
    with pytest.raises(RuntimeError, match="dpkg-deb rc="):
        build_deb(_stage(tmp_path), control, tmp_path, conffiles=CONFFILES)
    assert not (tmp_path / "Demo_App_1.0_amd64.deb").exists()


def test_a_failed_build_leaves_no_scaffolding_in_the_stage(tmp_path):
    """Cleanup on the failure path, not only after the return.

    `shutil.rmtree(debian)` placed after the `raise` never runs when the build
    fails, so the stage keeps a control/conffiles/postinst the caller thinks
    were consumed -- and the next call inherits them."""
    stage = _stage(tmp_path)
    with pytest.raises(RuntimeError, match="dpkg-deb rc="):
        build_deb(stage, {**CONTROL, "Package": "Demo_App"}, tmp_path,
                  conffiles=CONFFILES, scripts={"postinst": "#!/bin/sh\nexit 0\n"})
    assert not (stage / "DEBIAN").exists()


def test_a_stale_debian_directory_does_not_leak_into_the_next_package(tmp_path):
    """DEBIAN/ is built fresh, so no member survives a call.

    The failure this pins is silent success: a stage carrying scaffolding from
    an earlier build -- left there by a failed one, or pre-staged by a caller
    -- gets it packaged by a call that passed neither `scripts=` nor
    `conffiles=`, and returns a .deb at rc=0 shipping a postinst nobody wrote
    and a conffiles nobody declared.

    The stale `conffiles` names a path that IS in the payload on purpose:
    dpkg-deb rejects a conffile missing from the package (rc=2), so pointing it
    at a ghost would make this test pass through dpkg's error rather than
    through the leak. Reusing DEBIAN/ has to be caught while the build still
    succeeds -- that is the whole failure mode."""
    stage = _stage(tmp_path, etc=False)
    stale = stage / "DEBIAN"
    stale.mkdir()
    (stale / "postinst").write_text("#!/bin/sh\necho STALE\n")
    (stale / "postinst").chmod(0o755)
    (stale / "conffiles").write_text("/usr/lib/demo-app/app.txt\n")

    deb = build_deb(stage, CONTROL, tmp_path)

    members = {n.removeprefix("./") for n in _control_tar(deb).getnames()}
    assert "postinst" not in members, f"stale scaffolding shipped: {members}"
    assert "conffiles" not in members, f"stale scaffolding shipped: {members}"


def test_refuses_a_stage_carrying_an_absolute_symlink(tmp_path):
    """Rule 1's failure mode, caught by hazard rather than by directory name.

    `.venv` in JUNK catches the shape uv leaves behind; an absolute link under
    any other name -- what `uv venv --relocatable` and a careless `cp -a` both
    produce -- builds and ships just as happily, works on the build host and
    dies at the client. Task 3's stages carry symlinks by design, so this is
    the last place that sees them."""
    stage = _stage(tmp_path)
    (stage / "usr/lib/demo-app/python").symlink_to("/usr/bin/python3")
    with pytest.raises(ValueError, match="absolute symlink"):
        build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)


def test_refuses_a_symlink_escaping_the_stage(tmp_path):
    """Relative, so it passes the absolute check, and still resolves onto the
    build host -- the same escape written the other way."""
    stage = _stage(tmp_path)
    (stage / "usr/lib/demo-app/python").symlink_to("../../../../../usr/bin/python3")
    with pytest.raises(ValueError, match="escaping the stage"):
        build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)


def test_accepts_a_relative_symlink_that_stays_inside_the_stage(tmp_path):
    """The positive control for the two above. `vendor()` copies with
    symlinks=True, and a python-build-standalone tree is full of intra-tree
    links (`bin/python3 -> python3.12`); a guard that refused all symlinks
    would refuse every real porter stage -- the `__pycache__` mistake again in
    a different costume."""
    stage = _stage(tmp_path)
    (stage / "usr/lib/demo-app/app-current.txt").symlink_to("app.txt")
    deb = build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)
    assert "./usr/lib/demo-app/app-current.txt -> app.txt" in _contents(deb)


def test_refuses_a_file_under_etc_that_is_not_declared_a_conffile(tmp_path):
    """The other half of the two-file config rule. Refusing /etc/<pkg>/env
    stops the admin's file being replaced; nothing stopped the shipped half
    going in as ordinary payload, which is the same harm one directory over --
    dpkg overwrites an edited /etc/demo-app/defaults on every upgrade, with no
    .dpkg-dist, no prompt and no record, because it was never registered."""
    stage = _stage(tmp_path)
    with pytest.raises(ValueError, match="not declared a conffile"):
        build_deb(stage, CONTROL, tmp_path)


def test_a_multi_line_value_is_folded_not_injected(tmp_path):
    """A bare newline in a control value starts a new field.

    Descriptions come from porter.yaml at Task 6, so a multi-line one is
    ordinary input: it is folded onto Debian continuation lines rather than
    refused. Unfolded, `Description: "demo\\nDepends: sudo"` built at rc=0 and
    dpkg-deb reported a real Depends the caller never wrote -- so the injection
    case below is asserted on a value with no blank line in it, which is the
    one dpkg accepts either way. A blank line unfolded would merely end the
    control paragraph and fail the build, which is not the bug."""
    control = {**CONTROL, "Description": "demo\nDepends: sudo"}
    deb = build_deb(_stage(tmp_path), control, tmp_path, conffiles=CONFFILES)

    assert _field(deb, "Depends") == "", "a folded value still injected a field"
    assert _field(deb, "Description") == "demo\n Depends: sudo"

    # Positive control: --field exits 0 and prints nothing for an absent field,
    # so the emptiness above only means something if this probe can see a real
    # Depends when there is one.
    honest = build_deb(_stage(tmp_path / "b"), {**CONTROL, "Depends": "sudo"},
                       tmp_path, conffiles=CONFFILES)
    assert _field(honest, "Depends") == "sudo"

    # An empty line inside a value is spelled ` .`; unfolded it would close the
    # control paragraph and dpkg-deb would refuse the build.
    para = build_deb(_stage(tmp_path / "c"), {**CONTROL, "Description": "demo\n\ntail"},
                     tmp_path, conffiles=CONFFILES)
    assert _field(para, "Description") == "demo\n .\n tail"
