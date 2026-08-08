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

from collections.abc import Sequence

from porter.migrate import Migration, migration_postinst
from porter.types import Component

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


def env_postinst(pkg: str, migrations: Sequence[Migration] = (),
                 has_setup: bool = False) -> str:
    """Create the admin file if absent. NEVER prompts: a postinst that asks a
    question hangs an unattended install, which is the one thing the whole
    'same command installs and updates' promise rests on.

    The stage must carry `usr/share/<pkg>/env.example` -- this is the file
    copied into place. Keeping the template out of the script keeps
    `env_postinst` independent of any particular key set, and leaves the admin
    a shipped reference of the keys that are theirs, which `/etc/<pkg>/env`
    stops being the moment they edit it.

    `migrations` is spliced in **after** the config exists (a migration may read
    it) and **before** `systemctl try-restart` (the new code must not be started
    against state it has not migrated yet). This is the only emitter of a
    postinst: see `migrate.migration_postinst` for why there is not a second one.

    `has_setup` adds one line to a *fresh* install pointing at `<pkg>-setup`.
    Only one line, and only when the package actually ships that script -- an
    airgapped operator has no other place to learn the command exists, and a
    hint naming a binary that is not there is worse than no hint.
    """
    migrate_block = migration_postinst(pkg, migrations)
    # The whole `if`, not just its body: a then-clause with nothing in it is a
    # SYNTAX ERROR in sh, so interpolating an empty body into a fixed `if ...;
    # then / fi` would ship an unrunnable postinst in every package that has no
    # setup script -- which is most of them.
    setup_hint = (
        f'''  # An empty $2 is a fresh install and nothing else, so this is the one moment
  # the operator has not already been told. It is an echo: rule 5 is that this
  # script never ASKS, and printing where to go next is the opposite of asking.
  if [ -z "$2" ]; then
    echo "porter: run '{pkg}-setup' to configure /etc/{pkg}/env"
  fi
''' if has_setup else "")
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
{setup_hint}{migrate_block}  # /run/systemd/system exists only on a BOOTED systemd host. That is the whole
  # discrimination: in a container, a chroot or a debootstrap there is no
  # systemd to enable anything with, and the calls must be skipped. On a real
  # client there is, and a failure must be fatal.
  #
  # The previous shape was `systemctl enable ... >/dev/null 2>&1 || true`, which
  # is correct for the first case and silently wrong for the second: a masked
  # unit, a malformed unit or a full /etc makes `enable` fail, dpkg still exits
  # 0, and the service is simply absent after the next reboot. No `|| true`
  # belongs here -- `set -e` plus dpkg is exactly the machinery for a postinst
  # step that must not fail quietly.
  if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    systemctl enable {pkg}.service
    # Restart on upgrade so the operator needs no follow-up command. try-restart
    # is a no-op when the unit is not running, which is the fresh-install case.
    systemctl try-restart {pkg}.service
  fi
fi
exit 0
"""


def setup_script(component: Component) -> str:
    """`/usr/bin/<pkg>-setup`: the interactive half rule 5 moved out of postinst.

    Rule 5 says the postinst never asks a question. That is not a claim that
    nothing ever needs asking -- `/etc/<pkg>/env` is admin-owned and starts out
    as a copy of `env.example`, i.e. the right keys with none of the values. The
    question has to be asked somewhere; this is the somewhere, and the whole
    point is that it runs when a human is present and dpkg does not.

    Three properties, and each is a decision rather than a detail:

    - **Re-runnable.** Every prompt offers the current value and an empty answer
      keeps it, so running it a second time and pressing Enter through is a
      no-op. A wizard that only works once is a wizard the operator dares not
      run when something is wrong.
    - **It does not own the file.** `/etc/<pkg>/env` is the admin's. Lines whose
      key this script does not manage are carried through untouched, because a
      wizard that rewrites the file from its own idea of the key set deletes
      whatever the admin added -- silently, at rc=0, which is porter's
      characteristic bug in the one file nobody has a copy of.
    - **Written atomically at 600.** `mktemp` in the same directory, `chmod`
      before the `mv`, so there is no window in which the file is readable and
      no half-written file if the disk fills.

    Reads stdin without demanding a tty: `read` from a pipe is a legitimate way
    to drive this, and `read` from a *closed* stdin returns non-zero, which
    `set -e` turns into an exit before anything is written. Neither case hangs,
    which is the property that actually matters.
    """
    pkg = component.package
    if not component.admin_keys:
        raise ValueError(
            f"{pkg} declares no admin_keys, so {pkg}-setup would have nothing to "
            "ask about. A wizard that prompts for nothing and rewrites the admin's "
            "file anyway is worse than no wizard -- do not ship one"
        )
    prompts = "".join(
        f'''cur=$(current {key})
printf '{key} [%s]: ' "$cur"
read new
[ -n "$new" ] || new=$cur
{key}_value=$new
''' for key in component.admin_keys)
    # A single alternation, built once: the keys this script manages and
    # therefore the only lines it may drop from the admin's file.
    managed = "|".join(component.admin_keys)
    writes = "".join(
        f'''printf '{key}=%s\\n' "${key}_value" >> "$tmp"
''' for key in component.admin_keys)
    restart = f'''
# The values are read by systemd at start, so they do not reach a running
# service until it restarts. try-restart is a no-op when it is not running.
if [ -d /run/systemd/system ]; then
  systemctl try-restart {pkg}.service
fi
''' if component.kind == "service" else ""
    return f"""#!/bin/sh
# {pkg}-setup -- first-run configuration for {pkg}.
#
# The postinst never asks a question (porter rule 5), so it creates
# /etc/{pkg}/env from the shipped template and leaves the values to you. This
# is where they are filled in. Safe to re-run: press Enter to keep a value.
set -e
ENVFILE=/etc/{pkg}/env

if [ "$(id -u)" != 0 ]; then
  echo "{pkg}-setup: must run as root -- $ENVFILE is 600 root:root" >&2
  exit 1
fi

mkdir -p /etc/{pkg}
[ -f "$ENVFILE" ] || : > "$ENVFILE"
chmod 600 "$ENVFILE"

# The LAST assignment wins, matching how the shell reads the file when systemd
# hands it to the unit -- so what is offered as the current value is the value
# actually in effect, not the first one written.
current() {{
  sed -n "s/^$1=//p" "$ENVFILE" | tail -n 1
}}

echo "Configuring {pkg}. Enter keeps the current value."
{prompts}
tmp=$(mktemp "/etc/{pkg}/env.XXXXXX")
# Everything this script does NOT manage, carried through. `|| [ $? = 1 ]` and
# not `|| true`: grep exits 1 when it selected no lines, which is an ordinary
# empty file, and 2 on a real error, which must still fail.
grep -vE '^({managed})=' "$ENVFILE" > "$tmp" || [ $? = 1 ]
{writes}chmod 600 "$tmp"
mv "$tmp" "$ENVFILE"
echo "wrote $ENVFILE"
{restart}exit 0
"""
