"""The delivery half: a flat apt repo on a USB tree, and the upgrade path.

The property under test is the one the client is promised: **the same command
installs and updates**. Every docker test below deletes every network apt source
inside the container first. Without that, a working Debian mirror can satisfy
the install and the test passes with the local repo broken -- which is a lie
about the only thing this module does.
"""
import gzip
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from porter.deb import build_deb
from porter.repo import usb_tree, write_index

# Every apt source that could rescue a broken local repo, gone. `--network none`
# is not enough on its own: a cached index in /var/lib/apt/lists would still let
# apt resolve a package name we never indexed.
NO_NETWORK_SOURCES = (
    "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources "
    "/etc/apt/sources.list.d/*.list; rm -rf /var/lib/apt/lists/*;"
)
# Same, from an unprivileged shell. No single quotes above, so this nests.
NO_NETWORK_SOURCES_SUDO = f"sudo -n bash -c '{NO_NETWORK_SOURCES}';"


def _deb(tmp: Path, out: Path, *, version: str, payload: str) -> Path:
    """A minimal but real demo-app .deb, built by porter's own build_deb.

    The payload filename carries the version, so an upgrade is visible in the
    filesystem and not only in dpkg's database: dpkg removes a file the new
    version does not ship, and "the old payload is gone" is the half of an
    upgrade that a `dpkg-query -W` can never see.
    """
    stage = tmp / f"stage-{version}"
    (stage / "usr/lib/demo-app").mkdir(parents=True)
    (stage / "usr/lib/demo-app" / payload).write_text(f"payload of {version}\n")
    return build_deb(stage, {
        "Package": "demo-app",
        "Version": version,
        "Architecture": "amd64",
        "Maintainer": "porter <porter@example.com>",
        "Description": "porter's example package",
    }, out)


@pytest.fixture
def two_demo_debs(tmp_path) -> list[Path]:
    """v1.0 and v2.0 of one package, with distinguishable payloads."""
    out = tmp_path / "debs"
    return [_deb(tmp_path, out, version="1.0", payload="v1.txt"),
            _deb(tmp_path, out, version="2.0", payload="v2.txt")]


def _run(docker_image, out: Path, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{out}:/media/usb:ro", docker_image, "bash", "-c", script],
        capture_output=True, text=True)


SUDO_DOCKERFILE = """FROM {base}
RUN apt-get update \\
 && apt-get install -y --no-install-recommends sudo \\
 && rm -rf /var/lib/apt/lists/* \\
 && useradd -m deployer \\
 && useradd -m bystander
RUN echo 'deployer ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/deployer
"""


@pytest.fixture(scope="session")
def sudo_image(docker_image) -> str:
    """The client image plus sudo, one deployer who has it passwordlessly and
    one who does not.

    Derived from conftest's image so the no-system-python3 control still holds.
    `bystander` is the half that matters: the script must refuse *and say why*,
    in bounded time. An unattended install that stops at a password prompt is
    not a failed install, it is a machine that looks busy until someone visits.
    """
    tag = "porter-test-client:bookworm-sudo"
    build = subprocess.run(["docker", "build", "-q", "-t", tag, "-"],
                           input=SUDO_DOCKERFILE.format(base=docker_image),
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    return tag


# --- the index ---------------------------------------------------------------

def test_index_lists_every_deb_with_its_real_size_and_hash(two_demo_debs, tmp_path):
    """Presence of a `SHA256:` line is not the claim -- it being *correct* is.

    apt refuses a file whose recorded hash does not match, so a decorative
    index takes the install down at the client and nowhere else.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    for d in two_demo_debs:
        shutil.copy2(d, repo / d.name)

    write_index(repo)
    body = (repo / "Packages").read_text()

    assert body.count("Package: demo-app") == 2
    for d in two_demo_debs:
        assert f"Filename: ./{d.name}\n" in body
        assert f"Size: {d.stat().st_size}\n" in body
        assert f"SHA256: {hashlib.sha256(d.read_bytes()).hexdigest()}\n" in body
    assert "Version: 1.0\n" in body and "Version: 2.0\n" in body
    assert gzip.decompress((repo / "Packages.gz").read_bytes()).decode() == body
    assert (repo / "Release").exists()


def test_an_empty_directory_is_refused_and_nothing_is_written(tmp_path):
    """GUARD. An empty index is a USB stick that fails at the client.

    Refused *before* anything lands: the pre-guard order wrote Packages,
    Packages.gz and Release and only then raised, so a caller that caught the
    error -- or read the directory afterwards -- found a published, empty repo.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="no .deb"):
        write_index(repo)
    assert sorted(p.name for p in repo.iterdir()) == []


def test_a_file_that_is_not_a_deb_is_refused(tmp_path):
    """dpkg-deb's rc is read directly. Unchecked, a truncated or corrupt
    package contributes an empty stanza and the index still writes at rc=0."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "broken_1.0_amd64.deb").write_bytes(b"not an archive")
    with pytest.raises(RuntimeError, match="dpkg-deb"):
        write_index(repo)


# --- the USB tree ------------------------------------------------------------

def test_usb_tree_writes_an_executable_install_script_and_the_readme(
        two_demo_debs, tmp_path):
    out = usb_tree(two_demo_debs, tmp_path / "usb", app="demo-app",
                   readme="Installation\n")
    assert (out / "README.txt").read_text() == "Installation\n"
    assert (out / "install.sh").stat().st_mode & 0o111
    assert sorted(p.name for p in (out / "repo").glob("*.deb")) == [
        d.name for d in two_demo_debs]
    assert (out / "repo/Packages").exists()


def test_the_shipped_install_template_parses(two_demo_debs, tmp_path):
    """install.sh is 60 lines of shell living in a Python string literal, and it
    is the only thing the client runs. Nothing else in the suite would notice an
    editing slip in it until a container test failed to start."""
    out = usb_tree(two_demo_debs, tmp_path / "usb", app="demo-app", readme="x")
    proc = subprocess.run(["bash", "-n", str(out / "install.sh")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_a_template_that_does_not_parse_is_refused_at_build_time(
        two_demo_debs, tmp_path, monkeypatch):
    """GUARD, and the injection is the point.

    `app` is validated, so no *input* can make the shipped template stop
    parsing -- which would leave the runtime check unable to fire and therefore
    worth less than none. What it actually guards is the template itself, so
    the test supplies a broken one. deb.py paid for this class already:
    generated shell that is never syntax-checked builds at rc=0 and dies at the
    client, with no network to fix it from.
    """
    monkeypatch.setattr("porter.repo.INSTALL_SH", 'if [ -z "{app}" ]; then\n')
    with pytest.raises(RuntimeError, match="does not parse"):
        usb_tree(two_demo_debs, tmp_path / "usb", app="demo-app", readme="x")


def test_an_app_name_that_is_not_a_debian_package_name_is_refused(
        two_demo_debs, tmp_path):
    """GUARD, and TRAP 4 APPLIES -- what it adds is TIMING, not detection.

    `app` is interpolated into install.sh and into an apt sources filename, so a
    name carrying a space, a quote or a slash is a broken tree. But every such
    name is also absent from the index, so disabling this branch alone does not
    let one ship: the membership check below still refuses it. Measured.

    What is lost is *when*. That check runs after the .debs are copied and the
    index is written -- gigabytes, for a real payload -- and reports the wrong
    problem ("not in the index") for what is really a malformed name. Hence the
    assertion that nothing was created, which is the half that registers.
    """
    for bad in ('demo app', 'demo"app', 'demo/app', 'Demo-App', ''):
        with pytest.raises(ValueError, match="package name"):
            usb_tree(two_demo_debs, tmp_path / "usb", app=bad, readme="x")
    assert not (tmp_path / "usb").exists()


def test_a_tree_whose_repo_lacks_the_app_is_refused(two_demo_debs, tmp_path):
    """GUARD. install.sh names one package. A tree built for `demo-app` out of
    somebody else's .debs is a stick that copies fine, boots fine, and tells the
    client `Unable to locate package demo-app` -- with no network to fix it."""
    with pytest.raises(ValueError, match="not in the index"):
        usb_tree(two_demo_debs, tmp_path / "usb", app="other-app", readme="x")


# --- the delivery promise, in a container ------------------------------------

@pytest.mark.docker
def test_install_then_upgrade_offline_from_the_usb_tree(
        two_demo_debs, tmp_path, docker_image):
    """The delivery promise: the SAME command installs and upgrades.

    `--version 1.0` picks the older of the two packages sitting in the repo, so
    the second run -- with no flags at all -- has something real to move to. The
    v1 payload being *gone* afterwards is the half dpkg-query cannot report.
    """
    out = usb_tree(two_demo_debs, tmp_path / "usb", app="demo-app",
                   readme="Installation\n")
    script = (
        "set -e;" + NO_NETWORK_SOURCES +
        "bash /media/usb/install.sh --version 1.0 >/dev/null;"
        "test -f /usr/lib/demo-app/v1.txt;"
        "dpkg-query -W -f='${Version}' demo-app;"
        "echo -n ' -> ';"
        "bash /media/usb/install.sh >/dev/null;"
        "dpkg-query -W -f='${Version}' demo-app;"
        "test -f /usr/lib/demo-app/v2.txt;"
        "! test -e /usr/lib/demo-app/v1.txt || { echo ' STALE-V1-PAYLOAD'; exit 3; }"
    )
    proc = _run(docker_image, out, script)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "1.0 -> 2.0", proc.stdout


@pytest.mark.docker
def test_the_index_is_load_bearing_not_decorative(
        two_demo_debs, tmp_path, docker_image):
    """POSITIVE CONTROL for the test above.

    Corrupt one recorded SHA256 and the install must fail. Without this, an
    install that quietly bypassed the index -- or an apt that ignored it --
    would make the upgrade test pass no matter what write_index emitted.
    """
    out = usb_tree(two_demo_debs, tmp_path / "usb", app="demo-app", readme="x")
    packages = out / "repo/Packages"
    body = packages.read_text()
    old = [ln for ln in body.splitlines() if ln.startswith("SHA256: ")][0]
    packages.write_text(body.replace(old, "SHA256: " + "0" * 64, 1))
    (out / "repo/Packages.gz").write_bytes(gzip.compress(packages.read_bytes()))

    proc = _run(docker_image, out,
                "set -e;" + NO_NETWORK_SOURCES +
                "bash /media/usb/install.sh --version 1.0")
    assert proc.returncode != 0, (
        "apt accepted a package whose recorded hash was wrong: the index this "
        "module writes is decorative and the upgrade test proves nothing"
    )
    assert "Hash Sum mismatch" in proc.stdout + proc.stderr, proc.stdout + proc.stderr


@pytest.mark.docker
def test_a_broken_client_apt_source_cannot_break_the_offline_install(
        two_demo_debs, tmp_path, docker_image):
    """A client's stale source must not reach our update.

    The positive control is in the same container: an *unscoped* `apt-get
    update` is run first and asserted to fail against the same broken source,
    so a scoping option that silently stopped working could not pass this.

    The broken source is a `file:` one pointing at a stick that is gone, and
    NOT an unreachable mirror. Measured 2026-08-08: `deb http://stale.invalid/`
    makes `apt-get update` print `Some index files failed to download` and exit
    **0** -- as a control it can never fail, which is worse than no control. A
    missing `file:` path exits 100. It is also the failure this module's own
    cleanup trap exists to prevent, so the two tests are each other's evidence.
    """
    out = usb_tree(two_demo_debs, tmp_path / "usb", app="demo-app", readme="x")
    script = (
        "set -e;" + NO_NETWORK_SOURCES +
        "echo 'deb [trusted=yes] file:/media/old-usb/repo ./' "
        "> /etc/apt/sources.list.d/zz-client-stale.list;"
        # control: unscoped, the broken source really does take apt down.
        "if apt-get update -qq >/dev/null 2>&1; then "
        "echo 'CONTROL-FAILED: broken source did not break apt'; exit 3; fi;"
        "bash /media/usb/install.sh --version 1.0 >/dev/null;"
        "dpkg-query -W -f='${Version}' demo-app"
    )
    proc = _run(docker_image, out, script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "1.0", proc.stdout


@pytest.mark.docker
def test_a_non_root_operator_lifts_through_sudo_keeping_the_flags(
        two_demo_debs, tmp_path, sudo_image):
    """The re-exec must not eat the caller's argv.

    Parsing the flags before lifting to root -- the obvious order -- consumes
    every argument, so `exec sudo bash "$0" "$@"` re-enters with none of them
    and the deployer who asked for 1.0 silently gets 2.0, at rc=0. That is
    porter's characteristic bug wearing a shell script, so the assertion is on
    the version dpkg reports and not on the exit code.
    """
    out = usb_tree(two_demo_debs, tmp_path / "usb", app="demo-app", readme="x")
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--user", "deployer",
         "-v", f"{out}:/media/usb:ro", sudo_image, "bash", "-c",
         "set -e;" + NO_NETWORK_SOURCES_SUDO +
         "test \"$(id -u)\" -ne 0;"          # control: we really are unprivileged
         "bash /media/usb/install.sh --version 1.0 >/dev/null;"
         "dpkg-query -W -f='${Version}' demo-app"],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "1.0", proc.stdout


@pytest.mark.docker
def test_an_operator_without_passwordless_sudo_is_refused_not_prompted(
        two_demo_debs, tmp_path, sudo_image):
    """Rule 9. No prompt is reachable, so the failure is loud and immediate.

    `timeout` is the assertion, not a safety net: rc=124 would mean the script
    blocked, which on an airgapped client at 3am is indistinguishable from an
    install still running.
    """
    out = usb_tree(two_demo_debs, tmp_path / "usb", app="demo-app", readme="x")
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--user", "bystander",
         "-v", f"{out}:/media/usb:ro", sudo_image, "bash", "-c",
         "timeout 30 bash /media/usb/install.sh; echo \"rc=$?\""],
        capture_output=True, text=True, timeout=180)
    assert "rc=1" in proc.stdout, proc.stdout + proc.stderr
    assert "passwordless sudo is unavailable" in proc.stderr, proc.stderr
    assert "sudo bash /media/usb/install.sh" in proc.stderr, proc.stderr


@pytest.mark.docker
def test_the_install_leaves_the_clients_apt_as_it_found_it(
        two_demo_debs, tmp_path, docker_image):
    """GUARD. Our sources entry points at the USB mount. Left behind, every
    later `apt-get update` on that client fails with `File not found` on a
    stick that was unplugged weeks ago -- porter breaking apt for good, on a
    machine nobody can ssh into."""
    out = usb_tree(two_demo_debs, tmp_path / "usb", app="demo-app", readme="x")
    script = (
        "set -e;" + NO_NETWORK_SOURCES +
        "bash /media/usb/install.sh >/dev/null;"
        "dpkg-query -W -f='${Version}' demo-app;"
        "test ! -e /etc/apt/sources.list.d/demo-app.list "
        "|| { echo ' LEFTOVER-SOURCE'; exit 3; };"
        # And apt still works afterwards, which is the thing that actually broke.
        "apt-get update -qq >/dev/null 2>&1 || { echo ' APT-BROKEN'; exit 4; }"
    )
    proc = _run(docker_image, out, script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "2.0", proc.stdout
