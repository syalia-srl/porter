"""The gallery, built and installed the way a client receives it.

`tests/test_spec.py` proves the manifests parse. This file proves they *build*,
which is the only claim worth making about an example: a manifest that parses
and does not build is a schema nobody can use, and porter's characteristic bug
is precisely the thing that reports success one step before the step that
matters.

Both entries here are shapes nothing else in the suite covers:

  - `examples/command` -- a binary on PATH, with no unit, no /etc and no
    system user. `kind: command` had refusals since Task 4 and no manifest, so
    the branch that emits the wrapper was reached only by a fixture.
  - `examples/suite` -- four components and two metapackages, which is
    une-tools' two-machine shape. `apt install <one name>` is the claim, and
    it is asserted here as a claim about apt: the role is installed, its three
    components come with it, and the *other* role's component does not.

**What is checked and what is not.** The flat `Packages` index below is written
by this file, not by porter: `porter publish` (Task 5, `repo.py`) is what will
emit the real one, with a `Release`, a signature and an `install.sh`. So what
these tests prove is that the *packages* resolve as a role -- not that porter
can yet produce the USB tree that carries them. When Task 5 lands, `_index`
should be deleted and these tests pointed at the real thing.

One finding from writing it, which Task 5 needs: apt refuses an `MD5Sum:`-only
entry outright -- `Insufficient information available to perform this download
securely`, even under `[trusted=yes]` on a `file:` repo. `SHA256:` is required.
"""
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import _require_uv

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _porter() -> str:
    """The installed console script -- the surface a user actually reaches.

    Loud rather than skipped, for `_require_uv`'s reason: a run that quietly
    declined to build the gallery is the silence this file exists to end.
    """
    exe = shutil.which("porter")
    if exe is None:
        pytest.fail("the `porter` console script is not on PATH: use "
                    "`uv run --extra dev pytest`", pytrace=False)
    return exe


def _index(dist: Path) -> None:
    """A flat apt index over `dist`, so a role can be resolved by name.

    NOT `porter publish`. Task 5 owns the real repository -- Release, signature,
    install.sh -- and this is the smallest thing that lets apt answer the one
    question this file asks. `SHA256:` and not `MD5Sum:`: apt rejects an
    MD5-only entry as insecure even for a `file:` repo marked `[trusted=yes]`.
    """
    entries = []
    for deb in sorted(dist.glob("*.deb")):
        control = subprocess.run(["dpkg-deb", "-f", str(deb)],
                                 capture_output=True, text=True, check=True)
        body = deb.read_bytes()
        entries.append(f"{control.stdout.strip()}\n"
                       f"Filename: {deb.name}\n"
                       f"Size: {len(body)}\n"
                       f"SHA256: {hashlib.sha256(body).hexdigest()}\n")
    (dist / "Packages").write_text("\n".join(entries))


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """`porter build examples/<name>/porter.yaml` -> a directory of .debs.

    Built from the gallery file itself rather than a copy: the acceptance test
    for an example is that *it* builds, and a copy is one indirection away from
    the file an adopter reads. `--out` and `--stage` are absolute tmp paths, so
    nothing is written into the repo.

    Cached per example, because the suite vendors four interpreters (5.3 s and
    ~330 MB, measured on zion 2026-08-08) and three tests read the same .debs.
    """
    _require_uv()
    root = tmp_path_factory.mktemp("gallery")
    cache: dict[str, Path] = {}

    def build(name: str) -> Path:
        if name not in cache:
            dist, stage = root / name / "dist", root / name / "stage"
            proc = subprocess.run(
                [_porter(), "build", str(EXAMPLES / name / "porter.yaml"),
                 "--out", str(dist), "--stage", str(stage)],
                capture_output=True, text=True)
            assert proc.returncode == 0, (
                f"examples/{name} does not build: rc={proc.returncode}\n"
                f"{proc.stdout}\n{proc.stderr}")
            # porter removes the stage it creates, pass or fail. A tree left
            # behind means the *next* build of the same example meets the
            # non-empty-stage refusal instead of building.
            assert not stage.exists() or not any(stage.iterdir()), (
                f"porter left its stage behind: {sorted(stage.iterdir())}")
            _index(dist)
            cache[name] = dist
        return cache[name]

    return build


def _in_client(image: str, usb: Path, script: str) -> subprocess.CompletedProcess:
    """Run `script` on a client with no Python and no network.

    `--network none` plus a base asserted to have no `python3` (the
    `docker_image` fixture asserts it) is what makes "the command answered"
    mean "the vendored interpreter answered".
    """
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{usb}:/media/usb:ro", image, "bash", "-c", script],
        capture_output=True, text=True)


APT = ("echo 'deb [trusted=yes] file:/media/usb ./' > /etc/apt/sources.list.d/porter.list; "
       # Scoped to our own list file, so a stale or unreachable source in
       # sources.list.d cannot break an offline install (docs/design-spec.md).
       "apt-get -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/porter.list "
       "        -o Dir::Etc::sourceparts=- update >/dev/null 2>&1; "
       "export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a; ")


# --- examples/command -------------------------------------------------------

@pytest.mark.docker
def test_the_command_example_installs_a_binary_and_nothing_else(built, docker_image):
    """A command is a binary on PATH. The absences are the assertion.

    No unit and no /etc are not incidental: `assemble` refuses a command that
    declares config, ordering, a schedule or a migration, so a package that
    grew either would mean a refusal had stopped biting. And the interpreter
    path in the output is the positive control -- the image has no `python3`,
    so nothing but the vendored tree could have printed that line.
    """
    usb = built("command")
    result = _in_client(docker_image, usb, APT + (
        "set -e; apt-get install -y porter-example-command >/dev/null; "
        "porter-hello --version; "
        "porter-hello; "
        "test ! -e /usr/lib/systemd/system/porter-example-command.service && echo NO_UNIT; "
        "test ! -e /lib/systemd/system/porter-example-command.service && echo NO_UNIT_COMPAT; "
        "test ! -d /etc/porter-example-command && echo NO_ETC; "
        "test ! -d /var/lib/porter-example-command && echo NO_STATE; "
        "getent passwd porter-example-command || echo NO_USER"))
    assert result.returncode == 0, result.stderr
    assert "porter-hello 1.0" in result.stdout
    assert "/usr/lib/porter-example-command/python/bin/python3.12" in result.stdout, (
        "the command did not report the vendored interpreter as sys.executable, "
        f"so it is not clear what ran: {result.stdout}")
    for absent in ("NO_UNIT", "NO_UNIT_COMPAT", "NO_ETC", "NO_STATE", "NO_USER"):
        assert absent in result.stdout, f"{absent} missing from {result.stdout}"


@pytest.mark.docker
def test_the_command_resolves_relative_paths_against_the_callers_directory(
        built, docker_image):
    """The wrapper sets `PYTHONPATH` and does not `cd`, and this is the only
    place that distinction is observable.

    A wrapper that `cd`'d into /usr/lib/<pkg> -- the obvious way to put the
    payload on `sys.path` -- would resolve `porter-hello notes.txt` against the
    payload directory instead of the operator's, reading a file nobody named.
    The positive control is in the assertion itself: the expected string is the
    caller's directory and the refuted one is the payload's.
    """
    usb = built("command")
    result = _in_client(docker_image, usb, APT + (
        "set -e; apt-get install -y porter-example-command >/dev/null; "
        "mkdir -p /tmp/work && cd /tmp/work && porter-hello notes.txt"))
    assert result.returncode == 0, result.stderr
    assert "/tmp/work/notes.txt" in result.stdout, result.stdout
    assert "/usr/lib/porter-example-command/notes.txt" not in result.stdout


# --- examples/suite ---------------------------------------------------------

@pytest.mark.docker
def test_one_name_installs_one_machine_role(built, docker_image):
    """The claim the example exists to make, asserted against dpkg's database.

    Three assertions and the third is the one that makes the first two mean
    something: `desk` belongs to the *other* role and must NOT arrive. Without
    it, a metapackage that pulled in every package on the USB would pass.
    """
    usb = built("suite")
    result = _in_client(docker_image, usb, APT + (
        "set -e; apt-get install -y porter-example-suite-technical >/dev/null; "
        "dpkg-query -W -f='${Package} ${Status}\\n' 'porter-example-suite-*'; "
        "suite-console --version; "
        "test -f /usr/lib/systemd/system/porter-example-suite-sweep.timer && echo HAS_TIMER"))
    assert result.returncode == 0, result.stderr
    installed = {line.split()[0] for line in result.stdout.splitlines()
                 if "install ok installed" in line}
    assert installed == {"porter-example-suite-technical",
                         "porter-example-suite-api",
                         "porter-example-suite-sweep",
                         "porter-example-suite-console"}, result.stdout
    # The role is heterogeneous: a service, a scheduled job and a CLI behind one
    # name. The timer and the binary are each a different kind arriving.
    assert "HAS_TIMER" in result.stdout
    assert "suite-console 1.0" in result.stdout


@pytest.mark.docker
def test_a_role_will_not_install_without_its_components(built, docker_image):
    """The positive control for the test above.

    `Depends:` is a string in a control file until something enforces it. Here
    dpkg is handed the metapackage alone and must refuse: if it did not, the
    role would install on a machine with none of its components and report
    success -- and the test above would pass for a `Depends:` that was never
    read by anything.
    """
    usb = built("suite")
    result = _in_client(docker_image, usb, (
        "dpkg -i /media/usb/porter-example-suite-technical_1.0_all.deb"))
    assert result.returncode != 0, (
        "dpkg installed a metapackage with none of its dependencies present, so "
        f"Depends: is not being enforced: {result.stdout}")
    assert "dependency problems" in (result.stdout + result.stderr).lower()
    assert "porter-example-suite-api" in result.stdout + result.stderr


def test_a_metapackage_refuses_a_stage_that_is_not_empty(tmp_path):
    """The one refusal a metapackage needs, and nothing else would catch it.

    `build_deb`'s lint refuses top-level paths porter does not own -- and `usr`
    is one porter *does* own. So a component tree left in the role's stage
    directory passes every check in deb.py and is packaged into the role at
    rc=0: a 20 KB metapackage becomes a 91 MB one that owns
    `/usr/lib/<other-package>` and collides with the real owner on the client,
    at install time, on a machine with no network.

    The build is asserted to have produced no role .deb at all, not merely to
    have exited non-zero: a refusal that raises after writing the artefact
    leaves the wrong thing on the USB regardless of what the build said.
    """
    _require_uv()
    dist, stage = tmp_path / "dist", tmp_path / "stage"
    intruder = stage / "porter-example-suite-technical/usr/lib/porter-example-suite-api"
    intruder.mkdir(parents=True)
    (intruder / "leftover.py").write_text("# another package's payload\n")

    proc = subprocess.run(
        [_porter(), "build", str(EXAMPLES / "suite/porter.yaml"),
         "--out", str(dist), "--stage", str(stage)],
        capture_output=True, text=True)
    assert proc.returncode != 0, (
        "porter packaged a metapackage on top of another package's tree: "
        f"{proc.stdout}")
    assert "metapackage stage is not empty" in proc.stdout + proc.stderr
    assert not (dist / "porter-example-suite-technical_1.0_all.deb").exists()
    # The intruder is a caller's directory, so porter must not have removed it.
    assert (intruder / "leftover.py").exists()


def test_a_metapackage_is_a_control_file_and_no_payload(built):
    """Magnitude, in both directions.

    A role that accidentally carried the previously-staged component's tree
    would be a 91 MB "metapackage" owning another package's /usr/lib, and dpkg
    would report a file conflict on the client. Asserting only that the file
    exists cannot see that -- a 30 KB stub passed every path assertion in this
    repo once -- so the component debs are asserted large and the roles small,
    and `--contents` is asserted to list nothing but the root.
    """
    dist = built("suite")
    roles = sorted(dist.glob("*_all.deb"))
    components = sorted(dist.glob("*_amd64.deb"))
    assert len(roles) == 2 and len(components) == 4, (roles, components)

    for deb in components:
        assert deb.stat().st_size > 50 * 1024 * 1024, (
            f"{deb.name} is {deb.stat().st_size} bytes -- too small to contain a "
            "vendored interpreter, so something staged an empty tree")
    for deb in roles:
        assert deb.stat().st_size < 64 * 1024, (
            f"{deb.name} is {deb.stat().st_size} bytes: a metapackage has no "
            "payload, so this one is carrying something it should not")
        contents = subprocess.run(["dpkg-deb", "--contents", str(deb)],
                                  capture_output=True, text=True, check=True)
        paths = [line.split()[-1] for line in contents.stdout.splitlines()]
        assert paths == ["./"], f"{deb.name} ships {paths}"
        fields = subprocess.run(["dpkg-deb", "-f", str(deb)],
                                capture_output=True, text=True, check=True).stdout
        assert "Architecture: all" in fields, fields
        assert "Depends: porter-example-suite-" in fields, fields
