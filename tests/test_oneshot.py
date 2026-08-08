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
    with pytest.raises(ValueError, match="never fires"):
        assemble(_job(schedule=None, module="hello", source_paths=["src/hello.py"]),
                 PY, src_tree, tmp_path / "stage")


def test_a_service_declaring_a_schedule_is_refused(tmp_path, src_tree):
    """Only the oneshot branch writes a `.timer`, so a service that declares a
    schedule gets a package that runs continuously and never on the calendar its
    author asked for -- at rc=0, with the manifest saying otherwise."""
    with pytest.raises(ValueError, match="oneshot"):
        assemble(_job(kind="service", module="hello",
                      source_paths=["src/hello.py"]),
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
