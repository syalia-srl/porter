"""The upgrade, executed: dpkg decides when a migration runs and it runs once.

Everything here is asserted against an installed package on a client that has
no Python and no network. `tests/test_migrate.py` proves the *decision* by
running the generated block directly; this file proves that dpkg passes `$2` at
all, that a failing migration leaves the package unconfigured, that the wizard
writes a file at 600 root:root, and that none of it ever reaches a prompt.

The fixtures build three .debs -- 1.9, 1.10 and 1.11 -- from the two manifests
in examples/stateful-service/. 1.9 and 1.10 are the gallery's own pair; 1.11 is
1.10's staged tree packaged again with a bumped version, which is the cheapest
honest way to ask "does a migration for < 1.10 fire on an upgrade from 1.10".
"""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

# The arming helper, not a copy of it: a duplicated `shutil.which("uv")` here
# would drift from conftest's and would not honour PORTER_REQUIRE_UV, so a CI
# run could skip this whole file and still exit 0. Imported rather than taken as
# a fixture because `require_uv` is function-scoped and the .debs are built once
# per session.
from conftest import _require_uv

from porter.assemble import assemble
from porter.deb import build_deb
from porter.types import Component

pytestmark = pytest.mark.docker

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "stateful-service"
PKG = "stateful-demo"
STATE = f"/var/lib/{PKG}/state.json"
LOG = f"/var/lib/{PKG}/migrations.log"
UNIT = f"/usr/lib/systemd/system/{PKG}.service"

# Read the unit's own ExecStart and WorkingDirectory and run the first from the
# second, exactly as tests/test_service_e2e.py does and for the same reason: a
# hand-copied command line is a command line that can supply what the unit does
# not. Containers have no PID 1 systemd, so this is as close as a container gets.
READ_UNIT = (
    f"WD=$(sed -n 's/^WorkingDirectory=//p' {UNIT}); "
    f"ES=$(sed -n 's/^ExecStart=//p' {UNIT}); "
    'test -n "$WD"; test -n "$ES"; '
    f"set -a; . /etc/{PKG}/defaults; . /etc/{PKG}/env; set +a; "
)


def _run(image: str, script: str, mounts: dict[str, str]) -> subprocess.CompletedProcess:
    """One container, no network, the .deb directories mounted read-only."""
    argv = ["docker", "run", "--rm", "--network", "none"]
    for host, guest in mounts.items():
        argv += ["-v", f"{host}:{guest}:ro"]
    argv += [image, "bash", "-c", script]
    return subprocess.run(argv, capture_output=True, text=True)


def _section(out: str, name: str) -> str:
    """The text between `--- <name>` and the next `---` marker."""
    assert f"--- {name}" in out, f"the container never reached '{name}':\n{out}"
    return out.split(f"--- {name}\n", 1)[1].split("\n--- ", 1)[0]


@pytest.fixture(scope="session")
def stateful_debs(tmp_path_factory) -> dict[str, Path]:
    """1.9, 1.10 and 1.11, each alone in its own directory.

    Alone because every install below globs `/vN/*.deb`; a second package in
    the same directory would be installed with it.

    Built through `assemble`, not by hand: the whole claim of this file is
    about what porter *emits* for a manifest declaring `migrations:`, and a
    fixture that wrote its own postinst would be testing the fixture.
    """
    _require_uv()
    root = tmp_path_factory.mktemp("stateful")
    debs = {}

    for label, manifest_name in (("v1.9", "porter-previous.yaml"),
                                 ("v1.10", "porter.yaml")):
        manifest = yaml.safe_load((EXAMPLE / manifest_name).read_text())
        component, python = Component.from_manifest(manifest)
        staged = assemble(component, python, EXAMPLE, root / label / "stage")
        debs[label] = build_deb(staged.stage, staged.control, root / label / "debs",
                                conffiles=staged.conffiles, scripts=staged.scripts)
        if label == "v1.10":
            # The same tree, packaged again at 1.11. Nothing else changes --
            # which is the point: the only reason 1.10 -> 1.11 must not migrate
            # is the version, so the version must be the only difference.
            debs["v1.11"] = build_deb(
                staged.stage, {**staged.control, "Version": "1.11"},
                root / "v1.11" / "debs",
                conffiles=staged.conffiles, scripts=staged.scripts)

    for label, deb in debs.items():
        assert deb.stat().st_size > 10 * 1024 * 1024, (
            f"{label} is {deb.stat().st_size} bytes: a package that small carries "
            "no vendored interpreter, and every assertion below would be about a "
            "package the client cannot run")
    return debs


@pytest.fixture(scope="session")
def mounts(stateful_debs) -> dict[str, str]:
    return {str(stateful_debs["v1.9"].parent): "/v1",
            str(stateful_debs["v1.10"].parent): "/v2",
            str(stateful_debs["v1.11"].parent): "/v3"}


def test_a_fresh_install_runs_no_migration(docker_image, mounts):
    """`$2` is empty and the migration must not fire.

    The second half is the positive control, and without it this test is worth
    nothing: `rc=0` would be equally consistent with "the migration ran and
    found nothing to do". So the same migration is then run BY HAND, in the same
    container, against the same absent state file -- and it fails. The install's
    rc=0 therefore means the migration did not run, not that running it was
    harmless.
    """
    script = (
        "set -e; dpkg -i /v2/*.deb >/dev/null; "
        "dpkg-query -W -f 'STATUS=${Status}\\n' " + PKG + "; "
        f"if [ -e /var/lib/{PKG} ]; then echo 'STATEDIR=created'; "
        "else echo 'STATEDIR=absent'; fi; "
        "set +e; "
        f"/usr/lib/{PKG}/python/bin/python3.12 /usr/lib/{PKG}/migrate_v2.py {STATE} "
        "; echo \"CONTROL_RC=$?\""
    )
    proc = _run(docker_image, script, mounts)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "STATUS=install ok installed" in out, out
    assert "STATEDIR=absent" in out, (
        "a fresh install left client state behind: the package wrote to "
        f"/var/lib/{PKG}, which is the client's (rule 6)\n{out}")
    assert "CONTROL_RC=0" not in out, (
        "the migration succeeds against an absent state file, so 'the install "
        f"exited 0' does not prove it was skipped:\n{out}")


def test_an_upgrade_migrates_once_and_the_clients_own_data_survives(
        docker_image, mounts):
    """The whole arc, in one container, in the order a client lives it.

    1.9 is installed, configured with `stateful-demo-setup` the way an operator
    configures it, and started so that it writes real state. Then 1.10 upgrades
    it -- and the migration runs, exactly once, rewriting the client's own note
    into schema 2 without losing it. Re-installing 1.10 and then upgrading to
    1.11 must not run it again: `migrations.log` counts invocations, because
    "the data looks migrated" cannot tell one run from three.

    The last block is the control for the middle one. A schema-1 state file is
    written by hand and 1.10's own ExecStart is run against it: it must refuse.
    Without that, "the service started after the upgrade" would not distinguish
    a migrated state file from a payload that never checked.
    """
    runs = f"$(grep -c '^ran migrate_v2' {LOG} 2>/dev/null || true)"
    script = (
        "set -e; dpkg -i /v1/*.deb >/dev/null; "
        # The wizard, driven through a pipe. It is interactive; it does not
        # demand a tty, and `read` from a pipe is how a human's answer arrives.
        f"printf 'hola-desde-el-cliente\\n' | {PKG}-setup >/dev/null; "
        + READ_UNIT +
        # 1.9 running is what creates the client's state.
        '( cd "$WD" && eval "$ES" ) & V1=$!; sleep 3; kill $V1 2>/dev/null || true; '
        f"echo '--- state-1.9'; cat {STATE}; echo; "
        "dpkg -i /v2/*.deb >/dev/null; "
        f"echo '--- state-1.10'; cat {STATE}; echo; "
        f"echo \"--- runs\"; echo \"RUNS={runs}\"; "
        # 1.10's payload, on the migrated state: it must come up and serve.
        + READ_UNIT +
        '( cd "$WD" && eval "$ES" ) & V2=$!; sleep 3; '
        "echo '--- served'; curl -fsS http://127.0.0.1:${PORT}/; echo; "
        "kill $V2 2>/dev/null || true; sleep 1; "
        # Re-install of the same version, then an upgrade from above.
        "dpkg -i /v2/*.deb >/dev/null; dpkg -i /v3/*.deb >/dev/null; "
        f"echo '--- runs-after'; echo \"RUNS={runs}\"; "
        "dpkg-query -W -f 'VERSION=${Version}\\n' " + PKG + "; "
        # Control: 1.10's ExecStart against an UNMIGRATED state file.
        f"echo '--- control'; echo '{{\"schema\": 1, \"notes\": [\"x\"]}}' > {STATE}; "
        'set +e; ( cd "$WD" && timeout 5 sh -c "$ES" ) >/dev/null 2>&1; '
        'echo "CONTROL_RC=$?"'
    )
    proc = _run(docker_image, script, mounts)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out

    before = json.loads(_section(proc.stdout, "state-1.9"))
    assert before == {"schema": 1, "notes": ["hola-desde-el-cliente"]}, (
        f"1.9 did not write the state this test is about:\n{out}")

    after = json.loads(_section(proc.stdout, "state-1.10"))
    assert after == {"schema": 2, "notes": [{"text": "hola-desde-el-cliente"}]}, (
        f"the upgrade did not migrate the client's state:\n{out}")

    assert "RUNS=1" in _section(proc.stdout, "runs"), (
        f"the migration did not run exactly once on the 1.9 -> 1.10 upgrade:\n{out}")
    assert "RUNS=1" in _section(proc.stdout, "runs-after"), (
        "the migration ran again on a re-install of 1.10 or on the upgrade to "
        f"1.11, both of which are at or above its before_version:\n{out}")
    assert "VERSION=1.11" in out, out

    served = json.loads(_section(proc.stdout, "served"))
    assert served["notes"] == [{"text": "hola-desde-el-cliente"}], (
        f"1.10's payload did not serve the migrated state:\n{out}")

    control = _section(proc.stdout, "control")
    assert "CONTROL_RC=1" in control, (
        "1.10's payload started against a schema-1 state file (or was killed by "
        "the timeout, rc=124): 'the service came up after the upgrade' therefore "
        f"proves nothing about the migration having run:\n{out}")


def test_a_failing_migration_leaves_the_package_unconfigured(docker_image, mounts):
    """Half-migrated and reported healthy is the outcome this refuses.

    The failure is staged the way it actually happens: the client has a state
    directory and the file inside it is gone -- restored from a backup that
    missed it, deleted by an operator clearing space. migrate_v2 exits non-zero,
    and because nothing in the emitted block swallows a status, dpkg leaves the
    package `half-configured`. An operator who runs the install again sees it
    fail again, which is the whole difference from a silent success.
    """
    script = (
        "set -e; dpkg -i /v1/*.deb >/dev/null; "
        f"mkdir -p /var/lib/{PKG}; "
        "set +e; dpkg -i /v2/*.deb >/dev/null 2>&1; echo \"RC=$?\"; "
        "dpkg-query -W -f 'STATUS=${Status}\\n' " + PKG + "; "
        "dpkg-query -W -f 'VERSION=${Version}\\n' " + PKG
    )
    proc = _run(docker_image, script, mounts)
    out = proc.stdout + proc.stderr
    assert "RC=0" not in out, (
        f"the migration failed and dpkg still reported a clean upgrade:\n{out}")
    assert "STATUS=install ok half-configured" in out, (
        f"expected dpkg to leave the package unconfigured after the failure:\n{out}")


def test_the_upgrade_reaches_no_prompt_under_setsid_with_stdin_closed(
        docker_image, mounts):
    """Rule 5, measured: the postinst never asks a question.

    `setsid` detaches the controlling terminal and `0<&-` closes stdin, so any
    read -- from stdin, from /dev/tty, from debconf -- fails instead of blocking.
    `DEBIAN_FRONTEND=noninteractive` is deliberately NOT set: it governs debconf
    and not dpkg's own conffile prompt, so setting it here would only obscure
    which mechanism was responsible.

    The control is the first half and it carries the test. "No prompt was
    reached" is what a harness that cannot detect prompts also reports, so a
    bare `read` is run under the same `setsid ... 0<&-` first and must fail. If
    it ever passes, everything below it is meaningless.
    """
    script = (
        "set -e; command -v setsid >/dev/null; dpkg -i /v1/*.deb >/dev/null; "
        # The client has state, so this upgrade really does run the migration.
        + READ_UNIT +
        '( cd "$WD" && eval "$ES" ) & V=$!; sleep 3; kill $V 2>/dev/null || true; '
        "set +e; "
        "echo '--- control'; "
        # `set -e` INSIDE the control, and `&&` would do as well: the first
        # version of this control was `read x; echo REACHED_A_PROMPT`, where the
        # echo runs whatever `read` did and the marker printed on a harness that
        # was working perfectly. A control that cannot fail is worse than none.
        "setsid -w sh -c 'set -e; read x; echo REACHED_A_PROMPT' 0<&-; "
        'echo "CONTROL_RC=$?"; '
        # The other way a maintainer script asks: straight at the terminal,
        # which is what debconf's dialog frontends do. setsid took it away.
        "setsid -w sh -c 'set -e; read x </dev/tty; echo REACHED_A_TTY' 0<&-; "
        'echo "TTY_RC=$?"; '
        "echo '--- install'; "
        "setsid -w sh -c 'dpkg -i /v2/*.deb' 0<&- >/dev/null 2>&1; "
        'echo "INSTALL_RC=$?"; '
        "dpkg-query -W -f 'STATUS=${Status}\\n' " + PKG + "; "
        f"echo '--- state'; cat {STATE}"
    )
    proc = _run(docker_image, script, mounts)
    out = proc.stdout + proc.stderr

    control = _section(proc.stdout, "control")
    assert "REACHED_A_PROMPT" not in out and "REACHED_A_TTY" not in out, (
        "a bare `read` got past setsid with stdin closed, so this harness cannot "
        f"detect a postinst that asks a question:\n{out}")
    assert "CONTROL_RC=0" not in control and "TTY_RC=0" not in control, (
        f"`read` exited 0 with no stdin and no tty: the control is broken\n{out}")

    assert "INSTALL_RC=0" in _section(proc.stdout, "install"), (
        f"the upgrade did not complete with no tty and no stdin:\n{out}")
    assert "STATUS=install ok installed" in out, out
    assert json.loads(_section(proc.stdout, "state"))["schema"] == 2, (
        f"the install exited 0 but the migration did not run:\n{out}")


def test_the_setup_wizard_is_re_runnable_and_does_not_own_the_admins_file(
        docker_image, mounts):
    """`<pkg>-setup` is where the questions live, and it is safe to run twice.

    Four claims, all against the file on disk rather than the script's text:
    the first run records an answer; an empty answer on the second keeps it; a
    third answer replaces the line instead of appending a second one dpkg's
    readers would disagree about; and a key the wizard has never heard of --
    the admin's own -- survives all three.

    The mode is checked here for the reason test_service_e2e checks it: `chmod
    600` in a generated script proves nothing about the file that ends up on the
    client.
    """
    envfile = f"/etc/{PKG}/env"
    script = (
        "set -e; echo '--- install'; dpkg -i /v1/*.deb; "
        f"stat -c 'SETUP=%a' /usr/bin/{PKG}-setup; "
        f"echo 'CUSTOM=the-admins-own-key' >> {envfile}; "
        f"printf 'first\\n' | {PKG}-setup >/dev/null; "
        f"echo '--- first'; cat {envfile}; "
        f"stat -c 'MODE=%a:%U:%G' {envfile}; "
        f"printf '\\n' | {PKG}-setup >/dev/null; "
        f"echo '--- second'; cat {envfile}; "
        f"printf 'third\\n' | {PKG}-setup >/dev/null; "
        f"echo '--- third'; cat {envfile}; "
        f"echo \"GREETING_LINES=$(grep -c '^GREETING=' {envfile})\"; "
        # And the values really are what the service reads.
        + READ_UNIT +
        '( cd "$WD" && eval "$ES" ) & V=$!; sleep 3; '
        "echo '--- served'; curl -fsS http://127.0.0.1:${PORT}/; echo; "
        "kill $V 2>/dev/null || true"
    )
    proc = _run(docker_image, script, mounts)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out

    assert f"run '{PKG}-setup'" in _section(proc.stdout, "install"), (
        "a fresh install never told the operator the wizard exists, and an "
        f"airgapped client has nowhere else to find out:\n{out}")
    assert "SETUP=755" in out, (
        f"/usr/bin/{PKG}-setup is installed without the executable bit:\n{out}")

    first = _section(proc.stdout, "first")
    assert "GREETING=first" in first, out
    assert "CUSTOM=the-admins-own-key" in first, (
        f"the wizard deleted a key the admin added and it never asked about:\n{out}")
    assert "MODE=600:root:root" in out, out

    second = _section(proc.stdout, "second")
    assert "GREETING=first" in second, (
        f"an empty answer did not keep the current value, so re-running the "
        f"wizard is not safe:\n{out}")
    assert "CUSTOM=the-admins-own-key" in second, out

    third = _section(proc.stdout, "third")
    assert "GREETING=third" in third, out
    assert "GREETING=first" not in third, out
    assert "GREETING_LINES=1" in out, (
        f"the wizard appended a second GREETING= line instead of replacing "
        f"the first:\n{out}")

    served = json.loads(_section(proc.stdout, "served"))
    assert served["notes"] == ["third"], (
        f"the value the wizard wrote did not reach the running service:\n{out}")
