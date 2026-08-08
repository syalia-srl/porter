"""The end-to-end moment: a package that installs and answers HTTP, offline."""
import subprocess
import pytest

pytestmark = pytest.mark.docker


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
    app, the split config and a systemd unit. Run WITHOUT systemd (containers
    have no PID 1 systemd) by invoking ExecStart directly with the two env
    files sourced in order -- the unit ordering itself is asserted in
    test_config.py, and the full unit is exercised by the nspawn gate."""
    script = (
        "set -e; dpkg -i /debs/*.deb >/dev/null; "
        "echo 'GREETING=from-admin' > /etc/demo-app/env; "
        "set -a; . /etc/demo-app/defaults; . /etc/demo-app/env; set +a; "
        "/usr/lib/demo-app/python/bin/python3.12 -m uvicorn app:app "
        "  --app-dir /usr/lib/demo-app --host 127.0.0.1 --port ${PORT} & "
        "sleep 4; curl -fsS http://127.0.0.1:${PORT}/health"
    )
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{built_demo_deb.parent}:/debs:ro", docker_image, "bash", "-c", script],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert '"greeting":"from-admin"' in proc.stdout, proc.stdout
    assert '"tuning":"from-defaults"' in proc.stdout, proc.stdout


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
