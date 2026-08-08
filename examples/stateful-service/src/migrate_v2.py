"""Rewrite the client's state file from schema 1 to schema 2.

Run by the postinst, and only on an upgrade from below 1.10 -- dpkg decides
that, not this script (see porter/migrate.py). What this script does about the
cases it can still be handed is the interesting part:

**No state file at all: exit non-zero.** This is the ainbox failure written as
an assertion. A migration that finds nothing and shrugs is indistinguishable
from a migration that ran correctly, so the one bug it could ever catch -- being
run on a fresh install, against a database that does not exist -- would be the
one bug it hides. Failing here is also what makes the e2e's fresh-install test
mean something: the install exits 0 *because the migration did not run*, and
that is provable only because it would have failed if it had.

**Already schema 2: exit zero.** Idempotent on purpose. `$2` already stops the
re-run, and a second, independent reason not to corrupt the file is worth the
two lines.

Every invocation that gets past the existence check appends a line to
`migrations.log` beside the state file. That log is the witness for "exactly
once": nothing else on the client records how many times a maintainer script
ran, and "the data looks migrated" cannot tell one run from three.
"""
import json
import sys
from pathlib import Path

state_path = Path(sys.argv[1])

if not state_path.exists():
    sys.exit(
        f"migrate_v2: {state_path} does not exist. A migration must never run on "
        "a fresh install -- if this is one, the postinst's $2 guard is broken"
    )

log = state_path.parent / "migrations.log"
with log.open("a") as fh:
    fh.write(f"ran migrate_v2 against {state_path}\n")

state = json.loads(state_path.read_text())
if state["schema"] == 2:
    print("migrate_v2: already at schema 2, nothing to do")
    raise SystemExit(0)
if state["schema"] != 1:
    sys.exit(f"migrate_v2: cannot migrate schema {state['schema']}")

migrated = {"schema": 2, "notes": [{"text": note} for note in state["notes"]]}
# Written beside the original and renamed: a half-written state file is worse
# than an unmigrated one, and the client has no backup of either.
tmp = state_path.with_name(state_path.name + ".migrating")
tmp.write_text(json.dumps(migrated))
tmp.replace(state_path)
print(f"migrate_v2: {state_path} is now schema 2 ({len(migrated['notes'])} notes)")
