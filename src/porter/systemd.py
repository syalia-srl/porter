"""Units replace compose services.

depends_on/service_healthy has no systemd equivalent; ordering is After=/
Requires= plus Restart=on-failure convergence. That is a deliberate accepted
behavioural change, recorded in docs/design-spec.md.

NOT DynamicUser=yes. Measured 2026-08-07: it redirects StateDirectory to
/var/lib/private/<pkg>, mode 700 root:root, which a non-root operator can
neither read nor list -- so backups, monitoring and support all need root, and
admin-dropped files silently change owner as the UID rotates. A static system
user created in postinst gives a real /var/lib/<pkg> at 750 root:<pkg>, which
passed every non-root access check the private layout failed.
"""
from __future__ import annotations


def unit(pkg: str, description: str, exec_start: str, workdir: str,
         user: str | None = None, after: str = "network.target") -> str:
    user = user or pkg
    # EnvironmentFile order is the whole split: defaults first, admin second,
    # so the admin's value is the one systemd keeps. The `-` prefix on env
    # makes it optional -- the unit must still start on a client whose admin
    # has removed it, and a missing *required* EnvironmentFile is a start
    # failure with no output beyond the unit's status.
    return f"""[Unit]
Description={description}
After={after}

[Service]
EnvironmentFile=/etc/{pkg}/defaults
EnvironmentFile=-/etc/{pkg}/env
WorkingDirectory={workdir}
ExecStart={exec_start}
User={user}
Group={user}
StateDirectory={pkg}
ProtectSystem=strict
PrivateTmp=yes
NoNewPrivileges=yes
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
