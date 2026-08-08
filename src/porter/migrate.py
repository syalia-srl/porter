"""Migrations that run exactly once, because dpkg already knows when to run them.

ainbox's installer runs its beaver 1.x->2.x migration with the comment `ALWAYS,
not just on update` at line 522 of a 970-line script. It is not carelessness:
that script is invoked by hand and cannot tell an install from an upgrade, so
running the migration every time is the only shape that is ever *correct* --
and the price is that it also runs on a fresh install, against a database that
does not exist.

dpkg does not have that problem. A `postinst` is called as

    postinst configure <most-recently-configured-version>

and `$2` is empty when there is no previous version. That single argument is
the whole discrimination, and it is why porter needs no marker file: a stamp in
`/var/lib/<pkg>/` would break rule 6 (the package never writes there) and would
have to be kept in sync with a package state dpkg is already keeping.

So a migration is declared with the version it must run *before*, and fires on
exactly one transition:

    fresh install        $2 empty            no run
    1.9  -> 1.10         $2 lt 1.10          runs, once
    1.10 -> 1.10         $2 not lt 1.10      no run  (re-install)
    1.10 -> 1.11         $2 not lt 1.10      no run  (already migrated)

**Compared by dpkg, never by the shell.** `[ "1.9" \\< "1.10" ]` is false --
lexicographically '9' sorts after '1' -- so a string comparison skips the
migration on a genuine upgrade and reports success. `dpkg --compare-versions`
implements the real ordering, and it is the same one dpkg used to decide this
was an upgrade at all.

**A failing migration must fail the postinst.** No `|| true` anywhere here: the
block runs under the postinst's own `set -e`, so a non-zero migration leaves
the package `half-configured` rather than installed-and-quietly-broken. Each
script runs in a subshell, so a stray `exit 0` inside one cannot skip the rest
of the postinst -- which is where `systemctl enable` lives.
"""
from __future__ import annotations

import subprocess
from collections.abc import Sequence

from porter.types import Migration

__all__ = ["Migration", "migration_postinst"]


def _refuse_a_version_dpkg_cannot_parse(before_version: str) -> None:
    """A `before_version` dpkg cannot read is a migration that never runs.

    This has to be refused at build time, because at run time it is invisible:
    `dpkg --compare-versions vx lt 1.10` exits **1**, which is the same exit
    code as "false" (verified against dpkg 1.22 on 2026-08-08 -- the warning
    goes to stderr, which a postinst's caller does not read). So a `v2.0` or a
    `2.0rc1` in the manifest produces a package that installs at rc=0 on every
    client and silently migrates none of them.
    """
    probe = subprocess.run(["dpkg", "--validate-version", before_version],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        raise ValueError(
            f"before_version {before_version!r} is not a version dpkg can parse: "
            f"{probe.stderr.strip()}. `dpkg --compare-versions` exits 1 for a "
            "malformed version and 1 for false, so this migration would never run "
            "and nothing on the client would say so"
        )


def migration_postinst(pkg: str, migrations: Sequence[Migration]) -> str:
    """The `sh` block that runs `migrations`, for splicing into a postinst.

    A *block* and not a whole script, deliberately, against the letter of the
    task brief: a .deb has exactly one `postinst`, and porter already emits one
    (`config.env_postinst` -- the static user, the admin env file, the
    `systemctl` calls). Returning a second complete postinst would mean a
    caller choosing between them, and the choice that loses is silent: pick the
    migration one and the service is never enabled, pick the other and the
    migration never runs. `env_postinst(pkg, migrations=...)` splices this in
    instead, so there is one emitter and no choice to get wrong.

    The block is written to sit inside `if [ "$1" = configure ]`, after the
    config file exists (a migration may read it) and before `systemctl
    try-restart` (the new code must not meet an unmigrated state).

    Empty when there are no migrations, so the emitted postinst of a package
    without any is byte-identical to what it was before this module existed.
    """
    if not migrations:
        return ""
    for m in migrations:
        _refuse_a_version_dpkg_cannot_parse(m.before_version)

    blocks = []
    for m in migrations:
        blocks.append(
            f'''    if dpkg --compare-versions "$2" lt "{m.before_version}"; then
      echo "porter: {pkg}: upgrading from $2, running the migration for < {m.before_version}"
      # A subshell: a migration that ends in `exit 0` must not skip the rest of
      # this postinst. Its exit status still reaches the `set -e` above.
      (
{m.script}
      )
    fi
''')
    body = "".join(blocks)
    return f'''  # --- migrations ---------------------------------------------------------
  # $2 is the version of {pkg} that was configured BEFORE this one. dpkg leaves
  # it EMPTY on a fresh install, and that is the whole discrimination: a
  # migration that fires here runs against state that does not exist yet.
  if [ -n "$2" ]; then
{body}  fi
'''
