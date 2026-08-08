"""The end-to-end moment: a package that installs and answers HTTP, offline."""
import subprocess
import pytest

pytestmark = pytest.mark.docker

# A systemctl that succeeds at everything except `enable`, and the marker
# directory a maintainer script reads to decide whether it is on a booted
# systemd host. Together they stage the one failure a client cannot see: systemd
# is real, `enable` fails, and after the next reboot the service is simply not
# there. Written to /usr/sbin so it is on dpkg's maintainer-script PATH
# (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin).
FAKE_FAILING_SYSTEMCTL = (
    "printf '#!/bin/sh\\n[ \"$1\" = enable ] && exit 1\\nexit 0\\n' "
    "> /usr/sbin/systemctl; chmod +x /usr/sbin/systemctl; "
)


def _run(image: str, script: str, mounts: dict[str, str]) -> subprocess.CompletedProcess:
    """One container, no network, the .deb directories mounted read-only.

    `--network none` is the airgap: nothing here may reach an index, a wheel or
    a distro mirror. Loopback still exists inside the namespace, which is what
    lets the service be probed at all. rc is returned, never piped -- the
    caller reads it directly.
    """
    argv = ["docker", "run", "--rm", "--network", "none"]
    for host, guest in mounts.items():
        argv += ["-v", f"{host}:{guest}:ro"]
    argv += [image, "bash", "-c", script]
    return subprocess.run(argv, capture_output=True, text=True)


def test_installed_service_answers_and_admin_env_overrides(built_demo_deb, docker_image):
    """built_demo_deb is a .deb carrying the vendored interpreter, a FastAPI
    app, the split config and a systemd unit.

    Containers have no PID 1 systemd, so the unit is not *started* here -- but
    what is run is read out of the INSTALLED unit file rather than written by
    hand: `ExecStart=` verbatim, from the directory `WorkingDirectory=` names.
    An earlier version of this test ran a hand-copied command line with an extra
    `--app-dir /usr/lib/demo-app`, which quietly supplied the very thing
    `WorkingDirectory=` is there to supply -- so the single directive most
    likely to be wrong was the one directive the test could not catch.

    The first half is the positive control, per the gate rule: the same
    ExecStart from `/` must NOT answer. Without it "the service replied" would
    not distinguish "WorkingDirectory put app.py on sys.path" from "it would
    have worked from anywhere".

    Still NOT exercised here, and only by Task 4's nspawn gate: systemd itself
    running this line -- User=/Group=, StateDirectory=, ProtectSystem=strict,
    Restart=on-failure, and systemd's root-then-drop read of EnvironmentFile.
    """
    unit_path = "/usr/lib/systemd/system/demo-app.service"
    script = (
        "set -e; dpkg -i /debs/*.deb >/dev/null; "
        "echo 'GREETING=from-admin' > /etc/demo-app/env; "
        "set -a; . /etc/demo-app/defaults; . /etc/demo-app/env; set +a; "
        # The artefact is the source of the command line. sed, not a copy.
        f"WD=$(sed -n 's/^WorkingDirectory=//p' {unit_path}); "
        f"ES=$(sed -n 's/^ExecStart=//p' {unit_path}); "
        'test -n "$WD"; test -n "$ES"; '
        'echo "WD=$WD"; echo "ES=$ES"; '
        # Control: the same ExecStart, one directory too high.
        '( cd / && eval "$ES" ) >/dev/null 2>&1 & CTL=$!; '
        "sleep 4; set +e; "
        'curl -fsS http://127.0.0.1:${PORT}/health >/dev/null 2>&1; echo "CONTROL_RC=$?"; '
        "kill $CTL 2>/dev/null; set -e; sleep 1; "
        # The real thing: the unit's own ExecStart, in the unit's own directory.
        'cd "$WD"; eval "$ES" & '
        "sleep 4; curl -fsS http://127.0.0.1:${PORT}/health"
    )
    proc = _run(docker_image, script, {str(built_demo_deb.parent): "/debs"})
    out = proc.stdout
    assert "WD=/usr/lib/demo-app" in out, f"the unit declared no WorkingDirectory:\n{out}"
    assert "--app-dir" not in out, (
        "the shipped ExecStart carries its own path hint, so WorkingDirectory= is "
        f"not what puts app.py on sys.path and this test proves nothing:\n{out}")
    assert "CONTROL_RC=0" not in out, (
        "the same ExecStart answered from / as well: WorkingDirectory= is not "
        f"decisive here, so a wrong one would not be caught:\n{out}{proc.stderr}")
    assert proc.returncode == 0, proc.stderr
    assert '"greeting":"from-admin"' in out, out
    assert '"tuning":"from-defaults"' in out, out


def test_upgrade_keeps_the_admin_file_and_delivers_a_new_package_owned_key(
        built_demo_deb, built_demo_deb_v2, docker_image):
    """The row the design table calls "split (chosen)", executed.

    v1 is installed and configured the way a real client configures it -- a
    value written into /etc/demo-app/env. v2 then changes TUNING and adds
    DB_TDS_VERSION. Both halves must hold at once, and no single-file scheme
    manages both: the admin's edit survives, AND the new package-owned key
    lands, AND dpkg never asks a question (stdin is closed -- docker run
    without -i -- so a prompt is a failure, not a hang).

    The mode/owner assertion is here rather than in a unit test because
    `chmod 600` in the postinst text proves nothing about the file dpkg leaves
    on disk.
    """
    script = (
        "set -e; "
        "dpkg -i /v1/*.deb >/dev/null; "
        "stat -c 'ENVFILE=%a:%U:%G' /etc/demo-app/env; "
        "echo 'GREETING=from-admin' > /etc/demo-app/env; "
        "DEBIAN_FRONTEND=noninteractive dpkg -i /v2/*.deb >/dev/null; "
        "echo '--- env'; cat /etc/demo-app/env; "
        "echo '--- defaults'; cat /etc/demo-app/defaults; "
        "echo '--- version'; dpkg-query -W -f '${Version}\\n' demo-app"
    )
    proc = _run(docker_image, script,
                {str(built_demo_deb.parent): "/v1", str(built_demo_deb_v2.parent): "/v2"})
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # postinst created it root-owned and unreadable to anyone else, and systemd
    # still hands it to an unprivileged service (it reads EnvironmentFile as
    # root, before dropping privileges -- verified on demos 2026-08-07).
    assert "ENVFILE=600:root:root" in out, out
    assert "GREETING=from-admin" in out, out          # the admin's edit survived
    assert "TUNING=from-defaults-v2" in out, out      # a corrected value reached the client
    assert "DB_TDS_VERSION=7.0" in out, out           # a NEW key reached a configured client
    assert "1.1" in out.split("--- version")[1], out  # and it really was the upgrade


def test_a_locally_edited_conffile_would_have_failed_that_upgrade(
        built_demo_deb, built_demo_deb_v2, docker_image):
    """The positive control for the test above.

    Everything in this design rests on a measurement: a conffile the admin has
    edited fails an unattended upgrade outright. If that were not true, the
    split would be ceremony and the previous test's rc=0 would prove nothing --
    a single file would have passed too. So: edit the conffile, exactly as an
    admin would if the two owners shared one file, and watch dpkg refuse.

    DEBIAN_FRONTEND=noninteractive is set deliberately, to show it does not
    help: it governs debconf, not dpkg's own conffile prompt.
    """
    script = (
        "set -e; dpkg -i /v1/*.deb >/dev/null; "
        "sed -i 's/^TUNING=.*/TUNING=edited-by-the-admin/' /etc/demo-app/defaults; "
        "set +e; DEBIAN_FRONTEND=noninteractive dpkg -i /v2/*.deb; echo \"RC=$?\"; "
        "echo '--- defaults'; cat /etc/demo-app/defaults"
    )
    proc = _run(docker_image, script,
                {str(built_demo_deb.parent): "/v1", str(built_demo_deb_v2.parent): "/v2"})
    out = proc.stdout + proc.stderr
    assert "RC=0" not in out, f"an edited conffile upgraded cleanly; the control is broken:\n{out}"
    assert "conffile" in out, out


def test_a_failing_systemctl_fails_the_install_on_a_booted_host_and_not_in_a_chroot(
        built_demo_deb, docker_image):
    """`systemctl enable` must never fail quietly on a client that has systemd.

    The postinst's three `systemctl` calls used to end in `|| true`, which is
    right for the container and chroot case -- there is no systemd there and the
    noise is expected -- and wrong everywhere else. On a real client a failed
    `enable` means the unit is not wired to any target: the install reports
    success, the service runs until the next reboot, and then it is gone. That
    is a silent success on the one path an unattended client depends on, and
    nobody notices for weeks.

    So the two cases are separated, and BOTH halves are asserted here against
    the same failing `systemctl` -- the only difference between the containers
    is `/run/systemd/system`. That is what makes this a control rather than two
    unrelated observations: if the guard stopped discriminating, one half or the
    other goes red, and a `|| true` restored anywhere in the block turns the
    booted half green at rc=0, which is the original symptom exactly.
    """
    booted = _run(docker_image,
                  "set -e; mkdir -p /run/systemd/system; " + FAKE_FAILING_SYSTEMCTL +
                  "set +e; dpkg -i /debs/*.deb >/dev/null 2>&1; echo \"RC=$?\"; "
                  "dpkg-query -W -f 'STATUS=${Status}\\n' demo-app",
                  {str(built_demo_deb.parent): "/debs"})
    out = booted.stdout + booted.stderr
    assert "RC=0" not in out, (
        "systemd is present, `systemctl enable` failed, and dpkg still reported "
        f"success -- the service would not come back after a reboot:\n{out}")
    assert "STATUS=install ok half-configured" in out, (
        f"expected dpkg to leave the package unconfigured after the failure:\n{out}")

    chroot = _run(docker_image,
                  "set -e; test ! -d /run/systemd/system; " + FAKE_FAILING_SYSTEMCTL +
                  "set +e; dpkg -i /debs/*.deb >/dev/null 2>&1; echo \"RC=$?\"; "
                  "dpkg-query -W -f 'STATUS=${Status}\\n' demo-app",
                  {str(built_demo_deb.parent): "/debs"})
    out = chroot.stdout + chroot.stderr
    assert "RC=0" in out, (
        "no /run/systemd/system, so there is no systemd to enable anything with "
        f"-- the install must not care that `systemctl enable` would fail:\n{out}")
    assert "STATUS=install ok installed" in out, out
