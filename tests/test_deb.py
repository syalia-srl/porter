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


# A migration script as it arrives from porter.yaml, with one quote missing.
# `porter.migrate` splices `script:` into the postinst verbatim, so this is what
# an adopter's typo looks like by the time it reaches build_deb.
BROKEN_MIGRATION_POSTINST = '''#!/bin/sh
set -e
if [ "$1" = configure ]; then
  if [ -n "$2" ]; then
    (
      echo "migrating from $2
    )
  fi
fi
exit 0
'''


def test_a_maintainer_script_that_does_not_parse_is_refused(tmp_path):
    """The build is the last place an unparseable postinst is visible.

    Until Task 10, porter's maintainer scripts were built wholly from fixed
    strings and always parsed. `migrations: script:` is spliced in verbatim
    now, so one missing quote in porter.yaml produced a .deb that BUILT, linted
    and installed at rc=0 and then died on the client -- `subprocess installed
    post-installation script returned error exit status 2`, on a machine with no
    network and nobody watching.

    The positive control is the second half and it carries the test: the
    identical stage with a script that DOES parse must build. Without it, a
    refusal that fired on every input would satisfy the first assertion just as
    well.
    """
    with pytest.raises(ValueError, match="not valid sh"):
        build_deb(_stage(tmp_path), CONTROL, tmp_path, conffiles=CONFFILES,
                  scripts={"postinst": BROKEN_MIGRATION_POSTINST})
    good = BROKEN_MIGRATION_POSTINST.replace('from $2', 'from $2"')
    deb = build_deb(_stage(tmp_path / "ok"), CONTROL, tmp_path / "out",
                    conffiles=CONFFILES, scripts={"postinst": good})
    assert deb.exists()


def test_the_refusal_names_the_script_that_does_not_parse(tmp_path):
    """`postinst`, `prerm` and `postrm` fail identically on the client and the
    message is the only thing that says which one to look at."""
    with pytest.raises(ValueError, match="prerm"):
        build_deb(_stage(tmp_path), CONTROL, tmp_path, conffiles=CONFFILES,
                  scripts={"postinst": "#!/bin/sh\nexit 0\n",
                           "prerm": "#!/bin/sh\ncase $1 in\n"})


def test_a_generated_script_in_usr_bin_that_does_not_parse_is_refused(tmp_path):
    """`/usr/bin/<pkg>-setup` is not a maintainer script and fails the same way.

    porter writes every file it puts in /usr/bin -- the `command` wrapper and
    `<pkg>-setup` -- and both interpolate manifest text. dpkg-deb packages an
    unparseable one happily; the operator finds out when they run it, which for
    a setup wizard is the moment the client is being commissioned.

    The control is the third stage: the same directory with a script that parses
    must still build, or this refusal is just "no /usr/bin allowed".
    """
    stage = _stage(tmp_path)
    (stage / "usr/bin").mkdir(parents=True)
    (stage / "usr/bin/demo-app-setup").write_text(
        '#!/bin/sh\nprintf \'GREETING [%s]: \' "$cur\nread new\n')
    with pytest.raises(ValueError, match="/usr/bin/demo-app-setup"):
        build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)

    ok = _stage(tmp_path / "ok")
    (ok / "usr/bin").mkdir(parents=True)
    (ok / "usr/bin/demo-app-setup").write_text(
        '#!/bin/sh\nprintf \'GREETING [%s]: \' "$cur"\nread new\n')
    assert build_deb(ok, CONTROL, tmp_path / "out", conffiles=CONFFILES).exists()


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
    """DEBIAN/ is porter's to build, so a caller-staged one is refused.

    Two failures, one guard. The first is a leak: a stage carrying scaffolding
    from an earlier build -- left there by a failed one, or pre-staged by a
    caller -- used to get it packaged by a call that passed neither `scripts=`
    nor `conffiles=`, returning a .deb at rc=0 shipping a postinst nobody wrote.

    The second is what deleting the directory instead traded that for: this
    stage also carries `triggers`, a real Debian control member with no
    build_deb parameter (as are `shlibs`, `md5sums`, `templates`). Removing
    DEBIAN/ silently built a package *without* it at rc=0 -- the caller's
    trigger, gone, unreported. Refusing gives byte-for-byte the same protection
    against the leak and names the drop instead of performing it.

    The stale `conffiles` names a path that IS in the payload on purpose:
    dpkg-deb rejects a conffile missing from the package (rc=2), so under the
    delete-instead-of-raise mutation this stage still builds at rc=0, which is
    what makes the silent drop observable rather than masked by dpkg's error."""
    stage = _stage(tmp_path, etc=False)
    stale = stage / "DEBIAN"
    stale.mkdir()
    (stale / "postinst").write_text("#!/bin/sh\necho STALE\n")
    (stale / "postinst").chmod(0o755)
    (stale / "conffiles").write_text("/usr/lib/demo-app/app.txt\n")
    (stale / "triggers").write_text("interest-noawait /usr/lib/demo-app\n")

    with pytest.raises(ValueError, match="DEBIAN/ is porter's to build"):
        build_deb(stage, CONTROL, tmp_path)

    assert not (tmp_path / "demo-app_1.0_amd64.deb").exists()
    assert (stale / "triggers").exists(), "the caller's control member was deleted"


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


@pytest.mark.parametrize("top", ["home", "tmp", "srv", "boot"])
def test_refuses_a_top_level_path_porter_does_not_own(tmp_path, top):
    """porter's thesis is that a package owns exactly a declared set of paths,
    so an unbounded top level contradicts the whole tool.

    Measured on zion 2026-08-08: a stage carrying `home/apiad/secrets/id_rsa`
    built at rc=0 and dpkg-deb --contents listed `./home/apiad/secrets/id_rsa`
    -- an ssh key packaged for an airgapped client with nothing reporting it.
    This is also the check that catches a stage rooted one directory too high,
    which is how it goes wrong in practice.

    `srv` is in this list deliberately, against the set the review suggested:
    FHS defines /srv as data for services provided by *this system*, i.e. the
    site administrator's, which is the same argument that refuses /var/lib."""
    stage = _stage(tmp_path)
    (stage / top / "apiad/secrets").mkdir(parents=True)
    (stage / top / "apiad/secrets/id_rsa").write_text("PRIVATE KEY\n")
    with pytest.raises(ValueError, match=f"does not own: /{top}"):
        build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)
    assert not (tmp_path / "demo-app_1.0_amd64.deb").exists()


def test_accepts_the_top_level_shapes_a_real_package_ships(tmp_path):
    """The positive control for the allowlist above.

    An allowlist written one entry too narrow refuses a correct package, and
    does it at the moment a later task first stages the real shape rather than
    here -- the `__pycache__` mistake's structure exactly. So this stages every
    shape porter has committed to supporting: the two payload roots, a systemd
    unit at its usrmerged path and at Debian's pre-merge one, an /opt tree, a
    conffile, a /var path that is not the client's, and a symlinked *directory*
    (a python-build-standalone tree is full of them, and `vendor()` copies with
    symlinks=True). Every one must reach the payload."""
    stage = tmp_path / "stage"
    for rel, body in [
        ("usr/lib/demo-app/app.txt", "payload\n"),
        ("usr/share/demo-app/logo.svg", "<svg/>\n"),
        ("usr/lib/systemd/system/demo-app.service", "[Unit]\n"),
        ("opt/demo-app/blob.bin", "\x00\n"),
        ("lib/udev/rules.d/99-demo-app.rules", "# rules\n"),
        ("var/cache/demo-app/.keep", ""),
        ("etc/demo-app/defaults", "PORT=9000\n"),
    ]:
        (stage / rel).parent.mkdir(parents=True, exist_ok=True)
        (stage / rel).write_text(body)
    (stage / "usr/lib/demo-app/versions/1.0").mkdir(parents=True)
    (stage / "usr/lib/demo-app/current").symlink_to("versions/1.0")

    contents = _contents(build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES))
    for expected in ["./usr/lib/demo-app/app.txt", "./usr/share/demo-app/logo.svg",
                     "./usr/lib/systemd/system/demo-app.service", "./opt/demo-app/blob.bin",
                     "./lib/udev/rules.d/99-demo-app.rules", "./var/cache/demo-app/.keep",
                     "./etc/demo-app/defaults",
                     "./usr/lib/demo-app/current -> versions/1.0"]:
        assert expected in contents, f"{expected} did not reach the payload:\n{contents}"


def test_refuses_a_symlink_under_etc_that_is_not_declared_a_conffile(tmp_path):
    """The conffile lint walked files, so a symlink under /etc was neither
    refused nor required to be declared -- the same family as the file case it
    sits beside.

    Verified on dpkg 1.23.7, 2026-08-08: `etc/demo-app/extra.conf -> defaults`
    is relative and lands inside the stage, so the symlink guard passes it, and
    dpkg-deb built at rc=0 and shipped it. Undeclared, dpkg replaces the admin's
    edited target on every upgrade exactly as it would a plain file. Declared,
    dpkg merely warns that a conffile `is not a plain file` and proceeds -- so
    nothing downstream was ever going to catch this either."""
    stage = _stage(tmp_path)
    (stage / "etc/demo-app/extra.conf").symlink_to("defaults")
    with pytest.raises(ValueError, match="not declared a conffile"):
        build_deb(stage, CONTROL, tmp_path, conffiles=CONFFILES)


def test_refuses_an_env_symlink_even_when_it_is_declared_a_conffile(tmp_path):
    """Rule 4's admin-owned half, refused whether it is a file or a link.

    `Path.exists()` follows the link, so a *dangling* env symlink reported
    False and sailed through. It ships regardless: on 2026-08-08
    `etc/demo-app/env -> nowhere-at-all` built at rc=0 and dpkg-deb --contents
    listed it, both declared as a conffile and not.

    Declared here on purpose. It makes this the one test that can only be
    satisfied by the NEVER_SHIPPED check -- the conffile lint has nothing to
    say about a path the caller declared -- so removing that check makes the
    build *succeed* and ship /etc/demo-app/env, which is the original symptom
    rather than a differently-worded refusal."""
    stage = _stage(tmp_path)
    (stage / "etc/demo-app/env").symlink_to("nowhere-at-all")
    with pytest.raises(ValueError, match="never shipped"):
        build_deb(stage, CONTROL, tmp_path,
                  conffiles=[*CONFFILES, "/etc/demo-app/env"])


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
