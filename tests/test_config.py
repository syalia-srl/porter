import shutil
import subprocess

import pytest
from conftest import _require_systemd_analyze

from porter.config import split, env_postinst
from porter.systemd import unit

# A tuning constant plus two secrets -- the shape every real service has.
# DB_TDS_VERSION stands in for the case that motivates the whole split: a value
# WE discover and correct later (a driver protocol version measured against an
# old server, say). Package-owned, it reaches every client on upgrade; in a
# single-file scheme a client who had edited their config could never receive it.
TEMPLATE = {"PORT": "9000", "DB_TDS_VERSION": "7.0", "DB_HOST": "", "DB_PASSWORD": ""}
ADMIN = ["DB_HOST", "DB_PASSWORD"]


def test_defaults_holds_package_owned_keys_only():
    defaults, _ = split(TEMPLATE, ADMIN)
    assert "DB_TDS_VERSION=7.0" in defaults
    assert "PORT=9000" in defaults
    assert "DB_HOST" not in defaults


def test_env_holds_admin_keys_as_empty_placeholders():
    _, env = split(TEMPLATE, ADMIN)
    assert "DB_HOST=" in env
    assert "DB_PASSWORD=" in env
    assert "DB_TDS_VERSION" not in env


def test_postinst_creates_env_only_if_absent_and_never_prompts():
    body = env_postinst("porter-example-service")
    # `! -f`, not `-f`. The brief asserted the positive form, which no correct
    # implementation can contain: "create it only if absent" is spelled
    # `if [ ! -f ... ]`, and the positive substring is absent from that. The
    # assertion as written would have failed against the brief's own postinst.
    assert "if [ ! -f /etc/porter-example-service/env ]" in body
    assert "chmod 600" in body
    for interactive in ("read ", "debconf", "db_input"):
        assert interactive not in body, f"postinst must never prompt: found {interactive!r}"


def test_unit_uses_a_static_user_not_dynamicuser():
    """DynamicUser puts state in /var/lib/private/<pkg> at 700 root:root, which
    a non-root operator cannot read or list. Measured 2026-08-07."""
    u = unit("porter-example-service", "Example service", "/x/python3.12 -m uvicorn a:app", "/x")
    assert "DynamicUser" not in u
    assert "User=porter-example-service" in u
    assert "Group=porter-example-service" in u


def test_postinst_creates_the_system_user_and_restarts_on_upgrade():
    body = env_postinst("porter-example-service")
    assert "useradd --system" in body
    assert "try-restart" in body


def _verify(tmp_path, body: str) -> str:
    """`systemd-analyze verify` on a unit file, output read as the result.

    Not the rc: it is 1 whenever ExecStart names a binary the build host does
    not have, which for a real porter unit is always -- the interpreter lives
    at its CLIENT path. So these tests verify the unit's *shape* with a stand-in
    ExecStart, and the real one is exercised in the container e2e.
    """
    path = tmp_path / "porter-example-service.service"
    path.write_text(body)
    proc = subprocess.run(["systemd-analyze", "verify", str(path)],
                          capture_output=True, text=True, cwd=tmp_path)
    return proc.stdout + proc.stderr


def test_the_emitted_unit_parses_with_no_directive_systemd_would_ignore(
        tmp_path, require_systemd_analyze):
    """A misspelled directive is not an error to systemd -- it logs "Unknown
    key ... ignoring" and starts the service anyway. So `EnviromentFile` would
    hand the service an environment with none of its config in it, at rc=0,
    and only a runtime KeyError downstream would say so.

    The positive control is the point: prove the probe SEES a bad directive
    before trusting it to report a clean one.
    """
    good = unit("porter-example-service", "Example service", "/bin/true", "/tmp")
    assert "Unknown key" not in _verify(tmp_path, good), _verify(tmp_path, good)

    bad = good.replace("EnvironmentFile=-", "EnviromentFile=-")
    assert "Unknown key 'EnviromentFile'" in _verify(tmp_path, bad), (
        "systemd-analyze did not report a directive it does not know: this probe "
        "cannot detect the failure it exists to detect"
    )


def _outcome(fn) -> str:
    """Run `fn` and NAME what it did, rather than letting pytest's own
    control-flow exceptions become this test's outcome.

    `with pytest.raises(pytest.fail.Exception)` is the obvious spelling and it
    is a trap here. `Skipped` is not a subclass of `Failed`, so a skip is not
    caught: it propagates, and pytest marks *this* test skipped -- green, and
    silent, which is the precise failure the variable under test exists to
    prevent, reproduced inside its own test. scripts/reverify-guards.sh caught
    it 2026-08-08 by reporting the guard removed and the suite still green.
    """
    try:
        fn()
    except pytest.fail.Exception:
        return "failed"
    except pytest.skip.Exception:
        return "skipped"
    return "returned"


def test_a_missing_systemd_analyze_cannot_skip_an_armed_run(monkeypatch):
    """The variable is the guard, so the variable is what is tested.

    `test_the_emitted_unit_parses...` was the sole constraint on seven of the
    unit's directives and it skipped silently when the binary was absent -- on
    a slim CI image the suite would have exited 0 with all seven unconstrained.
    PORTER_REQUIRE_SYSTEMD turns that skip into a failure, and both branches are
    asserted because a variable nothing reads is the same silence with more
    ceremony.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    monkeypatch.delenv("PORTER_REQUIRE_SYSTEMD", raising=False)
    assert _outcome(_require_systemd_analyze) == "skipped", (
        "unarmed, a missing systemd-analyze should skip: a contributor without "
        "it must not face a wall of failures")

    monkeypatch.setenv("PORTER_REQUIRE_SYSTEMD", "1")
    assert _outcome(_require_systemd_analyze) == "failed", (
        "armed, a missing systemd-analyze must FAIL the run: skipping here is a "
        "green suite with seven unit directives unconstrained")


def _sections(body: str) -> dict[str, list[str]]:
    """Unit file -> {section: [lines]}. Placement is half of each directive's
    meaning: `WantedBy=` in `[Service]` is a key systemd does not know there,
    and `ProtectSystem=` in `[Install]` is silently inert."""
    out: dict[str, list[str]] = {}
    current = None
    for line in body.splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            out[current] = []
        elif line and current is not None:
            out[current].append(line)
    return out


def test_unit_carries_the_hardening_and_restart_directives_in_the_right_sections():
    """Presence, values and placement -- independent of `systemd-analyze`.

    Worth having even though `systemd-analyze verify` runs on the same unit,
    because the two checks are orthogonal rather than redundant. `verify`
    reports keys it does not *recognise*; it says nothing about keys that are
    *missing*, and a unit with all seven of these deleted verifies perfectly
    clean. It also does not run at all on a host without the binary -- which,
    until PORTER_REQUIRE_SYSTEMD, was every host where nobody noticed.

    So: `verify` pins spelling and section-validity, this pins presence and
    value. Dropping either one leaves a real hole. Exact-line membership rather
    than substring, so a value change (`RestartSec=3` -> `30`) or a directive
    demoted into a comment fails here.
    """
    body = unit("porter-example-service", "Example service", "/bin/true", "/tmp")
    sections = _sections(body)
    service = [
        "StateDirectory=porter-example-service",
        "ProtectSystem=strict",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
        "Restart=on-failure",
        "RestartSec=3",
    ]
    missing = [d for d in service if d not in sections.get("Service", [])]
    assert not missing, f"[Service] lost {missing}:\n{body}"
    assert "WantedBy=multi-user.target" in sections.get("Install", []), (
        f"the unit would install into no target and never start at boot:\n{body}")


def test_unit_loads_defaults_then_env_so_admin_wins():
    u = unit("porter-example-service", "Example service",
             "/usr/lib/x/python/bin/python3.12 -m uvicorn a:app", "/usr/lib/x")
    lines = u.splitlines()
    d = lines.index("EnvironmentFile=/etc/porter-example-service/defaults")
    e = lines.index("EnvironmentFile=-/etc/porter-example-service/env")
    assert d < e, "admin env must be read last so it overrides defaults"
