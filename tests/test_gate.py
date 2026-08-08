"""The gate, and the two bundles that must make it go red.

A gate that cannot fail is worth less than no gate, because it licenses
shipping. So the good bundle is one test and the mutations are the other two,
and each mutation asserts on the *specific* failure it is supposed to produce
rather than merely on `not ok` -- a mutation that goes red for an incidental
reason is a mutation that proves nothing about the check it names
(`know-how/mutation-testing-a-guard.md`, trap 3).

The two bundles are built from the same session fixtures as the rest of the
suite, so a vendored interpreter is materialised once and not three times.
"""
import shutil

import pytest
from conftest import EXAMPLE, _require_uv

from porter.config import env_postinst, split
from porter.deb import build_deb
from porter.gate import gate, versions_in
from porter.repo import usb_tree
from porter.systemd import unit

HEALTH = "http://127.0.0.1:9000/health"
SEED = {"/var/lib/demo-app/state.db": "client-data written by the operator\n"}


# --- the version-ordering guard, which needs no container --------------------

def test_the_upgrade_path_is_ordered_by_dpkg_and_not_lexically(tmp_path):
    """GUARD. `1.9` sorts after `1.10` as a string and before it to dpkg.

    examples/stateful-service is numbered 1.9 -> 1.10 -> 1.11 for exactly this
    reason. A gate that picked v_prev lexically would install the *newer*
    package, "upgrade" to the older one, find every seeded file untouched and
    report a pass for an upgrade that never happened -- porter's characteristic
    bug, in the one module written to catch it.

    The assertion is on the order and not merely on the set, and `sorted()` is
    computed alongside it so the test states what it is discriminating against.
    """
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo/Packages").write_text(
        "Package: demo-app\nVersion: 1.9\nFilename: ./a.deb\n"
        "\n"
        "Package: demo-app\nVersion: 1.10\nFilename: ./b.deb\n"
        "\n"
        # Another package's versions, which must not enter demo-app's path.
        "Package: other-app\nVersion: 9.9\nFilename: ./c.deb\n")
    assert versions_in(tmp_path, "demo-app") == ["1.9", "1.10"]
    assert sorted(["1.9", "1.10"]) == ["1.10", "1.9"], (
        "lexical order no longer disagrees with dpkg here, so this test has "
        "stopped discriminating -- pick versions where it does")


def test_a_health_url_that_could_run_shell_is_refused(tmp_path):
    """GUARD. `health_url` is interpolated into the gate's own shell.

    `; echo ok` in a URL does not merely break the probe -- `curl ... ; true`
    exits 0, so the gate would report a healthy service with nothing listening.
    That is a false pass produced by the gate itself, which is the one bug this
    module may not have.
    """
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo/Packages").write_text("Package: demo-app\nVersion: 1.0\n")
    for bad in ("http://127.0.0.1:9000/health; true",
                "http://127.0.0.1:9000/health && echo ok",
                "http://example.com/health",
                "http://127.0.0.1:9000/$(id)"):
        with pytest.raises(ValueError, match="loopback"):
            gate(tmp_path, app="demo-app", image="unused", health_url=bad, seed={})


# --- the bundles -------------------------------------------------------------

@pytest.fixture(scope="session")
def good_usb(built_demo_deb, built_demo_deb_v2, tmp_path_factory):
    """v1.0 and v1.1 of the gallery's service, on a real USB tree.

    The same two .debs the rest of the suite installs, published through
    `porter.repo.usb_tree` -- so what the gate exercises is the delivery
    artefact and not a directory of packages a test assembled.
    """
    return usb_tree([built_demo_deb, built_demo_deb_v2],
                    tmp_path_factory.mktemp("good") / "usb",
                    app="demo-app", readme="Installation\n")


@pytest.fixture(scope="session")
def state_eating_usb(built_demo_deb, built_demo_deb_v2, demo_stage,
                     demo_manifest, tmp_path_factory):
    """MUTATION 1. v1.1's postinst deletes /var/lib/demo-app.

    Rule 6: the package never writes to /var/lib/<pkg>, that directory is the
    client's. This is the failure une-tools' `smoke-update.sh` exists to catch,
    and it is invisible to every other check in the suite -- the package
    installs, the version moves, the service answers, and the client's data is
    gone.

    Built from the same stage as the real v1.1 and depending on it, because the
    two write their config into that stage in turn.
    """
    _require_uv()
    root = tmp_path_factory.mktemp("eater")
    postinst = env_postinst("demo-app").replace(
        "  mkdir -p /etc/demo-app\n",
        "  # THE MUTATION: the package helpfully tidies up the client's state.\n"
        "  rm -rf /var/lib/demo-app\n"
        "  mkdir -p /etc/demo-app\n",
    )
    assert "rm -rf /var/lib/demo-app" in postinst, "the mutation did not apply"
    v2 = _rebuild(demo_stage, demo_manifest, root / "debs", version="1.1",
                  postinst=postinst)
    return usb_tree([built_demo_deb, v2], root / "usb",
                    app="demo-app", readme="x")


@pytest.fixture(scope="session")
def truncated_usb(built_demo_deb_v2, demo_stage, demo_manifest,
                  tmp_path_factory):
    """MUTATION 2. v1.0 is a stub: every path exists, the tree is ~40 KB.

    Truncating **v_prev** rather than v_next is what makes this isolate the
    magnitude check. A stubbed v_next would also take the health assertion down,
    and a mutation that goes red for two reasons cannot say which check caught
    it (trap 4). Here the upgrade to the real 1.1 repairs the tree, so the
    service answers, the state survives, the versions move -- and the only red
    is the size of what was installed first.

    The stub is deliberately generous: it ships the unit, the conffile, the
    env template, the payload module and an executable at the interpreter's
    exact installed path. Every path assertion in this repo passes against it.
    That is not a strawman -- it is the 30,912-byte package that was reported as
    built during the design.
    """
    _require_uv()
    root = tmp_path_factory.mktemp("stub")
    stub = _stub_package(demo_manifest, root / "stage", root / "debs")
    return usb_tree([stub, built_demo_deb_v2], root / "usb",
                    app="demo-app", readme="x")


def _rebuild(stage, manifest, out, *, version, postinst):
    """The gallery package again, with a maintainer script of our choosing."""
    pkg = manifest["package"]
    defaults, env_example = split(manifest["env"], manifest["admin_keys"])
    (stage / "etc" / pkg / "defaults").write_text(defaults)
    (stage / "usr/share" / pkg / "env.example").write_text(env_example)
    return build_deb(stage, {
        "Package": pkg,
        "Version": version,
        "Architecture": manifest["architecture"],
        "Maintainer": manifest["maintainer"],
        "Description": manifest["description"],
    }, out, conffiles=[f"/etc/{pkg}/defaults"], scripts={"postinst": postinst})


def _stub_package(manifest, stage, out):
    """Every path the real package ships, and none of its substance."""
    pkg, pyver = manifest["package"], manifest["python"]["version"]
    libdir = stage / "usr/lib" / pkg
    (libdir / "python/bin").mkdir(parents=True)
    # Executable, and it exits 0. That is the shape of the burn: a probe
    # satisfied by something that runs and answers nothing -- the same class as
    # a memory-limit check satisfied by `command not found`.
    interpreter = libdir / f"python/bin/python{pyver}"
    interpreter.write_text("#!/bin/sh\n# a stub interpreter\nexit 0\n")
    interpreter.chmod(0o755)
    shutil.copy(EXAMPLE / "src/app.py", libdir / "app.py")

    installed = f"/usr/lib/{pkg}/python/bin/python{pyver}"
    exec_start = " ".join(
        [installed, "-m", manifest["exec"]["module"], *manifest["exec"]["args"]])
    units = stage / "usr/lib/systemd/system"
    units.mkdir(parents=True)
    (units / f"{pkg}.service").write_text(
        unit(pkg, manifest["description"], exec_start, f"/usr/lib/{pkg}"))

    defaults, env_example = split(manifest["env"], manifest["admin_keys"])
    (stage / "etc" / pkg).mkdir(parents=True)
    (stage / "etc" / pkg / "defaults").write_text(defaults)
    (stage / "usr/share" / pkg).mkdir(parents=True)
    (stage / "usr/share" / pkg / "env.example").write_text(env_example)
    return build_deb(stage, {
        "Package": pkg,
        "Version": manifest["version"],
        "Architecture": manifest["architecture"],
        "Maintainer": manifest["maintainer"],
        "Description": manifest["description"],
    }, out, conffiles=[f"/etc/{pkg}/defaults"],
        scripts={"postinst": env_postinst(pkg)})


# --- the gate ----------------------------------------------------------------

@pytest.mark.docker
def test_gate_passes_a_good_bundle(good_usb, docker_image):
    """The bundle porter is meant to ship, proved end to end.

    Every assertion inside carries its own control, so a green here is not one
    claim but eighteen. What it does NOT claim: that systemd starts the unit.
    No unit has ever been started by systemd in this project -- containers have
    no PID 1 systemd, and that is Task 14's nspawn work.
    """
    result = gate(good_usb, app="demo-app", image=docker_image,
                  health_url=HEALTH, seed=SEED)
    assert result.ok, f"{result.failures}\n{result.log}"


@pytest.mark.docker
def test_gate_fails_when_the_package_eats_client_state(state_eating_usb, docker_image):
    """MUTATION TEST. v1.1's postinst deletes /var/lib/demo-app.

    The assertion is on the state message specifically. `not result.ok` alone
    would be satisfied by any of the eighteen checks going red, and would then
    stay green if the state comparison were deleted outright.
    """
    result = gate(state_eating_usb, app="demo-app", image=docker_image,
                  health_url=HEALTH, seed=SEED)
    assert not result.ok, (
        "a package that deletes the client's state directory passed the gate; "
        f"the gate is broken, not the package\n{result.log}")
    assert any("client state" in f for f in result.failures), result.failures
    assert any("the file is gone" in f for f in result.failures), result.failures
    # ...and nothing ELSE went wrong, which is what makes the message above
    # attributable. The upgrade succeeded, the service answered; the only thing
    # that failed is the client's data.
    assert len(result.failures) == 1, (
        f"expected the state check alone to fire:\n{result.failures}")


@pytest.mark.docker
def test_a_gate_that_cannot_seed_says_so_rather_than_reporting_state_intact(
        good_usb, docker_image):
    """POSITIVE CONTROL for every state assertion above.

    /media/usb is mounted read-only, so a seed aimed there cannot land. Without
    the `before == want` half, both digests come back empty, empty compares
    equal to empty, and the gate reports the client's state as perfectly
    preserved -- a pass produced by nothing having happened at all. That is
    porter's characteristic bug inside the tool written to catch it, and it is
    the reason the seed is hashed on the way in and not only on the way out.
    """
    result = gate(good_usb, app="demo-app", image=docker_image,
                  health_url=HEALTH, seed={"/media/usb/porter-gate-probe": "x"})
    assert not result.ok, (
        "the gate reported client state intact for a file it never managed to "
        f"write\n{result.log}")
    assert any("could not seed" in f for f in result.failures), result.failures


@pytest.mark.docker
def test_gate_fails_when_the_payload_is_truncated(truncated_usb, docker_image):
    """MUTATION TEST. v1.0 is a 40 KB stub with every path in place.

    Two checks fire and they are not the same check -- magnitude (`du` of the
    installed tree) and provenance (the interpreter reporting its own
    `sys.executable`). TRAP 4 APPLIES and it is deliberate: a real stub defeats
    neither, so each is asserted by name here rather than left to a shared
    `not result.ok`, and each has its own entry in the guard registry.

    What must NOT fire is anything else: the upgrade to the real 1.1 repairs the
    tree, so the versions move and the service answers. A truncated package is
    invisible to every other assertion in this file, which is the whole point.
    """
    result = gate(truncated_usb, app="demo-app", image=docker_image,
                  health_url=HEALTH, seed={})
    assert not result.ok, (
        "a package whose payload is a stub passed the gate; every path "
        f"assertion in it holds, which is exactly why magnitude exists\n{result.log}")
    assert any("truncated package" in f for f in result.failures), result.failures
    assert any("sys.executable" in f for f in result.failures), result.failures
    assert not any("did not answer" in f for f in result.failures), (
        "the service failed to answer as well, so the two assertions above are "
        f"no longer attributable to the stub:\n{result.failures}")
    # A verdict about a machine the caller cannot see is only as useful as what
    # it carries. Asserted here rather than in a test of its own, because a
    # second full gate run costs another two minutes to learn nothing new.
    assert "DONE=yes" in result.log and "--- install v_prev" in result.log
