"""The split, proved on a client: core headless, desktop apart.

Rule 12 says a desktop dependency never enters the core package, and every
assertion elsewhere in this suite is about strings in a control file. This is
the one that installs. The property is not "the core package has no GUI
dependency in its `Depends:`" -- that is a restatement of the code -- it is
"the core package installs on a machine where the desktop package's
dependencies do not exist", and only dpkg can say that.

The three tests are one argument and none of them stands alone:

1. the core package configures on a bare Debian image, asserted to have neither
   `curl` nor `xdg-open`;
2. the **desktop** package is refused on that same image, naming what is
   missing -- without which (1) proves only that the image happened to satisfy
   whatever was asked of it;
3. the desktop package configures on an image that has those tools -- without
   which (2) proves only that the package is broken.

`--network none` throughout: an install that reaches out is not an airgapped
install, and apt would otherwise quietly fetch the very packages (2) is about.
"""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import _require_docker, _require_uv

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "desktop-app"

# Bare. Nothing added, because what this image must have is *nothing*: it is
# the headless server the core package has to install on.
HEADLESS = "debian:bookworm-slim"

# The same base plus the two tools the launcher runs. Built with network (image
# builds have one; the containers below do not) so that test 3 can show the
# desktop package installing rather than merely failing differently.
GUI_DOCKERFILE = f"""FROM {HEADLESS}
RUN apt-get update \\
 && apt-get install -y --no-install-recommends curl xdg-utils \\
 && rm -rf /var/lib/apt/lists/*
"""


@pytest.fixture(scope="session")
def manifest() -> dict:
    return yaml.safe_load((EXAMPLE / "porter.yaml").read_text())


@pytest.fixture(scope="session")
def desktop_debs(tmp_path_factory, manifest) -> dict[str, Path]:
    """Both packages, built through `porter build` the way a user reaches it.

    The console script and not an in-process call: the desktop package exists
    only because `cli.build` notices the `desktop:` block, and an in-process
    call would test `assemble_desktop` while leaving the one line that decides
    whether a second .deb is ever produced unexercised.

    Alone in their own directory -- the containers mount it and install
    `/debs/<name>.deb` by name, but a stray third package here would mean the
    manifest grew one nobody noticed.
    """
    _require_uv()
    exe = shutil.which("porter")
    if exe is None:
        pytest.fail("the `porter` console script is not on PATH; use "
                    "`uv run --extra dev pytest`", pytrace=False)
    work = tmp_path_factory.mktemp("desktop")
    shutil.copytree(EXAMPLE, work / "example")
    proc = subprocess.run([exe, "build", "example/porter.yaml"],
                          cwd=work, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    pkg, version = manifest["package"], manifest["version"]
    arch = manifest["architecture"]
    debs = {"dir": work / "dist",
            "core": work / f"dist/{pkg}_{version}_{arch}.deb",
            "desktop": work / f"dist/{pkg}-desktop_{version}_{arch}.deb"}
    built = sorted(p.name for p in debs["dir"].glob("*.deb"))
    assert built == sorted([debs["core"].name, debs["desktop"].name]), built
    # Magnitude, not presence. A 30 KB stub passed every path assertion in this
    # repo once: the core package carries a vendored interpreter and cannot be
    # small, and the desktop package carries three files and cannot be large.
    assert debs["core"].stat().st_size > 20_000_000, debs["core"].stat().st_size
    assert 1_000 < debs["desktop"].stat().st_size < 100_000, (
        f"{debs['desktop']} is {debs['desktop'].stat().st_size} bytes; a "
        "launcher, a .desktop entry and a 256px PNG are neither of those sizes")
    return debs


@pytest.fixture(scope="session")
def headless_image() -> str:
    """A client with no browser tooling at all, asserted rather than assumed.

    The same argument as `conftest.docker_image`'s python3 probe: on a base
    that happened to ship `xdg-utils`, the refusal test below would pass with
    the split completely broken.
    """
    _require_docker()
    pull = subprocess.run(["docker", "pull", "-q", HEADLESS],
                          capture_output=True, text=True)
    assert pull.returncode == 0, pull.stderr
    for tool in ("curl", "xdg-open"):
        probe = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", HEADLESS,
             "sh", "-c", f"command -v {tool}"], capture_output=True, text=True)
        assert probe.returncode != 0, (
            f"{HEADLESS} ships {tool} at {probe.stdout.strip()!r}: this image "
            "cannot show that the desktop package's dependencies are absent")
    return HEADLESS


@pytest.fixture(scope="session")
def gui_image() -> str:
    _require_docker()
    tag = "porter-test-client:desktop"
    build = subprocess.run(["docker", "build", "-q", "-t", tag, "-"],
                           input=GUI_DOCKERFILE, capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    return tag


def _run(image: str, debs: Path, script: str) -> subprocess.CompletedProcess:
    """One container, no network, with the built packages mounted read-only."""
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{debs}:/debs:ro", image, "sh", "-c", script],
        capture_output=True, text=True)


@pytest.mark.docker
def test_the_core_package_installs_where_no_desktop_dependency_exists(
        desktop_debs, headless_image, manifest):
    """The property the whole split exists for.

    A GUI needs GTK, X11 and NSS from the client and apt cannot fetch them on
    an airgapped box, so a desktop dependency in the core package would not
    make a server install awkward -- it would make it impossible, at the one
    moment there is nobody watching. This image has neither of the two tools
    the desktop package needs and no network to get them.

    `dpkg-query -W` and not `dpkg -s | grep`: a pipeline hands the caller
    `grep`'s exit code, and "unpacked" versus "install ok installed" is exactly
    the distinction that would be lost.
    """
    pkg = manifest["package"]
    proc = _run(headless_image, desktop_debs["dir"], f"""
set -e
dpkg -i /debs/{desktop_debs['core'].name}
test "$(dpkg-query -W -f='${{Status}}' {pkg})" = "install ok installed"
test -x /usr/lib/{pkg}/python/bin/python3.12
echo REACHED_THE_END
""")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REACHED_THE_END" in proc.stdout, (
        "the script exited 0 without running to completion: " + proc.stdout)


@pytest.mark.docker
def test_the_desktop_package_is_refused_on_that_same_image(
        desktop_debs, headless_image, manifest):
    """The positive control, and it is what makes the test above mean anything.

    Without it, "the core package installs here" is compatible with an image
    that satisfies everything asked of it. dpkg must refuse to *configure* the
    desktop package and name what is missing.
    """
    pkg = manifest["package"]
    proc = _run(headless_image, desktop_debs["dir"], f"""
dpkg -i /debs/{desktop_debs['core'].name} >/dev/null 2>&1
dpkg -i /debs/{desktop_debs['desktop'].name}
echo "REFUSED_WITH=$?"
test "$(dpkg-query -W -f='${{Status}}' {pkg})" = "install ok installed"
""")
    # The script's LAST command is the core package's status check, so this is
    # also the assertion that a refused desktop package left the service
    # installed rather than taking it down.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REFUSED_WITH=1" in proc.stdout, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "xdg-utils" in combined and "curl" in combined, combined


@pytest.mark.docker
def test_the_desktop_package_installs_where_its_dependencies_do_exist(
        desktop_debs, gui_image, manifest):
    """The other control: the refusal above is about the image, not the package.

    Also the only place the staged layout is checked against a real dpkg
    unpack. `Exec=` and the hicolor path are strings until something puts them
    on a filesystem.
    """
    pkg = manifest["package"]
    proc = _run(gui_image, desktop_debs["dir"], f"""
set -e
dpkg -i /debs/{desktop_debs['core'].name} /debs/{desktop_debs['desktop'].name}
test "$(dpkg-query -W -f='${{Status}}' {pkg}-desktop)" = "install ok installed"
test -x /usr/bin/{pkg}-desktop
test -f /usr/share/applications/{pkg}-desktop.desktop
test -f /usr/share/icons/hicolor/256x256/apps/{pkg}-desktop.png
bash -n /usr/bin/{pkg}-desktop
echo REACHED_THE_END
""")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REACHED_THE_END" in proc.stdout, proc.stdout


def test_the_core_package_declares_the_dependencies_it_derived(desktop_debs):
    """Derived, and non-empty. `libcrypt1` is the whole point of the module.

    Nothing in the manifest names it; it arrives through the single
    dynamically-linked extension in the vendored interpreter. A hand-written
    `Depends:` would not have it, and its absence on a client is an interpreter
    that imports `crypt` and dies.
    """
    depends = subprocess.run(
        ["dpkg-deb", "--field", str(desktop_debs["core"]), "Depends"],
        capture_output=True, text=True).stdout.strip()
    names = {d.split("(")[0].strip() for d in depends.split(",") if d.strip()}
    assert "libc6" in names, depends
    assert "libcrypt1" in names, depends


def test_the_core_package_declares_nothing_the_desktop_package_needs(
        desktop_debs, manifest):
    """Rule 12 read off the two artefacts, with no porter code in the loop."""
    def field(deb: Path, name: str) -> set[str]:
        out = subprocess.run(["dpkg-deb", "--field", str(deb), name],
                             capture_output=True, text=True).stdout
        return {d.split("(")[0].strip() for d in out.split(",") if d.strip()}

    core = field(desktop_debs["core"], "Depends")
    desktop = field(desktop_debs["desktop"], "Depends") - {manifest["package"]}
    assert desktop, "the desktop package declares no dependencies at all"
    assert not (core & desktop), sorted(core & desktop)
