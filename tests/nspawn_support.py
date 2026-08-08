"""Boot a real systemd and ask it what it did.

Everything else in this suite reads files. Two claims cannot be established that
way -- that an ordered pair of services *converges*, and that a `Type=oneshot`
job actually runs under the unit porter emitted -- so both boot a container.

Not a test module and not a conftest: a plain helper, imported by
`tests/test_ordering.py` and `tests/test_oneshot.py`, because the two need the
same 200 MB root and the same `sudo` discipline and neither owns the other.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# systemd-sysv is what provides /sbin/init; without it `nspawn --boot` has
# nothing to start. dbus is what makes systemctl inside the container able to
# talk to PID 1.
DOCKERFILE = """FROM debian:bookworm-slim
RUN apt-get update \\
 && apt-get install -y --no-install-recommends systemd systemd-sysv dbus \\
 && rm -rf /var/lib/apt/lists/*
"""


def require_nspawn() -> None:
    """Same bargain as PORTER_REQUIRE_UV/DOCKER/SYSTEMD, for the tests that run
    systemd rather than reading its files.

    Deliberately **not** in .github/workflows/ci.yml's env block, unlike the
    other three, and that is a hole stated rather than hidden: whether a
    GitHub-hosted runner can boot systemd-nspawn has not been measured here, and
    arming a gate that cannot be satisfied turns every CI run red for a reason
    unrelated to the change under test. Armed locally, it is what makes a green
    run evidence.
    """
    missing = None
    if not shutil.which("systemd-nspawn"):
        missing = "systemd-nspawn is not on PATH"
    elif os.geteuid() != 0 and subprocess.run(
            ["sudo", "-n", "true"], capture_output=True).returncode != 0:
        missing = "not root and `sudo -n` does not work"
    elif not (shutil.which("docker") and subprocess.run(
            ["docker", "info"], capture_output=True).returncode == 0):
        missing = "no usable docker daemon to build the container root from"
    if missing is None:
        return
    if os.environ.get("PORTER_REQUIRE_NSPAWN", "") not in ("", "0"):
        pytest.fail(f"PORTER_REQUIRE_NSPAWN is set and {missing}: this run would "
                    "have skipped a test that runs systemd.", pytrace=False)
    pytest.skip(missing)


def sudo(*args: str, **kw) -> subprocess.CompletedProcess:
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return subprocess.run([*prefix, *args], **kw)


def build_root(parent: Path) -> Path:
    """A bootable Debian tree, built by exporting a docker image.

    debootstrap is not installed here and docker already is (the suite's
    container tests need it), and `docker export` of an image that has systemd
    in it is the same filesystem debootstrap would have produced. The image is a
    build-time convenience only -- nothing under test runs in docker, and the
    subject is systemd's own behaviour inside nspawn.

    Caller frees it with `sudo("rm", "-rf", root)`: it is extracted as root, so
    pytest's own tmp_path cleanup cannot delete it and 443 MB per run
    accumulates three deep in /tmp/pytest-of-*. Measured here.
    """
    require_nspawn()
    build = subprocess.run(["docker", "build", "-q", "-t",
                            "porter-test-nspawn:bookworm", "-"],
                           input=DOCKERFILE, capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    created = subprocess.run(["docker", "create", "porter-test-nspawn:bookworm"],
                             capture_output=True, text=True)
    assert created.returncode == 0, created.stderr
    cid = created.stdout.strip()
    root = parent / "root"
    root.mkdir()
    try:
        export = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE)
        extract = sudo("tar", "-x", "-C", str(root), stdin=export.stdout)
        export.stdout.close()
        assert export.wait() == 0 and extract.returncode == 0
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    # The positive control on the root itself: without an init, nspawn --boot
    # exits immediately and every assertion downstream would be about a
    # container that never ran.
    assert (root / "sbin/init").exists() or (root / "usr/sbin/init").exists(), (
        f"{root} has no init: systemd-nspawn --boot has nothing to start")
    return root


def boot_with_probe(root: Path, probe_sh: str, out: Path, debs: Path,
                    timeout: int = 300) -> subprocess.CompletedProcess:
    """Boot `root`, run `probe_sh` at multi-user.target, come back.

    The probe is a `Type=oneshot` unit that writes into the bind-mounted `out`
    and ends with `systemctl poweroff`, which is what returns control here. Its
    own output goes to /out/probe.log first thing, so a failure anywhere inside
    is readable from the host even when nothing else was written.

    `--private-network` is not optional: without it the container shares the
    host's network namespace, so a service binding 127.0.0.1:9101 binds the
    HOST's loopback -- colliding with whatever is already listening and with any
    concurrent run. There is nothing to fetch; porter's whole premise is that a
    client needs no network.
    """
    out.mkdir(parents=True, exist_ok=True)
    sudo("chmod", "0777", str(out))
    sudo("cp", "/dev/stdin", str(root / "probe.sh"), input=probe_sh, text=True)
    sudo("chmod", "0755", str(root / "probe.sh"))
    sudo("cp", "/dev/stdin", str(root / "etc/systemd/system/porter-probe.service"),
         input="[Unit]\nDescription=porter probe\nAfter=multi-user.target\n\n"
               "[Service]\nType=oneshot\nExecStart=/probe.sh\n\n"
               "[Install]\nWantedBy=multi-user.target\n", text=True)
    enable = sudo("systemctl", f"--root={root}", "enable", "porter-probe.service",
                  capture_output=True, text=True)
    assert enable.returncode == 0, enable.stderr

    # An explicit, unique --machine=. Without it nspawn names the machine after
    # the directory -- every root here is called `root` -- and registers a
    # `root.scope` with the HOST's systemd. A second boot then dies with
    # "Failed to allocate scope: Unit root.scope was already loaded", which is
    # what happens the moment a second nspawn test exists, or when one earlier
    # container was killed rather than powered off and its scope outlived it.
    # Measured here: a leaked `root` machine from an interrupted run blocked
    # every subsequent boot on the host until it was terminated.
    machine = f"porter-{os.getpid()}-{abs(hash(str(root))) % 100000}"
    booted = sudo("systemd-nspawn", "-q", f"--directory={root}",
                  f"--machine={machine}",
                  f"--bind-ro={debs}:/debs", f"--bind={out}:/out",
                  "--private-network", "--boot",
                  capture_output=True, text=True, timeout=timeout)
    # Powering off from inside is the normal exit; this is for the paths where
    # it did not happen (a probe that failed before `systemctl poweroff`, a
    # timeout), so a scope is never left behind to block the next boot.
    sudo("machinectl", "terminate", machine, capture_output=True)
    # Written by root inside the container; handed back so pytest can clean up
    # after itself. Before any assertion, deliberately -- a failing test must not
    # be what decides whether /tmp fills up.
    sudo("chown", "-R", f"{os.getuid()}:{os.getgid()}", str(out))
    return booted


def context(booted: subprocess.CompletedProcess, out: Path) -> str:
    """Everything a failure needs, in one string: the container's console, the
    probe's own trace, and whatever the probe managed to write."""
    files = "\n".join(
        f"--- {p.name} ---\n{p.read_text()[:2000]}"
        for p in sorted(out.iterdir()) if p.is_file() and p.name != "probe.log")
    log = (out / "probe.log").read_text()[-3000:] if (out / "probe.log").exists() \
        else "(no probe log -- the probe unit did not run)"
    return (f"nspawn rc={booted.returncode}\n{booted.stdout[-1500:]}\n"
            f"{booted.stderr[-1500:]}\n{files}\n--- probe.log ---\n{log}")
