"""One interpreter, several components -- and the client that resolves it.

**The measurement this exists for.** A vendored python-build-standalone tree is
97 MB before a single dependency. ainbox has ten services; bundled, that is
~780 MB of the same bytes ten times over on one USB stick. `python:
{package: <name>}` makes it one package that every component `Depends:` on.

Until Task 13, `python.package` other than `bundled` was refused *by name*,
because nothing knew where a shared one would install. What replaced that
refusal has one property that cannot be assumed and is proved at the bottom of
this file: **installing a component off the USB repo, offline, with every
network apt source removed, pulls the interpreter package in on its own and the
service then runs.** Everything above that is the build-side half.
"""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import _require_uv

from porter.assemble import assemble, assemble_interpreter
from porter.repo import usb_tree
from porter.spec import load
from porter.types import Component, Python

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "shared-interpreter"
SHARED = Python(version="3.12", package="demo-python")
BUNDLED = Python(version="3.12", package="bundled")

# Every apt source that could rescue a repo that does not really carry the
# interpreter. `--network none` is not enough on its own: a cached index in
# /var/lib/apt/lists would let apt resolve a package name from a mirror. Copied
# rather than imported from tests/test_repo.py so the two files can be edited
# by different hands without one silently changing the other's control.
NO_NETWORK_SOURCES = (
    "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources "
    "/etc/apt/sources.list.d/*.list; rm -rf /var/lib/apt/lists/*;")


def _component(**kw) -> Component:
    base = dict(name="api", package="demo-api", description="d", kind="service",
                module="app", source_paths=["app.py"])
    base.update(kw)
    return Component(**base)


@pytest.fixture
def src(tmp_path) -> Path:
    root = tmp_path / "src"
    root.mkdir()
    (root / "app.py").write_text("import sys\n\n\ndef main():\n    print(sys.executable)\n")
    return root


@pytest.fixture(scope="session")
def interpreter(tmp_path_factory):
    """One shared interpreter package for the whole module -- it is 97 MB."""
    _require_uv()
    return assemble_interpreter(SHARED, tmp_path_factory.mktemp("interp") / "stage")


# --- the interpreter package -------------------------------------------------

def test_the_interpreter_is_a_package_of_its_own_versioned_by_cpython(interpreter):
    """A .deb holding a tree and nothing else, versioned by what is in it.

    `Version:` is the full CPython version the staged tree *reports*, asked of
    the binary rather than parsed out of a directory name or taken from the
    manifest -- the manifest says `3.12`, and what a component's compiled wheels
    were selected against is the patch release.

    The absences are half the claim: no unit, no /etc, no maintainer script.
    A package with a postinst would have something to go wrong on a client, and
    this one has nothing to do at all.
    """
    stage = interpreter.stage
    assert interpreter.version.startswith("3.12."), interpreter.version
    assert interpreter.control["Version"] == interpreter.version
    assert interpreter.control["Package"] == "demo-python"
    # Magnitude, not existence: a stub interpreter passes every path assertion
    # in this file, and a 30,912-byte package once did exactly that here.
    binary = stage / "usr/lib/demo-python/python/bin/python3.12"
    assert binary.is_file()
    total = sum(p.stat().st_size for p in stage.rglob("*")
                if p.is_file() and not p.is_symlink())
    assert total > 50_000_000, f"the interpreter stage is only {total} bytes"
    assert sorted(p.name for p in stage.iterdir()) == ["usr"]
    assert not (stage / "etc").exists()
    # Derived, and it matters: the tree's one dynamically-linked extension pulls
    # in libcrypt1, which no manifest names and which the components no longer
    # carry now that the interpreter is not theirs.
    assert "libc6" in interpreter.control["Depends"], interpreter.control
    assert "libcrypt1" in interpreter.control["Depends"], interpreter.control


def test_a_tree_that_is_not_the_declared_version_is_refused(interpreter):
    """GUARD, and it is a positive control on `vendor()` rather than on input.

    uv is *asked* for 3.12 and answers with a tree, and nothing between the
    request and the staged directory has so far confirmed the two agree. A 3.13
    tree under a `python3.12` ExecStart is a package that installs and cannot
    start -- and the version it is asked for is also the version the whole
    project's exact `Depends:` is built on, so the wrong answer propagates to
    every component at once.
    """
    from porter.assemble import _cpython_version
    with pytest.raises(RuntimeError, match="reports Python"):
        _cpython_version(interpreter.python_bin, "3.11")


def test_a_component_depends_on_the_interpreter_by_exact_version_and_ships_none(
        interpreter, src, tmp_path):
    """The two halves of "shared": the dependency is there, the tree is not.

    `(= 3.12.x)` and not `>=`: a client that kept an older interpreter
    satisfying a `>=` would run wheels compiled against a newer ABI -- an
    install at rc=0 and an ImportError in a unit's status.

    The magnitude assertion is the one that would catch a regression to
    bundling. Every path assertion in this file passes on a component that
    quietly vendored 97 MB of its own; the byte count does not.
    """
    staged = assemble(_component(), SHARED, src, tmp_path / "stage",
                      interpreter=interpreter)
    assert staged.control["Depends"].startswith(f"demo-python (= {interpreter.version})"), \
        staged.control["Depends"]
    libdir = staged.stage / "usr/lib/demo-api"
    assert not (libdir / "python").exists(), "the component bundled an interpreter too"
    total = sum(p.stat().st_size for p in staged.stage.rglob("*")
                if p.is_file() and not p.is_symlink())
    assert total < 1_000_000, f"the component stage is {total} bytes -- too big to be payload only"


def test_execstart_names_the_interpreter_packages_path_and_not_its_own(
        interpreter, src, tmp_path):
    """The client path, read off the emitted unit.

    An ExecStart under /usr/lib/demo-api/python would be a path that exists on
    the build host's stage and on no client -- rule 1's failure with a new
    doorway. The refuted string is the assertion that matters.
    """
    staged = assemble(_component(), SHARED, src, tmp_path / "stage",
                      interpreter=interpreter)
    unit = (staged.stage / "usr/lib/systemd/system/demo-api.service").read_text()
    assert "ExecStart=/usr/lib/demo-python/python/bin/python3.12 -m app" in unit, unit
    assert "/usr/lib/demo-api/python" not in unit, unit
    # WorkingDirectory stays the component's payload root: it is the import
    # root, and the interpreter's directory holds nothing importable.
    assert "WorkingDirectory=/usr/lib/demo-api" in unit, unit


def test_requirements_land_in_the_component_and_never_in_the_interpreter(
        interpreter, src, tmp_path):
    """Where a shared-interpreter component's wheels go, and where they do not.

    The interpreter's site-packages belongs to a package every component
    installs: two components writing `idna` there is a dpkg file conflict on
    the client, and it would rebuild 97 MB whenever either one's requirements
    changed. So `--target` puts them in /usr/lib/<pkg>/ -- already the unit's
    WorkingDirectory, hence already on sys.path, hence no new plumbing.

    The second assertion is the control: the shared stage is checked *after*
    the install, so a `--target` that silently did nothing would be caught.
    """
    staged = assemble(_component(requirements=["idna"]), SHARED, src,
                      tmp_path / "stage", interpreter=interpreter)
    libdir = staged.stage / "usr/lib/demo-api"
    assert (libdir / "idna/__init__.py").is_file(), sorted(p.name for p in libdir.iterdir())
    site = interpreter.stage / "usr/lib/demo-python/python/lib/python3.12/site-packages"
    assert not (site / "idna").exists(), (
        "the component's requirement was installed into the SHARED interpreter: "
        "two components wanting it would be a dpkg file conflict on the client")
    # uv's own byproducts, which rule 3 forbids running and nobody should ship.
    assert not (libdir / "bin").exists(), "console scripts with build-host shebangs shipped"
    assert not (libdir / ".lock").exists()


def test_bundled_is_unchanged(src, tmp_path):
    """REGRESSION. `package: bundled` must behave exactly as it did in slice 1.

    The interpreter is inside the package, ExecStart names the package's own
    path, and no `Depends:` entry carries an exact-version pin on anything.
    """
    _require_uv()
    staged = assemble(_component(), BUNDLED, src, tmp_path / "stage")
    assert (staged.stage / "usr/lib/demo-api/python/bin/python3.12").is_file()
    unit = (staged.stage / "usr/lib/systemd/system/demo-api.service").read_text()
    assert "ExecStart=/usr/lib/demo-api/python/bin/python3.12 -m app" in unit, unit
    assert "(= " not in staged.control.get("Depends", ""), staged.control


# --- the refusals ------------------------------------------------------------

def test_bundled_beside_a_shared_interpreter_is_refused(interpreter, src, tmp_path):
    """GUARD. The silent reading -- bundle anyway -- ships a second 97 MB tree
    the manifest did not ask for, at rc=0."""
    with pytest.raises(ValueError, match="'bundled'"):
        assemble(_component(), BUNDLED, src, tmp_path / "stage",
                 interpreter=interpreter)


def test_an_interpreter_for_another_package_is_refused(interpreter, src, tmp_path):
    """GUARD. The component would `Depends:` on one package and ExecStart the
    path of the other -- an install that resolves and a service that cannot
    start."""
    other = Python(version="3.12", package="some-other-python")
    with pytest.raises(ValueError, match="was handed"):
        assemble(_component(), other, src, tmp_path / "stage",
                 interpreter=interpreter)


def test_an_interpreter_built_for_another_version_is_refused(interpreter, src, tmp_path):
    """GUARD. ExecStart names python3.13; that package contains python3.12."""
    wrong = Python(version="3.13", package="demo-python")
    with pytest.raises(ValueError, match="python.version"):
        assemble(_component(), wrong, src, tmp_path / "stage",
                 interpreter=interpreter)


def test_a_source_entry_colliding_with_a_requirement_is_refused(
        interpreter, src, tmp_path):
    """GUARD. Requirements and source share one directory once the interpreter
    is shared.

    A payload package named after one of its own dependencies would otherwise
    ship as whichever landed second, and the manifest would still list both.
    `copytree` does raise here, but with a `FileExistsError` naming a staging
    path an adopter has never seen -- and the file case, one directory over,
    does not raise at all.
    """
    (src / "idna").mkdir()
    (src / "idna/__init__.py").write_text("VALUE = 'the payload, not the wheel'\n")
    with pytest.raises(ValueError, match="already there"):
        assemble(_component(requirements=["idna"], source_paths=["app.py", "idna"]),
                 SHARED, src, tmp_path / "stage", interpreter=interpreter)


def test_one_interpreter_name_with_two_versions_is_refused(tmp_path):
    """GUARD, in the loader. One package cannot hold two interpreters.

    porter would build whichever it staged last and the other component's
    ExecStart would name a python that is not in it -- an install apt resolves
    happily and a service that never starts.
    """
    manifest = tmp_path / "porter.yaml"
    manifest.write_text(yaml.safe_dump({
        "version": "1.0", "maintainer": "m", "architecture": "amd64",
        "components": [
            {"package": "a", "description": "d", "kind": "command",
             "bin_name": "a", "exec": {"module": "a"},
             "python": {"version": "3.12", "package": "proj-python"}},
            {"package": "b", "description": "d", "kind": "command",
             "bin_name": "b", "exec": {"module": "b"},
             "python": {"version": "3.13", "package": "proj-python"}},
        ]}))
    with pytest.raises(ValueError, match="two interpreters"):
        load(manifest)


def test_an_interpreter_name_a_component_already_claims_is_refused(tmp_path):
    """GUARD, in the loader. Two .debs, one filename, the second overwriting
    the first -- silently, at rc=0.

    `_refuse_two_packages_with_one_name` cannot see this: `python.package` is
    not a package *entry*, so the collision arrives through a key that check
    never reads.
    """
    manifest = tmp_path / "porter.yaml"
    manifest.write_text(yaml.safe_dump({
        "version": "1.0", "maintainer": "m", "architecture": "amd64",
        "package": "proj-python", "description": "d", "kind": "command",
        "bin_name": "x", "exec": {"module": "x"},
        "python": {"version": "3.12", "package": "proj-python"}}))
    with pytest.raises(ValueError, match="also the name of a package"):
        load(manifest)


# --- the gallery entry, and the property that cannot be assumed --------------

@pytest.fixture(scope="session")
def shared_usb(tmp_path_factory) -> Path:
    """`examples/shared-interpreter` built and laid out as a USB tree.

    Built from the gallery file itself: the acceptance test for an example is
    that *it* builds. `usb_tree` names one app -- the API service -- so apt is
    given exactly the name a sysadmin types and has to find the interpreter for
    itself.
    """
    _require_uv()
    porter = shutil.which("porter")
    assert porter, "the `porter` console script is not on PATH: use `uv run`"
    root = tmp_path_factory.mktemp("shared")
    dist = root / "dist"
    proc = subprocess.run(
        [porter, "build", str(EXAMPLE / "porter.yaml"),
         "--out", str(dist), "--stage", str(root / "stage")],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    debs = sorted(dist.glob("*.deb"))
    assert len(debs) == 3, [d.name for d in debs]
    return usb_tree(debs, root / "usb", app="porter-example-shared-api",
                    readme="porter's shared-interpreter example\n")


def test_the_example_builds_three_packages_and_only_one_of_them_is_large(shared_usb):
    """The saving, measured on the artefacts rather than asserted in prose.

    Two components at ~10 MB and ~20 KB beside one 80 MB interpreter. Bundled,
    the same two would be ~91 MB *each* -- this assertion is what would notice
    a regression to that, and no path assertion anywhere else would.
    """
    sizes = {d.name.split("_")[0]: d.stat().st_size
             for d in (shared_usb / "repo").glob("*.deb")}
    assert sizes["porter-example-shared-python"] > 50_000_000, sizes
    assert sizes["porter-example-shared-tool"] < 1_000_000, sizes
    assert sizes["porter-example-shared-api"] < 40_000_000, sizes


@pytest.mark.docker
def test_installing_a_component_offline_pulls_the_interpreter_and_the_service_runs(
        shared_usb, docker_image):
    """THE property. Nothing above it is worth much on its own.

    One name is installed -- the API service -- on a client with `--network
    none`, every network apt source deleted and every cached index removed, off
    an image asserted to carry no `python3`. If the interpreter package arrives,
    apt resolved it out of the USB repo by itself; if the service answers, the
    interpreter it ran is the one in that package and nothing else.

    Two controls, and they are not decoration:

    - The component's own directory must hold **no** `python/`. Without that,
      a regression that quietly bundled after all would pass every other
      assertion here, because the service would answer just as well.
    - The command line is read out of the **installed unit** -- `ExecStart=`
      verbatim, run from `WorkingDirectory=` -- rather than written here. A
      hand-written one supplies the very path the unit is there to supply.
    """
    unit = "/usr/lib/systemd/system/porter-example-shared-api.service"
    script = (
        "set -e; " + NO_NETWORK_SOURCES +
        # The only command the client runs. It names one package.
        "bash /media/usb/install.sh >/dev/null; "
        # Did apt bring the interpreter along on its own?
        "dpkg-query -W -f='INTERPRETER=${Package} ${Status}\\n' "
        "  porter-example-shared-python; "
        # Control: the component ships no interpreter of its own.
        "test ! -e /usr/lib/porter-example-shared-api/python && echo NO_BUNDLED_TREE; "
        "set -a; . /etc/porter-example-shared-api/defaults; "
        "  . /etc/porter-example-shared-api/env; set +a; "
        f"WD=$(sed -n 's/^WorkingDirectory=//p' {unit}); "
        f"ES=$(sed -n 's/^ExecStart=//p' {unit}); "
        'test -n "$WD"; test -n "$ES"; echo "ES=$ES"; '
        'cd "$WD"; eval "$ES" & '
        "sleep 5; curl -fsS http://127.0.0.1:${PORT}/health")
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{shared_usb}:/media/usb:ro", docker_image, "bash", "-c", script],
        capture_output=True, text=True)
    out = result.stdout + result.stderr
    assert "INTERPRETER=porter-example-shared-python install ok installed" in out, (
        "apt did not pull the interpreter package in: the component was "
        f"installed by name and its dependency was not resolved.\n{out}")
    assert "NO_BUNDLED_TREE" in out, (
        f"the component shipped an interpreter of its own after all:\n{out}")
    assert "ES=/usr/lib/porter-example-shared-python/python/bin/python3.12" in out, out
    assert '"status":"ok"' in out, out
    assert '"python":"/usr/lib/porter-example-shared-python/python/bin/python3.12"' in out, (
        f"the service answered from some other interpreter:\n{out}")
    assert result.returncode == 0, out


@pytest.mark.docker
def test_the_command_component_shares_the_same_interpreter(shared_usb, docker_image):
    """The second half of "shared": two packages, one interpreter, one copy.

    Installed from the same repo in the same container, so `dpkg -L` can be
    asked the question that matters -- neither component owns a `python/`
    directory, and the one they both run belongs to the third package.
    """
    script = (
        "set -e; " + NO_NETWORK_SOURCES +
        "echo 'deb [trusted=yes] file:/media/usb/repo ./' > /etc/apt/sources.list.d/p.list; "
        "apt-get -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/p.list "
        "  -o Dir::Etc::sourceparts=- update >/dev/null 2>&1; "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "  porter-example-shared-tool >/dev/null; "
        "porter-shared-tool; "
        "dpkg -L porter-example-shared-tool | grep -c '/python/bin/' || echo NO_TREE_IN_TOOL")
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{shared_usb}:/media/usb:ro", docker_image, "bash", "-c", script],
        capture_output=True, text=True)
    out = result.stdout + result.stderr
    assert "TOOL_OK python=/usr/lib/porter-example-shared-python/python/bin/python3.12" in out, out
    assert "NO_TREE_IN_TOOL" in out, (
        f"the command component owns interpreter files of its own:\n{out}")
    assert result.returncode == 0, out
