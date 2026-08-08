"""`kind: oneshot` -- a scheduled job, which is a unit shape and not a service.

Task 4 refused this kind by name. The refusal was right: `unit()` emitted no
`Type=`, nothing emitted a `.timer`, and the postinst enabled `<pkg>.service`
unconditionally, so a job pushed through them installed at rc=0 and ran as a
permanently-restarting service. Every one of those three is silent -- the
package installs, dpkg is happy, and the manifest's `schedule:` is simply not
in the artefact.

So the tests here are about what is in the artefact. The central one runs
`systemctl enable` against the tree extracted from the built .deb, using the
unit name read out of the package's own postinst, and asserts on the symlinks
that appear: `timers.target.wants/<pkg>.timer` and **no**
`multi-user.target.wants/<pkg>.service`. That is the difference between a
nightly job and a process that runs at boot forever, and it is the one thing
that cannot be established by reading a unit file on its own.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
import nspawn_support as nspawn
from conftest import _require_uv

from porter.assemble import assemble
from porter.systemd import timer, unit
from porter.types import Component, Python

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "oneshot-timer"
SERVICE_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "service-fastapi"
PY = Python(version="3.12")


def _sections(body: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = None
    for line in body.splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            out[current] = []
        elif line and current is not None:
            out[current].append(line)
    return out


def _job(**over) -> Component:
    base = dict(name="job", package="demo-job", description="demo job",
                kind="oneshot", module="job", schedule="*-*-* 03:00:00")
    base.update(over)
    return Component(**base)


# --- the two units -----------------------------------------------------------

def test_a_oneshot_unit_is_typed_and_is_not_restarted():
    """`Type=oneshot`, and no `Restart=`.

    Both halves matter and they fail differently. Without `Type=oneshot`,
    systemd treats the fork as success: the job's exit code is nobody's
    business and a failed nightly run reports nothing. Without the *absence* of
    `Restart=`, a job that fails at 03:00 is retried every three seconds until
    tomorrow -- the timer is already the retry, and the pair together is a
    tight loop against whatever made the job fail.
    """
    body = unit("demo-job", "demo job", "/bin/true", "/tmp", kind="oneshot")
    service = _sections(body)["Service"]
    assert "Type=oneshot" in service, body
    assert not [line for line in service if line.startswith("Restart")], body
    # The hardening is not conditional on the kind. A job runs the same payload
    # a service does, on the same client.
    for directive in ("ProtectSystem=strict", "PrivateTmp=yes",
                      "NoNewPrivileges=yes", "StateDirectory=demo-job"):
        assert directive in service, body


def test_a_oneshot_installs_its_timer_and_never_itself():
    """`[Install] Also=<pkg>.timer` with no `WantedBy=`.

    This is what makes the postinst's existing `systemctl enable <pkg>.service`
    correct for a job without the postinst knowing anything about timers. A
    `WantedBy=multi-user.target` here is precisely the bug Task 4 refused the
    kind over: the job would be pulled in at boot, every boot, in addition to
    its schedule.
    """
    body = unit("demo-job", "demo job", "/bin/true", "/tmp", kind="oneshot")
    install = _sections(body)["Install"]
    assert install == ["Also=demo-job.timer"], body


def test_a_service_is_unchanged_by_the_oneshot_branch():
    """The control for both tests above: the default kind still emits exactly
    what it emitted before oneshot existed. A shared emitter is how a job's
    `Type=oneshot` or its missing `Restart=` reaches every service in the
    gallery, and neither would fail any test written about jobs."""
    body = unit("demo-app", "demo", "/bin/true", "/tmp")
    service = _sections(body)["Service"]
    assert "Type=oneshot" not in service, body
    assert "Restart=on-failure" in service and "RestartSec=3" in service, body
    assert _sections(body)["Install"] == ["WantedBy=multi-user.target"], body


def test_the_timer_carries_the_schedule_and_survives_a_powered_off_night():
    """`Persistent=true` is not decoration on an appliance.

    Without it a client that was switched off at 03:00 misses the run entirely
    and reports nothing; a crawler that skipped a day is indistinguishable from
    one that found nothing. With it, the run happens once at the next boot.
    """
    body = timer("demo-job", "demo job", "*-*-* 03:00:00")
    assert "OnCalendar=*-*-* 03:00:00" in _sections(body)["Timer"], body
    assert "Persistent=true" in _sections(body)["Timer"], body
    assert _sections(body)["Install"] == ["WantedBy=timers.target"], body


def test_a_timer_with_no_schedule_is_refused():
    """A `[Timer]` with no `OnCalendar=` loads, enables, and never fires. There
    is no interval porter could pick that is not a guess about someone's job."""
    with pytest.raises(ValueError, match="never"):
        timer("demo-job", "demo job", "")


def test_a_schedule_systemd_cannot_parse_is_refused(require_systemd_analyze):
    """Measured 2026-08-08: `OnCalendar=every tuesday-ish` in a shipped timer is
    accepted by `systemctl enable`, which exits 0 and creates the
    timers.target.wants symlink. systemd rejects the expression only when it
    loads the unit -- in the journal, on the client, months later, with the job
    never having run.

    The positive control is the good expression on the line below: a check that
    refused everything would satisfy the assertion above just as well.
    """
    with pytest.raises(ValueError, match="cannot parse"):
        timer("demo-job", "demo job", "every tuesday-ish")
    assert "OnCalendar=daily" in timer("demo-job", "demo job", "daily")


def _verify(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body)
    proc = subprocess.run(["systemd-analyze", "verify", str(path)],
                          capture_output=True, text=True, cwd=tmp_path)
    return proc.stdout + proc.stderr


def test_both_emitted_units_carry_no_directive_systemd_would_ignore(
        tmp_path, require_systemd_analyze):
    """With the control aimed at this file's own subject: prove the probe sees a
    misspelled `OnCalendar` before trusting it to report a clean timer."""
    (tmp_path / "demo-job.service").write_text(
        unit("demo-job", "demo job", "/bin/true", "/tmp", kind="oneshot"))
    good = timer("demo-job", "demo job", "*-*-* 03:00:00")
    assert "Unknown key" not in _verify(tmp_path, "demo-job.timer", good)

    bad = good.replace("OnCalendar=", "OnCalender=")
    out = _verify(tmp_path, "demo-job.timer", bad)
    assert "Unknown key" in out and "OnCalender" in out, (
        f"systemd-analyze did not report a timer key it does not know: this "
        f"probe cannot detect the failure it exists to detect. Got: {out!r}")


# --- what assemble stages ----------------------------------------------------

def test_assemble_stages_both_units_for_a_oneshot(tmp_path, src_tree):
    job = _job(module="hello", source_paths=["src/hello.py"])
    staged = assemble(job, PY, src_tree, tmp_path / "stage")
    units = staged.stage / "usr/lib/systemd/system"
    assert (units / "demo-job.service").exists()
    assert (units / "demo-job.timer").exists(), (
        "the schedule was read from the manifest and dropped: the package "
        "installs at rc=0 with nothing to fire the job")
    assert "OnCalendar=*-*-* 03:00:00" in (units / "demo-job.timer").read_text()


def test_a_oneshot_with_no_schedule_is_refused_before_anything_is_staged(
        tmp_path, src_tree):
    """And the second half of that name is now checked, and is now true.

    It was neither. The refusal lived only in `timer()`, which `assemble`
    reaches *after* `vendor()` has materialised ~97 MB and after
    `<pkg>.service` has been written -- so the stage did exist, and the test
    asserting it did not was asserting nothing. The refusal is in
    `_refuse_what_porter_cannot_emit` now, beside its sibling, and `timer()`
    keeps its own for callers that reach it directly.
    """
    stage = tmp_path / "stage"
    with pytest.raises(ValueError, match="never fires"):
        assemble(_job(schedule=None, module="hello", source_paths=["src/hello.py"]),
                 PY, src_tree, stage)
    assert not stage.exists(), (
        f"the refusal fired after staging began; {stage} holds "
        f"{sorted(p.name for p in stage.iterdir())}")


@pytest.mark.parametrize("kind,extra", [("service", {}),
                                        ("command", {"bin_name": "demo"})])
def test_a_kind_that_emits_no_timer_may_not_declare_a_schedule(
        tmp_path, src_tree, kind, extra):
    """Only the oneshot branch writes a `.timer`.

    A `service` that declares a schedule gets a package that runs continuously
    and never on the calendar its author asked for. A `command` gets less than
    that: it emits no unit at all, so the schedule is read, dropped, and nothing
    anywhere is different -- at rc=0, with the manifest saying otherwise. The
    second case was accepted until the Task 11 review found it, while its
    sibling had been refused since the kind existed.
    """
    with pytest.raises(ValueError, match="oneshot"):
        assemble(_job(kind=kind, module="hello",
                      source_paths=["src/hello.py"], **extra),
                 PY, src_tree, tmp_path / "stage")


# --- the gallery entry, built and enabled ------------------------------------

def _build(work: Path, manifest: Path, out: str = "dist") -> Path:
    proc = subprocess.run(
        ["uv", "run", "porter", "build", str(manifest), "--out", out,
         "--stage", "build"],
        cwd=work, capture_output=True, text=True,
        env={**os.environ, "UV_PROJECT": str(Path(__file__).resolve().parents[1])})
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return work / out


def _extract(deb: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["dpkg-deb", "-x", str(deb), str(dest)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return dest


def _postinst(deb: Path) -> str:
    proc = subprocess.run(["dpkg-deb", "--info", str(deb), "postinst"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _enable_as_the_postinst_would(deb: Path, tree: Path) -> str:
    """Run the package's own `systemctl enable` against its own extracted tree.

    The unit name is **read out of the postinst in the .deb**, not written here.
    That is what couples this test to the shipped behaviour: if the postinst
    ever enables something else, this test follows it and the assertions below
    describe the new reality instead of a hardcoded hope.

    `systemctl --root=` performs a real enable offline -- it reads `[Install]`,
    follows `Also=`, and writes the symlinks -- so a booted host and root are
    not needed to establish which units a client would end up with.
    """
    # Comment lines stripped first: the postinst explains the shape it replaced
    # ("the previous shape was `systemctl enable ... || true`"), and a naive
    # findall reads that sentence as an instruction.
    script = "\n".join(line for line in _postinst(deb).splitlines()
                       if not line.lstrip().startswith("#"))
    enabled = re.findall(r"systemctl enable (\S+)", script)
    assert enabled == [f"{deb.name.split('_')[0]}.service"], (
        f"the postinst enables {enabled}, which this test was not written "
        "against; the coupling between the postinst and [Install] has changed")
    proc = subprocess.run(["systemctl", f"--root={tree}", "enable", *enabled],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stderr


@pytest.fixture(scope="module")
def built_job(tmp_path_factory) -> Path:
    _require_uv()
    work = tmp_path_factory.mktemp("oneshot")
    dist = _build(work, EXAMPLE / "porter.yaml")
    debs = list(dist.glob("*.deb"))
    assert len(debs) == 1, debs
    assert debs[0].stat().st_size > 20 * 1024 * 1024, (
        f"{debs[0]} is {debs[0].stat().st_size} bytes -- too small to hold a "
        "vendored interpreter, and every assertion below would still pass")
    return debs[0]


def test_the_oneshot_example_ships_both_units(built_job, tmp_path):
    tree = _extract(built_job, tmp_path / "tree")
    units = tree / "usr/lib/systemd/system"
    assert (units / "demo-job.service").exists()
    assert (units / "demo-job.timer").exists()
    assert "OnCalendar=*-*-* 03:00:00" in (units / "demo-job.timer").read_text()


def test_installing_the_job_enables_its_timer_and_not_a_boot_time_service(
        built_job, tmp_path, require_systemd_analyze):
    """The artefact assertion this whole kind turns on.

    Enable the package the way its own postinst does, against the tree the
    client would have, and look at what systemd linked:

    - `timers.target.wants/demo-job.timer` must exist, or the job never runs;
    - `multi-user.target.wants/demo-job.service` must NOT, or the job runs at
      boot -- which, before `Type=oneshot`, was a permanently-restarting
      service, and is the reason Task 4 refused the kind rather than shipping
      it.
    """
    tree = _extract(built_job, tmp_path / "tree")
    _enable_as_the_postinst_would(built_job, tree)

    wants = tree / "etc/systemd/system"
    assert (wants / "timers.target.wants/demo-job.timer").is_symlink(), (
        f"the timer was not enabled; installed tree holds: "
        f"{sorted(p.relative_to(tree) for p in wants.rglob('*'))}")
    assert not (wants / "multi-user.target.wants/demo-job.service").exists(), (
        "the job's service was enabled into multi-user.target: it will run at "
        "boot as well as on its schedule")


def test_the_same_procedure_enables_a_service_at_boot(tmp_path, require_uv,
                                                      require_systemd_analyze):
    """The positive control for the test above, and it is not optional.

    That test asserts an absence -- no `multi-user.target.wants` link -- and an
    absence is what a probe that does nothing at all also reports. So run the
    identical procedure against the gallery's *service* entry: there the boot
    link must appear and the timer link must not. A `systemctl --root` that
    silently failed, or an extract that produced an empty tree, fails here.
    """
    work = tmp_path / "svc"
    work.mkdir()
    deb = next(_build(work, SERVICE_EXAMPLE / "porter.yaml").glob("*.deb"))
    tree = _extract(deb, tmp_path / "svc-tree")
    _enable_as_the_postinst_would(deb, tree)

    wants = tree / "etc/systemd/system"
    assert (wants / "multi-user.target.wants/demo-app.service").is_symlink(), (
        f"a plain service did not get its boot link, so the oneshot test's "
        f"assertion of its absence proves nothing; tree holds: "
        f"{sorted(p.relative_to(tree) for p in wants.rglob('*')) if wants.exists() else '(no etc/systemd/system at all)'}")
    assert not (wants / "timers.target.wants").exists(), (
        "a service acquired a timer link")


# --- the job, actually run by systemd ----------------------------------------

# Installs the job and asks systemd two things it cannot be asked offline: is
# the timer armed (does systemd have a next elapse for it?), and does the job
# run under the unit porter emitted. `systemctl start <pkg>.service` on a
# Type=oneshot BLOCKS until the process exits, so its rc is the job's own -- on
# Type=simple it would return the moment the fork succeeded and tell us nothing.
#
# NOTHING HERE STARTS THE TIMER. The first version of this probe ran
# `systemctl start demo-job.timer` between the install and the questions, which
# made every assertion below about a timer the TEST had armed -- so the probe
# was structurally unable to notice that `dpkg -i` alone armed nothing, which
# for four commits it did not (`enable` links a unit; `try-restart` is a no-op
# on one that is not running). The line is gone, and `is-active` below is what
# it bought. See porter/config.py's fresh-install block.
JOB_PROBE_SH = '''\
#!/bin/sh
exec >/out/probe.log 2>&1
set -x
dpkg -i /debs/*.deb; echo "DPKG_RC=$?" > /out/dpkg-rc
systemctl is-enabled demo-job.timer > /out/timer-enabled 2>&1
systemctl is-enabled demo-job.service > /out/service-enabled 2>&1
# After the install and NOTHING ELSE. `enabled` means the symlink exists and
# says nothing about whether systemd is counting down; `active` on a .timer is
# what "armed" means.
systemctl is-active demo-job.timer > /out/timer-active 2>&1
# The next time systemd intends to fire the job. Empty means the calendar was
# never understood -- which is exactly what a shipped typo looks like -- and, on
# a timer nobody started, it is also what an unarmed timer looks like.
systemctl show -p NextElapseUSecRealtime --value demo-job.timer > /out/next-elapse
systemctl start demo-job.service; echo "RUN_RC=$?" > /out/run-rc
systemctl show -p Result --value demo-job.service > /out/result
cat /var/lib/demo-job/report.txt > /out/report 2>&1
ls -ld /var/lib/demo-job > /out/state-dir
journalctl --no-pager -u demo-job > /out/journal 2>&1
systemctl poweroff
'''


@pytest.fixture(scope="module")
def nspawn_root(tmp_path_factory) -> Path:
    root = nspawn.build_root(tmp_path_factory.mktemp("nspawn"))
    yield root
    nspawn.sudo("rm", "-rf", str(root))


@pytest.mark.nspawn
def test_the_job_runs_under_systemd_and_writes_to_its_state_directory(
        nspawn_root, built_job, tmp_path):
    """The end of the path, and the part no file can establish.

    Everything else in this file proves the package *contains* the right units
    and that enabling it links the right symlinks. None of that runs the job.
    Here systemd installs the package, arms the timer, executes the service, and
    the payload writes into the `StateDirectory=` systemd created for it -- the
    four things that have to work together for `kind: oneshot` to mean anything,
    and the last two of which involve `ProtectSystem=strict` and a static system
    user that only a real boot exercises.

    **And the arming is the package's, not the test's.** This probe used to run
    `systemctl start demo-job.timer` by hand, so `is-enabled` and
    `NextElapseUSecRealtime` were both being read off a timer it had started
    itself: the test could not fail on a package that installed a job and left
    it inert until the next reboot, which is exactly what porter shipped. The
    probe starts nothing now. `dpkg -i` is the only thing that happens between
    the boot and the questions.
    """
    out = tmp_path / "out"
    booted = nspawn.boot_with_probe(nspawn_root, JOB_PROBE_SH, out,
                                    built_job.parent)
    context = nspawn.context(booted, out)

    # Existence before content: a probe that never ran leaves no files at all,
    # and a FileNotFoundError three frames deep hides the container's console.
    assert (out / "dpkg-rc").exists(), context
    assert (out / "dpkg-rc").read_text().strip() == "DPKG_RC=0", context
    # The timer is enabled and the service is not -- the same claim
    # `test_installing_the_job_enables_its_timer...` makes offline, confirmed
    # here by the systemd that actually did the enabling during postinst.
    assert (out / "timer-enabled").read_text().strip() == "enabled", context
    assert (out / "service-enabled").read_text().strip() == "indirect", context
    # THE fresh-install claim, and the only test in porter that makes it: after
    # `dpkg -i` and nothing else, is the unit running? `enabled` above is a
    # symlink in timers.target.wants and is true of a timer that will not fire
    # until the next boot -- which is what a client installing a nightly job at
    # 10:00 got, at rc=0, with dpkg reporting `install ok installed`.
    assert (out / "timer-active").read_text().strip() == "active", (
        f"the timer is enabled but not running after dpkg -i: the job does not "
        f"exist until this machine is rebooted\n{context}")
    # A next elapse means systemd parsed `OnCalendar=*-*-* 03:00:00` and intends
    # to act on it. A timer with an expression it could not read is still
    # "enabled" and has none -- and so is one nobody started.
    assert (out / "next-elapse").read_text().strip() not in ("", "0", "n/a"), (
        f"the timer is enabled but systemd has no next elapse for it: the "
        f"calendar expression did not survive into a schedule, or nothing armed "
        f"the timer\n{context}")

    # The job ran to completion. `Result=success` is systemd's own verdict on a
    # Type=oneshot, which it can only have because it waited for the process.
    assert (out / "run-rc").read_text().strip() == "RUN_RC=0", context
    assert (out / "result").read_text().strip() == "success", context
    # And it wrote where it was supposed to: /var/lib/demo-job, created by
    # StateDirectory=, under ProtectSystem=strict where nothing else is
    # writable. The payload reads $STATE_DIRECTORY to find it.
    report = (out / "report").read_text()
    assert "ran, keeping 14 days" in report, (
        f"the job did not write its report, or wrote it somewhere else\n{context}")
    # RETENTION_DAYS=14 came out of /etc/demo-job/defaults through the unit's
    # EnvironmentFile, so this line also proves the config split reaches the
    # running process and not merely the disk.
    assert "demo-job" in (out / "state-dir").read_text(), context


def test_the_shipped_postinst_arms_the_timer_and_not_the_job(built_job):
    """Read out of the built .deb, because that is what a client runs.

    Two units are in play and the postinst has to name the right one: it
    ENABLES `demo-job.service` (whose `[Install] Also=` carries the timer) and
    STARTS `demo-job.timer`. Starting the service instead would run the job at
    install time -- once, at whatever hour the operator happened to be there --
    and still leave nothing counting down to 03:00.

    This is the non-nspawn half of the claim `test_the_job_runs_under_systemd_`
    makes on a booted container, and it exists so that a CI run without
    PORTER_REQUIRE_NSPAWN still fails if `assemble` stops telling the postinst
    what kind of component it is staging.
    """
    script = "\n".join(line for line in _postinst(built_job).splitlines()
                       if not line.lstrip().startswith("#"))
    assert re.findall(r"systemctl start (?:--no-block )?(\S+)", script) == [
        "demo-job.timer"], script
    assert re.findall(r"systemctl enable (\S+)", script) == ["demo-job.service"], script


def test_the_oneshot_examples_config_split_is_intact(built_job, tmp_path):
    """A job gets the same two-file split a service does. The admin's half is
    never shipped -- deb.py's lint refuses `/etc/<pkg>/env` in the stage -- and
    the package-owned half is a conffile."""
    tree = _extract(built_job, tmp_path / "tree")
    assert (tree / "etc/demo-job/defaults").exists()
    assert not (tree / "etc/demo-job/env").exists()
    assert (tree / "usr/share/demo-job/env.example").exists()
    manifest = yaml.safe_load((EXAMPLE / "porter.yaml").read_text())
    defaults = (tree / "etc/demo-job/defaults").read_text()
    assert "RETENTION_DAYS=14" in defaults
    for key in manifest["admin_keys"]:
        assert f"{key}=" not in defaults, (
            f"{key} is the admin's and is being shipped in the package-owned "
            "half, where an upgrade overwrites it")
