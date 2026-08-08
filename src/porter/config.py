"""Config is two files, because one file with two owners cannot work.

Measured 2026-08-07: a single conffile fails an unattended upgrade outright
(dpkg rc=1, "end of file on stdin at conffile prompt") once the admin has
edited it; DEBIAN_FRONTEND=noninteractive does not help, because that governs
debconf and not dpkg's own prompt. A postinst-copies-if-absent file never
fails -- and never delivers a newly added key to an existing client, ever.

Splitting by owner gets both: `defaults` is package-owned so it can never
conflict and new keys always land; `env` is admin-owned and never shipped, so
dpkg never has an opinion about it.
"""
from __future__ import annotations

DEFAULTS_HEADER = (
    "# Package-owned. Overwritten on upgrade -- do NOT edit.\n"
    "# Put site overrides in ./env, which is yours and is never overwritten.\n"
)
ENV_HEADER = (
    "# Admin-owned. The package never overwrites this file.\n"
    "# Values here override ./defaults.\n"
)


def split(template: dict[str, str], admin_keys: list[str]) -> tuple[str, str]:
    """Split one env template into (package-owned defaults, admin-owned env).

    Membership in `admin_keys` is the only thing that decides which file a key
    lands in, and it decides it exclusively: a key in both files is the
    single-file scheme again, one directory over -- the shipped copy would
    overwrite the admin's value on every upgrade because `env` is read last
    only for keys `defaults` does not also re-assert.
    """
    defaults = DEFAULTS_HEADER + "".join(
        f"{k}={v}\n" for k, v in template.items() if k not in admin_keys)
    env = ENV_HEADER + "".join(
        f"{k}={template.get(k, '')}\n" for k in admin_keys)
    return defaults, env


def env_postinst(pkg: str) -> str:
    """Create the admin file if absent. NEVER prompts: a postinst that asks a
    question hangs an unattended install, which is the one thing the whole
    'same command installs and updates' promise rests on.

    The stage must carry `usr/share/<pkg>/env.example` -- this is the file
    copied into place. Keeping the template out of the script keeps
    `env_postinst` independent of any particular key set, and leaves the admin
    a shipped reference of the keys that are theirs, which `/etc/<pkg>/env`
    stops being the moment they edit it.
    """
    return f"""#!/bin/sh
set -e
if [ "$1" = configure ]; then
  # Static system user: stable UID across boots, so /var/lib/{pkg} keeps
  # predictable ownership and a non-root operator can back it up.
  getent group {pkg} >/dev/null || groupadd --system {pkg}
  getent passwd {pkg} >/dev/null || useradd --system --gid {pkg} \\
      --no-create-home --shell /usr/sbin/nologin {pkg}
  mkdir -p /etc/{pkg}
  # Only if absent. On an upgrade this file already holds the client's own
  # secrets; copying over it would lose them silently, at rc=0, which is
  # exactly the harm the split exists to prevent.
  if [ ! -f /etc/{pkg}/env ]; then
    cp /usr/share/{pkg}/env.example /etc/{pkg}/env
    chmod 600 /etc/{pkg}/env
  fi
  systemctl daemon-reload || true
  systemctl enable {pkg}.service >/dev/null 2>&1 || true
  # Restart on upgrade so the operator needs no follow-up command. try-restart
  # is a no-op when the unit is not running, which is the fresh-install case.
  systemctl try-restart {pkg}.service >/dev/null 2>&1 || true
fi
exit 0
"""
