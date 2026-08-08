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


def test_unit_loads_defaults_then_env_so_admin_wins():
    u = unit("porter-example-service", "Example service",
             "/usr/lib/x/python/bin/python3.12 -m uvicorn a:app", "/usr/lib/x")
    lines = u.splitlines()
    d = lines.index("EnvironmentFile=/etc/porter-example-service/defaults")
    e = lines.index("EnvironmentFile=-/etc/porter-example-service/env")
    assert d < e, "admin env must be read last so it overrides defaults"
