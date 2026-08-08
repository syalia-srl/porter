"""The gate that starts the unit.

Until this file existed, no **service** unit had been started by systemd in this
project. `User=`, `StateDirectory=`, `ProtectSystem=strict` and
`Restart=on-failure` were assertions about a *file*, backed by
`systemd-analyze verify` -- which reports keys it does not recognise and **never
keys that are missing**, so deleting `ProtectSystem=strict` leaves it perfectly
clean (measured 2026-08-08). The container e2e ran the shipped `ExecStart=` as
root from the shipped `WorkingDirectory=`, with no PID 1, no namespace and no
supervision: the unit is syntactically valid and the program runs, and that the
two *compose* was undemonstrated.

Three boots, and the two mutants are what make the first mean anything -- a gate
that cannot fail licenses shipping:

- a good bundle, where every directive must be observed doing its job;
- **MUTATION 1**, the shipped `ExecStart=` naming a module that is not there:
  the package must still install at rc=0 and the gate must still go red, on the
  unit never reaching a settled `active`;
- **MUTATION 2**, a unit with `User=`, `ProtectSystem=` and `Restart=` deleted:
  it installs, comes up and answers, `systemd-analyze verify` is clean on it,
  and the gate must still go red on each missing directive separately.

The two are kept apart deliberately. A mutation that goes red for two unrelated
reasons cannot say which check caught it (trap 4): the first isolates liveness,
the second isolates hardening and supervision on a service that runs fine.

Both are applied to the **built .deb**, by unpacking and repacking it, and never
to the shared session stage: `demo_stage` is conftest's and every other module
builds from it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from porter.gate import nspawn_available, nspawn_gate, nspawn_root
from porter.repo import usb_tree

HEALTH = "http://127.0.0.1:9000/health"
# The real package carries a vendored interpreter and is ~97 MB unpacked. The
# floor is the magnitude check: a stubbed tree where every path exists was
# 30,912 bytes, and passed every path assertion in this repo.
MIN_PAYLOAD_KB = 20 * 1024


def require_nspawn() -> None:
    """The fourth arming variable, and -- unlike the first three -- it is now in
    CI too.

    It was left out while it was unknown whether a GitHub-hosted runner can boot
    nspawn, and arming a gate that cannot be satisfied turns every run red for a
    reason unrelated to the change under test. That was measured on 2026-08-08
    rather than argued: an `ubuntu-latest` runner installs `systemd-container`,
    exports a Debian root through docker, boots it `--private-network --boot`
    with systemd as PID 1, isolates to `lo` alone, and applies
    `ProtectSystem=strict` to a transient unit -- in 33 seconds. So the hole
    AGENTS.md's rule pointed at is closed rather than excused.
    """
    reason = nspawn_available()
    if reason is None:
        return
    if os.environ.get("PORTER_REQUIRE_NSPAWN", "") not in ("", "0"):
        pytest.fail(
            f"PORTER_REQUIRE_NSPAWN is set and {reason}: this run would have "
            "skipped the only tests in porter that start a unit with systemd.",
            pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Path:
    """One bootable root for both boots.

    Reusable because `nspawn_gate` boots it `--ephemeral`: each run writes to a
    throwaway copy and the source tree is untouched, so the mutant is never
    gated against a client the good bundle already installed into. Verified
    2026-08-08 -- a file created inside a boot is absent from the source root.
    """
    require_nspawn()
    made = nspawn_root(tmp_path_factory.mktemp("nspawn-gate"))
    yield made
    # Extracted as root, so pytest's own cleanup cannot remove it and ~100 MB
    # per run accumulates in /tmp -- which is tmpfs on this build host, i.e. RAM.
    subprocess.run(["sudo", "-n", "rm", "-rf", str(made)], capture_output=True)


@pytest.fixture(scope="module")
def good_usb(built_demo_deb_v2, tmp_path_factory) -> Path:
    """The gallery's service, published through porter's own `usb_tree`.

    One version, not two: the upgrade path is the docker gate's subject. This
    gate's question is whether systemd brings the thing up.
    """
    return usb_tree([built_demo_deb_v2], tmp_path_factory.mktemp("good") / "usb",
                    app="demo-app", readme="Installation\n")


@pytest.fixture(scope="module")
def broken_exec_usb(built_demo_deb_v2, tmp_path_factory) -> Path:
    """THE MUTATION: the shipped `ExecStart=` names a module that is not there.

    Chosen so the package still **installs**. `Type=` is unset, so systemd
    treats the unit as `Type=simple` and `systemctl start --no-block` reports
    success the moment the fork does -- the interpreter then exits 1, dpkg exits
    0, and apt is entirely satisfied. That is the failure this gate exists for:
    an install that succeeds and a service that is not there.

    Applied by unpacking and repacking the built .deb, so the shared session
    stage every other module builds from is never touched, and so what is
    mutated is the artefact a client would receive.
    """
    def break_exec(body: str) -> str:
        mutated = body.replace("-m uvicorn ", "-m uvicorn_is_not_installed ")
        assert "-m uvicorn_is_not_installed app:app" in mutated, mutated
        return mutated

    work = tmp_path_factory.mktemp("broken")
    deb = _repack(built_demo_deb_v2, work, break_exec)
    return usb_tree([deb], work / "usb", app="demo-app", readme="x")


def _repack(deb: Path, work: Path, mutate) -> Path:
    """Unpack a built .deb, let `mutate` rewrite its unit, repack it.

    Never touches conftest's session stage, which every other module builds
    from, and mutates the artefact a client would actually receive.
    """
    tree = work / "tree"
    unpack = subprocess.run(["dpkg-deb", "-R", str(deb), str(tree)],
                            capture_output=True, text=True)
    assert unpack.returncode == 0, unpack.stderr

    unit_file = tree / "usr/lib/systemd/system/demo-app.service"
    body = unit_file.read_text()
    mutated = mutate(body)
    # Trap 2: a "mutation" that changes bytes and not behaviour is worse than
    # none -- it reports a working gate as broken.
    assert mutated != body, f"the mutation did not apply:\n{body}"
    unit_file.write_text(mutated)

    out = work / "debs"
    out.mkdir()
    made = out / deb.name
    pack = subprocess.run(["dpkg-deb", "-b", str(tree), str(made)],
                          capture_output=True, text=True)
    assert pack.returncode == 0, pack.stderr
    # Magnitude on the repacked artefact: a repack that dropped the payload
    # would fail the gate for a reason that reads exactly like the mutation's
    # (trap 3).
    assert made.stat().st_size > 20 * 1024 * 1024, (
        f"the repacked .deb is {made.stat().st_size} bytes -- the payload did "
        "not survive dpkg-deb -R/-b, so a red gate would not be the mutation")
    shutil.rmtree(tree)
    return made


@pytest.fixture(scope="module")
def unhardened_usb(built_demo_deb_v2, tmp_path_factory) -> Path:
    """MUTATION 2: the unit keeps working and loses everything that protects it.

    `User=`, `ProtectSystem=strict` and `Restart=on-failure` are deleted. The
    service still installs, still comes up and still answers -- which is the
    entire point. This is the failure `systemd-analyze verify` **cannot** see:
    it reports keys it does not recognise and never keys that are missing, so a
    unit with `ProtectSystem=strict` deleted passes it perfectly clean (measured
    2026-08-08). Every file-level assertion in the rest of this suite is written
    against the emitted unit, and this .deb's unit simply does not contain the
    lines they would look for.

    Isolated from MUTATION 1 on purpose: there the service never starts, here it
    starts fine. A mutation that goes red for two unrelated reasons cannot say
    which check caught it (trap 4).
    """
    def strip(body: str) -> str:
        return (body
                .replace("User=demo-app\n", "User=root\n")
                .replace("ProtectSystem=strict\n", "")
                .replace("Restart=on-failure\nRestartSec=3\n", ""))

    work = tmp_path_factory.mktemp("unhardened")
    deb = _repack(built_demo_deb_v2, work, strip)
    return usb_tree([deb], work / "usb", app="demo-app", readme="x")


# --- refusals, before any container ------------------------------------------

def test_a_health_url_that_could_run_shell_is_refused(tmp_path):
    """Same refusal as the docker gate, for the same reason: the URL is
    interpolated into a shell command inside the container, so one carrying `;`
    could make the probe report success with nothing listening -- a false pass
    produced by the gate itself."""
    for bad in ("http://127.0.0.1:9000/health; true",
                "http://example.com/health",
                "http://127.0.0.1:9000/$(id)"):
        with pytest.raises(ValueError, match="loopback"):
            nspawn_gate(tmp_path, tmp_path, app="demo-app", health_url=bad)


def test_an_app_name_that_is_not_a_debian_package_name_is_refused(tmp_path):
    with pytest.raises(ValueError, match="Debian package name"):
        nspawn_gate(tmp_path, tmp_path, app="demo app", health_url=HEALTH)


# --- the two boots -----------------------------------------------------------

@pytest.mark.nspawn
def test_the_service_comes_up_under_real_systemd_and_survives_being_killed(
        root, good_usb):
    """The end of the path, and the part no file can establish.

    systemd installs the bundle with the client's own `install.sh`, and then --
    with the probe starting **nothing** -- the postinst's
    `systemctl start --no-block` has to leave a unit that is `active`, running
    as the static system user, inside a `StateDirectory=` systemd created, under
    a `ProtectSystem=strict` shown to actually block writes on this kernel, and
    answering HTTP. Then its process is killed and systemd has to put it back.

    Every one of those carries its own control inside `nspawn_gate`, including
    three taken *before* the install -- nothing answers the URL, /var/lib/demo-app
    does not exist, the demo-app user does not exist -- so none of the
    afterwards can be satisfied by something the base image already had.

    It also exercises the one thing the split-config design rests on and no
    other test can reach: systemd reads `/etc/demo-app/env` at `600 root:root`
    as root and *then* drops to the service user. The app does
    `os.environ["GREETING"]` with no default, so a unit that never read it
    500s on the first request rather than serving a plausible answer.
    """
    result = nspawn_gate(root, good_usb, app="demo-app", health_url=HEALTH,
                         min_payload_kb=MIN_PAYLOAD_KB)
    assert result.ok, "\n".join(result.failures) + "\n\n" + result.log
    assert result.failures == []


@pytest.mark.nspawn
def test_the_gate_detects_a_unit_that_installs_and_then_fails_to_start(
        root, broken_exec_usb):
    """THE control for the test above. Without it the nspawn path is decoration.

    The discrimination that matters is not "it went red" -- it is **red while
    the install stayed green**. A mutation that broke the package would take the
    gate down through `INSTALL_RC`, which the docker gate already catches, and
    prove nothing about systemd having been asked to start anything. So this
    asserts all three:

    1. the install reported success (the marker is read out of the transcript);
    2. no failure is about the install;
    3. a failure names the unit not reaching `active`.
    """
    result = nspawn_gate(root, broken_exec_usb, app="demo-app",
                         health_url=HEALTH, min_payload_kb=MIN_PAYLOAD_KB)
    assert not result.ok, (
        "the gate passed a bundle whose ExecStart names a module that does not "
        f"exist: it cannot detect a service that never starts\n{result.log}")

    assert "INSTALL_RC=0" in result.log, (
        f"the mutated bundle did not install cleanly, so this gate went red for "
        f"a reason the docker gate already covers and says nothing about "
        f"systemd starting the unit\n{result.log}")
    install_failures = [f for f in result.failures if "install.sh exited" in f]
    assert install_failures == [], install_failures

    came_up = [f for f in result.failures if "the service did not come up" in f]
    assert came_up, (
        f"the gate is red, but not because the unit failed to reach active -- "
        f"so this does not show it can detect the failure it exists for. "
        f"Failures were:\n" + "\n".join(result.failures))
    # And the health check fired too, on a marker that is absent rather than
    # wrong: the probe skips its live checks when MainPID is 0, because
    # `kill -9 0` signals the probe's own process group.
    assert "LIVE_CHECKS=skipped" in result.log, result.log

    # The two stability checks, asserted separately so they are guarded rather
    # than merely present. `ActiveState` alone cannot tell a service that came
    # up from one in a restart loop -- with no `Type=`, systemd calls the unit
    # `active (running)` the instant the fork succeeds, and this bundle's
    # process lives about 25 ms. Measured here: the wait loop DID break on that
    # transient, and if the reading after it had caught the same window the gate
    # would have passed this package. `SubState` and a restart count taken
    # before anything killed anything are what close that.
    assert [f for f in result.failures if "SubState is" in f], (
        "SubState was not checked: 'active' is true throughout an auto-restart "
        "cycle\n" + "\n".join(result.failures))
    assert [f for f in result.failures if "restart loop" in f], (
        "the unit was already flapping and the gate did not say so\n"
        + "\n".join(result.failures))


@pytest.mark.nspawn
def test_the_gate_detects_a_unit_that_runs_but_is_not_hardened_or_supervised(
        root, unhardened_usb):
    """The guard on every directive that only a running systemd can check.

    This bundle installs, comes up and answers. `systemd-analyze verify` is
    clean on its unit. The container e2e -- which runs `ExecStart=` itself, as
    root, from `WorkingDirectory=` -- is clean on it too, because none of what
    was removed is anything that gate looks at. Nothing in porter caught this
    until the unit was started by systemd.

    Each assertion below names one directive and is checked separately, so this
    stays evidence about *which* guard bit rather than "something went red".
    """
    result = nspawn_gate(root, unhardened_usb, app="demo-app",
                         health_url=HEALTH, min_payload_kb=MIN_PAYLOAD_KB)
    assert not result.ok, (
        "the gate passed a unit with User=, ProtectSystem= and Restart= "
        f"removed\n{result.log}")

    # It really did come up -- otherwise this is MUTATION 1 again and says
    # nothing about the hardening checks (trap 3).
    assert "ACTIVE=active" in result.log, (
        f"the unhardened unit did not start, so this test is measuring "
        f"liveness and not hardening\n{result.log}")
    assert "INSTALL_RC=0" in result.log, result.log

    def failing(fragment: str) -> list[str]:
        return [f for f in result.failures if fragment in f]

    assert failing("loaded User="), (
        "User=root was accepted\n" + "\n".join(result.failures))
    assert failing("the running process belongs to"), (
        "the process running as root was accepted: User= is checked only "
        "against the file\n" + "\n".join(result.failures))
    assert failing("loaded ProtectSystem="), (
        "ProtectSystem= was deleted from the unit and the gate did not notice "
        "-- which is exactly what systemd-analyze verify does\n"
        + "\n".join(result.failures))
    assert failing("loaded Restart="), (
        "Restart= was deleted and accepted\n" + "\n".join(result.failures))
    # And the demonstrated half, not just the declared one: with no Restart=,
    # systemd must NOT put the process back after the kill.
    assert failing("systemd did not replace the process"), (
        "the process was killed and the gate still reported a restart\n"
        + "\n".join(result.failures))
