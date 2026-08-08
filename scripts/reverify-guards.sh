#!/usr/bin/env bash
# Re-verify every guard porter relies on, with bytecode caches PURGED.
#
# Run this before any release. It is the executable form of the rule at the door:
# a check that cannot fail is worth less than no check, because it licenses
# shipping. Each entry disables one guard AT ITS USE SITE and asserts the suite
# goes red; the run ends by confirming the suite is green again, so a dirty
# harness cannot masquerade as a pass.
#
# Mutate the use site, never a constant. `ALLOWED_TOP_LEVEL = () or (...)` is the
# ORIGINAL tuple -- () is falsy -- so that "mutation" changes nothing and the
# harness cheerfully reports the guard as broken. Measured here 2026-08-08.
#
# Add an entry for every new guard. A guard with no entry here is untested by
# the only test that matters.
#
# Task 3 found that a reverted mutation kept running: CPython invalidates .pyc on
# source mtime AND size, so an equal-length edit reverted inside the same second
# leaves stale bytecode Python considers current. That threatens every mutation
# result recorded before the finding -- including Tasks 1 and 2, whose guards are
# what the release gate rests on.
#
# Method: mutate -> purge -> run -> restore -> purge -> confirm green again.
# The trailing re-run is the control: if the suite is not green after restore,
# the harness itself is dirty and no verdict above it means anything.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Run in a DETACHED WORKTREE, never the shared checkout.
#
# This script edits source files in place. porter's checkout is shared with peer
# agents, and on 2026-08-08 a reviewer found this running mid-mutation while it
# was reading the tree -- it had to verify in its own worktree to get an honest
# answer. A mutation harness that can make a concurrent reviewer read a mutant is
# worse than no harness.
#
# PORTER_GUARDS_INPLACE=1 opts out (CI, where the checkout is nobody else's).
if [ "${PORTER_GUARDS_INPLACE:-0}" != 1 ]; then
  WT="$(mktemp -d)/guards"
  git -C "$REPO" worktree add --detach -q "$WT" HEAD || { echo "ABORT: cannot create worktree"; exit 2; }
  trap 'git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1; rm -rf "$(dirname "$WT")"' EXIT
  echo "  isolated in $WT ($(git -C "$WT" rev-parse --short HEAD))"
  cd "$WT" || exit 2
else
  echo "  IN-PLACE in $REPO — only safe when no peer shares this checkout"
  cd "$REPO" || exit 2
fi
purge() { find src tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true; }
# All three gates armed. A skipped test is green, and a green run here would be
# read as "the guard still bites" when in fact nothing ran -- the same silence
# this script exists to detect, one level up. On a host missing uv, docker or
# systemd-analyze the baseline below goes red and says so, which is correct: this
# is a release gate, not a contributor's first run.
run()   { PORTER_REQUIRE_UV=1 PORTER_REQUIRE_DOCKER=1 PORTER_REQUIRE_SYSTEMD=1 \
            uv run --extra dev pytest -q "$@" >/tmp/rv.log 2>&1; echo $?; }

fail=0
check() {  # check <label> <file> <expr> <scope> [max-changed-lines, default 2]
  local label=$1 file=$2 expr=$3 scope=$4 maxlines=${5:-2}
  cp "$file" /tmp/rv.keep
  uv run python - "$file" <<PY
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
$expr
p.write_text(s)
PY
  if cmp -s "$file" /tmp/rv.keep; then
    echo "  SKIP  $label — pattern did not match; the file is unchanged"
    cp /tmp/rv.keep "$file"; purge; fail=1; return
  fi
  # Trap 6: `s.replace(old, new)` is a SUBSTRING match with no occurrence limit.
  # A Task 4 entry drifted onto Task 11's .timer branch and made this gate report
  # FAIL on healthy code (2026-08-08). With 67 entries that recurs, and the
  # failure mode is indistinguishable from a real regression. A mutation is meant
  # to disable ONE guard: if it rewrote several places, the verdict below is
  # about something other than the guard named.
  local changed
  changed=$(diff /tmp/rv.keep "$file" | grep -c '^[<>]' || true)
  if [ "${changed:-0}" -gt "$maxlines" ]; then
    echo "  SKIP  $label — mutation rewrote $changed lines, allowance $maxlines. Narrow the pattern, or raise the allowance if it is deliberate."
    cp /tmp/rv.keep "$file"; purge; fail=1; return
  fi
  purge
  local rc; rc=$(run "$scope")
  cp /tmp/rv.keep "$file"; purge
  if [ "$rc" -ne 0 ]; then echo "  PASS  $label — guard removed => suite red (rc=$rc)"
  else echo "  FAIL  $label — guard removed and suite STAYED GREEN"; fail=1; fi
}

echo "════ baseline (must be green, or nothing below is meaningful) ════"
purge; base=$(run); echo "  baseline rc=$base"
[ "$base" -eq 0 ] || { echo "ABORT: suite is not green before mutating"; exit 2; }

echo "════ Task 1 — interpreter provenance ════"
check "T1 provenance: root must be under uv python dir" src/porter/interpreter.py \
  's = s.replace("uv\", \"python\", \"dir\"", "uv\", \"python\", \"dir_DISABLED\"")' tests/test_interpreter.py

echo "════ Task 2 — the FHS lint ════"
check "T2 top-level allowlist" src/porter/deb.py \
  's = s.replace("if entry.name not in ALLOWED_TOP_LEVEL:", "if False:")' tests/test_deb.py
check "T2 client-owned refusal" src/porter/deb.py \
  's = s.replace("if p.exists() and any(p.rglob(\"*\")):", "if False:")' tests/test_deb.py
check "T2 env never ships" src/porter/deb.py \
  's = s.replace("if p.exists() or p.is_symlink():", "if False:")' tests/test_deb.py
check "T2 undeclared /etc conffile" src/porter/deb.py \
  's = s.replace("if shipped not in declared:", "if False:")' tests/test_deb.py

echo "════ Task 3 — the unit, and the postinst's systemctl block ════"
# The container e2e reads ExecStart= and WorkingDirectory= out of the INSTALLED
# unit and runs the first from the second. Break the second and it must go red;
# the pre-fix test, which hand-copied the command line with an extra --app-dir,
# stayed green through exactly this mutation.
check "T3 WorkingDirectory is the directory the service runs in" src/porter/systemd.py \
  's = s.replace("WorkingDirectory={workdir}", "WorkingDirectory=/")' tests/test_service_e2e.py
# The two halves of the postinst's systemd discrimination, one mutation each.
check "T3 a failed systemctl enable on a booted host is fatal" src/porter/config.py \
  's = s.replace("systemctl enable {pkg}.service", "systemctl enable {pkg}.service || true")' \
  tests/test_service_e2e.py
check "T3 a host with no booted systemd skips systemctl entirely" src/porter/config.py \
  's = s.replace("if [ -d /run/systemd/system ]", "if [ -d / ]")' tests/test_service_e2e.py 6
# Presence of the hardening directives, which systemd-analyze cannot see: it
# reports keys it does not recognise, never keys that are absent.
check "T3 the unit carries its hardening directives" src/porter/systemd.py \
  's = s.replace("ProtectSystem=strict\n", "")' tests/test_config.py 6
# And the arming variable itself: disarmed, a missing systemd-analyze goes back
# to skipping silently and taking seven directives with it.
check "T3 PORTER_REQUIRE_SYSTEMD arms the systemd-analyze skip" tests/conftest.py \
  's = s.replace("PORTER_REQUIRE_SYSTEMD\", \"\"", "PORTER_REQUIRE_SYSTEMD_OFF\", \"\"")' \
  tests/test_config.py

echo "════ Task 3 — guards verified once by hand and, until now, on no run ════"
check "T3 split: admin keys are excluded from defaults" src/porter/config.py \
  's = s.replace("if k not in admin_keys", "if True")' tests/test_config.py
check "T3 postinst creates env only when absent" src/porter/config.py \
  's = s.replace("if [ ! -f /etc/{pkg}/env ]; then", "if true; then")' tests/
check "T3 env is chmod 600" src/porter/config.py \
  's = s.replace("chmod 600", "chmod 644")' tests/ 8
check "T3 admin env is read, and read last" src/porter/systemd.py \
  's = s.replace("EnvironmentFile=-/etc/{pkg}/env", "EnvironmentFile=/etc/{pkg}/defaults")' tests/test_config.py

echo "════ Task 4 — assemble ════"
# RETIRED by Task 11: "T4 oneshot is refused by name". Its subject -- the
# refusal branch in assemble -- no longer exists, because `oneshot` is emitted
# now. Deleting it was not optional and not cosmetic: `str.replace` is a
# SUBSTRING match, so the retired pattern `if component.kind == "oneshot":`
# still matched, but it matched Task 11's `.timer` branch instead, disabled
# that, and scoped the run to tests/test_assemble.py -- which does not test
# timers. Measured 2026-08-08: the entry reported
#   FAIL ... guard removed and suite STAYED GREEN
# on a codebase whose guards were all fine. A stale entry does not go quiet
# when its subject disappears; it drifts onto whatever line it still matches.
# "T11 a oneshot ships a .timer" is the live guard for that branch.
#
# This one is a real refusal, and nothing else catches it: with it gone,
# `kind: sevice` falls through to the command branch and assemble RETURNS a
# staged wrapper at rc=0 for a component that asked for a unit.
#
# That symptom is the FIXTURE's, and the fixture carries a bin_name. The typo
# this guard is really for -- a *service* manifest with `kind: sevice`, which
# therefore has no bin_name -- lands instead in the `bindir / None` TypeError
# the bin_name entry above is honestly labelled incidental for. The guard is
# worth having either way; rc=0 with a wrapper is not the only shape it stops.
check "T4 an unknown kind is refused" src/porter/assemble.py \
  's = s.replace("if component.kind not in SUPPORTED_KINDS:", "if False:")' tests/test_assemble.py
check "T4 an interpreter porter does not bundle is refused" src/porter/assemble.py \
  's = s.replace("if not python.bundled:", "if False:")' tests/test_assemble.py
# Removed, a command's declared env is silently dropped: the command branch
# writes no /etc at all, so assemble returns rc=0 having thrown the config away.
check "T4 a command may not declare config" src/porter/assemble.py \
  's = s.replace("if component.defaults or component.admin_keys:", "if False:")' tests/test_assemble.py
# Incidental failure, not a silent success: without it, `bindir / None` raises
# TypeError deep in staging. The guard buys a legible refusal BEFORE 97 MB is
# vendored, which is worth an entry but is not the same class as the others.
check "T4 a command needs a bin_name (incidental -- TypeError without it)" \
  src/porter/assemble.py \
  's = s.replace("if not component.bin_name:", "if False:")' tests/test_assemble.py
# Removed, a second build into a directory still holding the previous
# component's tree stages on top of it and ships both, at rc=0.
check "T4 a non-empty stage root is refused, never emptied" src/porter/assemble.py \
  's = s.replace("if stage.exists() and any(stage.iterdir()):", "if False:")' tests/test_assemble.py
# The airgap failure that looks like success on the build host: requirements
# omitted, .deb builds and lints and installs, ExecStart dies on the client.
check "T4 the staged interpreter must be able to import the module" src/porter/assemble.py \
  's = s.replace("if probe.returncode != 0:", "if False:")' tests/test_assemble.py
# conffiles are DERIVED from the staged tree and returned, so the caller cannot
# get them wrong. Emptied, deb.py's lint refuses the build -- loudly, which is
# the designed outcome and the reason assemble returns them at all.
check "T4 conffiles are derived from the staged etc/ tree" src/porter/assemble.py \
  's = s.replace("return sorted(found)", "return []")' tests/test_assemble.py
# `module` is the RUNNER (uvicorn) and the payload is its argument, so the probe
# above is satisfied by a manifest that forgot fastapi. Removed, the gallery
# builds and installs at rc=0 and dies at the first HTTP request on the client.
check "T4 the payload's own imports must be findable too" src/porter/assemble.py \
  's = s.replace("if found.returncode != 0:", "if False:")' tests/test_assemble.py
# The one failure neither probe can see: find_spec("src") SUCCEEDS on a
# directory with no __init__.py -- it is a namespace package -- while nothing
# inside it is importable from the payload root. Removed, `source: [src]` stages
# a payload one directory below WorkingDirectory and ships at rc=0.
check "T4 a source directory below the import root is refused" src/porter/assemble.py \
  's = s.replace("if modules:", "if False:")' tests/test_assemble.py

echo "════ Task 4 fix 1 — the entry point, which had no test at all ════"
# THE entry: every assemble test passes an absolute tmp_path, so the suite was
# blind to `porter build`'s own defaults being relative. Removed, the import
# probe execs a relative interpreter path with cwd= set below it, the child
# chdir's before exec, and `porter build` fails on porter's own example.
# tests/test_assemble.py stays GREEN under this mutation -- that is the finding.
check "T4 stage_root is resolved before anything execs out of it" src/porter/assemble.py \
  's = s.replace("Path(stage_root).resolve()", "Path(stage_root)")' tests/test_cli.py
# porter invents the stage, so porter removes it. Removed, a 97 MB tree survives
# every run: the command succeeds exactly once and then reports a refusal about
# a directory the user never created, and a failure reports a different error
# the second time than the first.
check "T4 the CLI removes the stage it created, pass or fail" src/porter/cli.py \
  's = s.replace("if ours:", "if False:")' tests/test_cli.py
# microcli registers an optional with help=argparse.SUPPRESS when its
# Annotated[...] help is empty. Emptied, --out and --stage vanish from --help
# entirely while still working: functional, undiscoverable.
check "T4 --out and --stage are documented where argparse can see it" src/porter/cli.py \
  's = s.replace("directory the .deb is written to", "")' tests/test_cli.py

echo "════ Task 4 fix 1 — the porter.spec seam ════"
# spec.py re-exports rather than redefines, so Task 7 adds a validator instead
# of giving the gallery a second schema. Mutated into its own definitions --
# exactly what "Task 7 duplicates" looks like -- the identity assertion is the
# only thing anywhere that notices.
check "T4 porter.spec re-exports porter.types, never redefines it" src/porter/spec.py \
  's = s.replace("from porter.types import BakeArtifact, Component, Python", "class BakeArtifact: pass\nclass Component: pass\nclass Python: pass")' \
  tests/test_types.py 6

echo "════ Task 9 ════"
# Task 9 -- bake. Entries for scripts/reverify-guards.sh; the controller merges
# them in. Same `check <label> <file> <python-replace-expr> <scope>` form as the
# 28 already there, and every scope is tests/test_bake.py.
echo "════ Task 9 — the bake stage ════"
# THE guard this task exists for. beaver runs SQLite in WAL mode, so rows
# committed without a checkpoint live in <db>-wal while <db> stays a valid,
# readable, STALE database -- and the build host reads through the WAL sitting
# beside it. Measured: 500 of 1000 rows lost, from a 49 KB file that passes
# every existence and magnitude check in this repo.
#
# TRAP 4 APPLIES, and it is worth reading before trusting the PASS below. The
# WAL-MODE guard (next entry) also refuses this fixture -- a database with a
# non-empty WAL is necessarily in WAL mode -- so removing this branch alone does
# NOT let a stale corpus ship. What it loses is the diagnosis. The test therefore
# matches "uncheckpointed", a word only this refusal uses, so the mutation
# produces the wrong error rather than no error. Removing BOTH is what
# reproduces the rc=0 ship, and that is the entry after next.
check "T9 rows stranded in an uncheckpointed WAL are refused (MESSAGE ONLY -- see Trap 4)" \
  src/porter/bake.py \
  's = s.replace("if not wal.exists() or wal.stat().st_size == 0:", "if True:")' \
  tests/test_bake.py
# The second failure, found while writing this task's example and absent from
# its brief. Checkpointing is necessary and NOT sufficient: a reader of a
# WAL-mode database creates <db>-shm beside it, /usr/share/<pkg>/ is root-owned,
# and the service runs as a non-root user -- so the first query dies with
# "attempt to write a readonly database" on the client and nowhere else.
# Removed, examples/baked-data builds, lints and installs at rc=0 with a payload
# no client can open.
check "T9 a database still in WAL mode is refused" src/porter/bake.py \
  's = s.replace("if header[_WRITE_VERSION_OFFSET] != _WAL_FORMAT:", "if True:")' \
  tests/test_bake.py
# Both together: the honest measurement of what the pair is worth. With neither,
# the stranding fixture bakes successfully and its half-database is the payload.
check "T9 BOTH sqlite guards removed — the stale corpus ships at rc=0" src/porter/bake.py \
  's = s.replace("if not wal.exists() or wal.stat().st_size == 0:", "if True:").replace("if header[_WRITE_VERSION_OFFSET] != _WAL_FORMAT:", "if True:")' \
  tests/test_bake.py 6
# The magnitude check, which is the difference between "the file is there" and
# "the file is real". Removed, a bake whose step created an empty SQLite
# database -- one that exists, opens and answers queries with nothing -- returns
# successfully and that file becomes the package's payload.
check "T9 an artifact below its declared min_bytes is refused" src/porter/bake.py \
  's = s.replace("if size < artifact.min_bytes:", "if False:")' tests/test_bake.py
# min_bytes: 0 is how a magnitude check is written back into an existence check
# without anyone noticing in review. Removed, the zero is accepted and every
# file on earth clears it.
check "T9 a declared minimum of zero is refused" src/porter/bake.py \
  's = s.replace("if artifact.min_bytes <= 0:", "if False:")' tests/test_bake.py
# A bake whose entire output is an rc. An ETL that meets a missing input, writes
# an empty table and exits 0 is the ordinary case; with no artifacts declared,
# porter reports a clean bake and packages it.
check "T9 steps that declare no artifacts are refused" src/porter/bake.py \
  's = s.replace("if component.bake_steps and not component.bake_artifacts:", "if False:")' \
  tests/test_bake.py
# The rc, read directly. Removed, a failing ETL step is a successful bake and
# the rest of the package is built on top of last week's data.
check "T9 a non-zero step rc aborts the bake" src/porter/bake.py \
  's = s.replace("if proc.returncode != 0:", "if False:")' tests/test_bake.py
# pipefail. `build_corpus.py | tee build.log` reports tee's success under a
# plain shell, so without this the step above is a gate that cannot fail.
# Isolated from the rc entry: the bare `exit 3` step stays red either way, and
# only the piped test distinguishes them.
check "T9 a step's failure cannot be laundered through a pipe" src/porter/bake.py \
  's = s.replace("\"-o\", \"pipefail\", ", "")' tests/test_bake.py
# Not a refusal but the same failure class: the payload silently absent. Removed,
# examples/baked-data builds a .deb at rc=0 with no corpus in it -- the service
# installs, starts, and 500s on its first request.
check "T9 data: entries are staged into /usr/share/<pkg>" src/porter/assemble.py \
  's = s.replace("for entry in component.data_paths:", "for entry in []:")' \
  tests/test_bake.py
# The provenance seam. Removed, every package ships without a VERSION and a
# client fault report can name the package version and nothing else -- two
# builds of 1.0 from different corpora being the case it exists for.
check "T9 the CLI carries bake's stamp into the package" src/porter/cli.py \
  's = s.replace("stamp=baked.stamp", "stamp=None")' tests/test_bake.py
# An artifact that does not exist at all. INCIDENTAL, and labelled so: without
# the branch, _size() raises FileNotFoundError a few lines later rather than
# porter returning a wrong artefact. The guard buys the sentence that says which
# declared artifact is missing.
check "T9 a missing artifact is refused (incidental -- FileNotFoundError without it)" \
  src/porter/bake.py \
  's = s.replace("if not path.exists():", "if False:")' tests/test_bake.py
# Also INCIDENTAL: without it, `size < None` raises TypeError. Same trade --
# a legible refusal in place of a stack trace naming neither the artifact nor
# the manifest key.
check "T9 a missing min_bytes is refused (incidental -- TypeError without it)" \
  src/porter/bake.py \
  's = s.replace("if artifact.min_bytes is None:", "if False:")' tests/test_bake.py

echo "════ Task 10 ════"
# Task 10 -- migrations and <pkg>-setup. Entries for scripts/reverify-guards.sh.
#
# Written here rather than into the script itself because three implementers
# shared the checkout on 2026-08-08 and reverify-guards.sh is one file. The
# controller merges these in; the `check "label" <file> '<expr>' <scope>` form
# is identical to the 28 already there.
#
# Method (know-how/mutation-testing-a-guard.md): mutate the USE SITE -> purge
# caches -> run -> restore -> purge -> run again. Every entry below was executed
# that way in a detached worktree at 0e8dbd3; results are in task-10-report.md.
echo "════ Task 10 — migrations ════"
# THE guard. Removed, a migration runs on a fresh install -- ainbox's `ALWAYS,
# not just on update`, reproduced -- and migrate_v2 dies on a state file that
# does not exist yet, so dpkg leaves the package half-configured on every first
# install. The e2e's own positive control is what makes the green case mean
# anything: it runs the same migration by hand afterwards and it fails.
check "T10 a fresh install runs no migration (\$2 is empty)" src/porter/migrate.py \
  's = s.replace("if [ -n \"$2\" ]; then", "if true; then")' tests/test_migrate_e2e.py
# The shell orders 1.9 AFTER 1.10 -- '"'"'9'"'"' sorts after '"'"'1'"'"'. With the comparison done
# by the shell, the one upgrade that needs the migration skips it and exits 0:
# the client runs 1.10's payload against a schema-1 state file. dpkg's ordering
# is not a nicety here, it is the difference between running and not running.
check "T10 versions are compared by dpkg, never by the shell" src/porter/migrate.py \
  's = s.replace("dpkg --compare-versions \"$2\" lt \"", "[ \"$2\" \\< \""); s = s.replace("{m.before_version}\"; then", "{m.before_version}\" ]; then")' \
  tests/test_migrate_e2e.py
# No `|| true` anywhere in the block. Added, a migration that fails leaves the
# package `install ok installed` with half-migrated state -- the failure mode
# the whole task exists to close, and the one nobody notices for weeks.
check "T10 a failing migration fails the postinst" src/porter/migrate.py \
  's = s.replace("      )\n    fi", "      ) || true\n    fi")' tests/test_migrate_e2e.py
# The subshell. Without it a migration ending in `exit 0` -- a reasonable last
# line for a script someone pasted in -- ends the POSTINST there, before
# `systemctl enable`. Install exits 0; the service is gone after the next reboot.
check "T10 a migration's own exit does not end the postinst" src/porter/migrate.py \
  's = s.replace("      (\n{m.script}\n      )", "{m.script}")' tests/test_migrate.py
# Refusal only, and honestly labelled: no test installs a package built from a
# malformed before_version, so what goes red is the refusal test and not a
# client symptom. It is still the only place the mistake is visible -- at run
# time `dpkg --compare-versions` exits 1 for "false" and 1 for "malformed".
check "T10 a before_version dpkg cannot parse is refused at build time (REFUSAL ONLY)" \
  src/porter/migrate.py \
  's = s.replace("if probe.returncode != 0:", "if False:")' tests/test_migrate.py
# The wiring. assemble is the only path from a manifest'"'"'s `migrations:` to a
# client, so an emitter that drops them is a package that builds, lints,
# installs and upgrades at rc=0 having migrated nothing.
check "T10 manifest migrations reach the emitted postinst" src/porter/assemble.py \
  's = s.replace("migrations=component.migrations", "migrations=()")' \
  tests/test_migrate_e2e.py
# The command kind emits no postinst at all, so a migration declared on one is
# read and dropped. Refusal only -- the symptom it prevents has no example.
check "T10 a command declaring migrations is refused (REFUSAL ONLY)" src/porter/assemble.py \
  's = s.replace("if component.migrations:", "if False:")' tests/test_migrate.py
echo "════ Task 10 — <pkg>-setup, and the postinst that must not prompt ════"
# Does the setsid harness detect a postinst that asks a question? A `read` is
# added after the configure block, so the emitted postinst tries to read from a
# closed stdin with no controlling terminal. If the install still exits 0, the
# non-interactivity test proves nothing and every claim about rule 5 rests on a
# harness that cannot fail.
check "T10 the setsid harness detects a postinst that asks a question (HARNESS)" \
  src/porter/config.py \
  's = s.replace("fi\nexit 0", "fi\nread ans\nexit 0")' tests/test_migrate_e2e.py
# Staged at all. Removed, /usr/bin/<pkg>-setup is simply not in the package: the
# operator is told to run a command that does not exist, on a client with no
# other documentation.
check "T10 <pkg>-setup is staged for a component with admin keys" src/porter/assemble.py \
  's = s.replace("if component.admin_keys:", "if False:")' tests/test_migrate_e2e.py
# The hint, tied to the same condition. Removed, a fresh install says nothing
# about the wizard and an airgapped operator has nowhere to learn it exists.
check "T10 a fresh install points at the wizard" src/porter/assemble.py \
  's = s.replace("has_setup=bool(component.admin_keys)", "has_setup=False")' \
  tests/test_migrate_e2e.py
# /etc/<pkg>/env is the ADMIN'"'"'S file. Removed, the wizard rewrites it from its own
# idea of the key set and every key the admin added is gone -- silently, at
# rc=0, in the one file on the client nobody has a copy of.
check "T10 the wizard carries through keys it does not manage" src/porter/config.py \
  's = s.replace("grep -vE \x27^({managed})=\x27 \"$ENVFILE\" > \"$tmp\" || [ $? = 1 ]", ": > \"$tmp\"")' \
  tests/test_migrate_e2e.py
# The mode the file ends up with, asserted on disk. `chmod 600` in a generated
# script says nothing about what mv leaves behind.
check "T10 the wizard leaves /etc/<pkg>/env at 600" src/porter/config.py \
  's = s.replace("chmod 600 \"$tmp\"", "chmod 644 \"$tmp\"")' tests/test_migrate_e2e.py
# Re-runnable. Removed, pressing Enter through a second run BLANKS every value
# the operator set the first time -- and the wizard is exactly what they reach
# for when something is already wrong.
check "T10 an empty answer keeps the current value" src/porter/config.py \
  's = s.replace("[ -n \"$new\" ] || new=$cur", ":")' tests/test_migrate_e2e.py

echo "════ Task 11 ════"
# Task 11 -- multi-service ordering, and `oneshot`.
#
# The Task 4 entry "oneshot is refused by name" was retired as part of this
# task; see the note where it used to be, under Task 4 above. Its companion
# test in tests/test_assemble.py went with it.
echo "════ Task 11 — ordering: the directives ════"
# Removed, a multi-component project builds at rc=0 with every unit carrying
# `After=network.target` and nothing else: the graph is read from the manifest,
# resolved, translated into unit names, and then not written. On the client the
# three services start simultaneously and the dependent loses the race.
check "T11 a resolved dependency reaches the emitted unit" src/porter/systemd.py \
  's = s.replace("    if depends_on:", "    if False:")' tests/test_ordering.py
# The other half of the same fact, one module along: `assemble` resolves the
# ordering and hands it to `unit()`. Dropped here, `unit()` is perfect and the
# staged file has no dependencies -- and every test that calls `unit()` directly
# stays green, which is why the round-trip test asserts on the staged file.
check "T11 assemble passes the ordering it resolved to the emitter" \
  src/porter/assemble.py \
  's = s.replace("depends_on=depends_on", "depends_on=()")' tests/test_ordering.py
echo "════ Task 11 — ordering: the refusals ════"
# TWO replacements, because the honest wrong version of this code is not "no
# check" (that raises KeyError, an incidental failure) but "be tolerant of a
# name we do not know" -- which is what a defensive author writes. Mutated that
# way, `after: [alpah]` builds at rc=0 with the dependency silently dropped.
check "T11 an after: naming an undeclared component is refused" src/porter/systemd.py \
  's = s.replace("            if dep not in by_name:", "            if False:").replace("for d in c.after]", "for d in c.after if d in by_name]")' \
  tests/test_ordering.py 6
# Removed, a cycle is emitted. systemd loads it, deletes one of the jobs to
# break the cycle, and boots -- so the order is systemd's choice, it differs
# from what the manifest says, and nothing anywhere reports a problem.
check "T11 an ordering cycle is refused" src/porter/systemd.py \
  's = s.replace("            if colour[dep] == GREY:", "            if False:")' \
  tests/test_ordering.py
# Removed, two components sharing a name resolve to whichever came last in the
# file: `after: [alpha]` then points at a package chosen by file order.
check "T11 two components with one name are refused" src/porter/systemd.py \
  's = s.replace("    if duplicates:", "    if False:")' tests/test_ordering.py
# Removed, `after:` on a command is read and dropped -- there is no unit to
# order, so the manifest says one thing and the package does another, at rc=0.
check "T11 a command may not declare after:" src/porter/assemble.py \
  's = s.replace("        if component.after:", "        if False:")' \
  tests/test_ordering.py
echo "════ Task 11 — the CLI's multi-component loop ════"
# Removed, `porter build` on a three-component manifest emits ONE .deb and exits
# 0 reporting it. The two missing packages are missing everywhere quietly: the
# manifest still declares them, the ordering in the one built package still
# names them, and the client installs a gateway whose dependencies do not exist.
check "T11 porter build emits one package per component" src/porter/cli.py \
  's = s.replace("for c, py in pairs]", "for c, py in pairs[:1]]")' tests/test_ordering.py
echo "════ Task 11 — oneshot: the three things that made it a refusal ════"
# Each of these is one third of the reason Task 4 refused `kind: oneshot`
# outright, and each fails silently on its own.
#
# 1. No Type=oneshot: systemd treats the fork as success, so the job's exit
#    code is nobody's business and a failed nightly run reports nothing.
check "T11 a oneshot gets Type=oneshot" src/porter/systemd.py \
  's = s.replace("        type_line = \"Type=oneshot\\n\"", "        type_line = \"\"")' \
  tests/test_oneshot.py
# 2. [Install] Also=<pkg>.timer, and no WantedBy=. This mutation IS the Task 4
#    bug, restored exactly: the postinst enables <pkg>.service, systemd links it
#    into multi-user.target, and the "scheduled job" runs at every boot.
check "T11 a oneshot's [Install] enables its timer, not itself" src/porter/systemd.py \
  's = s.replace("        install = f\"Also={pkg}.timer\"", "        install = \"WantedBy=multi-user.target\"")' \
  tests/test_oneshot.py
# 3. No Restart= on a job. Restored, a job that fails at 03:00 is retried every
#    three seconds until tomorrow -- against whatever made it fail.
check "T11 a oneshot is not restarted (the timer is the retry)" src/porter/systemd.py \
  's = s.replace("        restart = \"\"", "        restart = \"Restart=on-failure\\nRestartSec=3\\n\"")' \
  tests/test_oneshot.py
# And the .timer itself. Removed, the manifest's `schedule:` is read and
# dropped: the package installs at rc=0, the service is `indirect` (enabled via
# an Also= pointing at a unit that is not in the package), and nothing fires.
check "T11 a oneshot ships a .timer" src/porter/assemble.py \
  's = s.replace("        if component.kind == \"oneshot\":", "        if False:")' \
  tests/test_oneshot.py
echo "════ Task 11 — oneshot: schedules that never fire ════"
# Removed, a oneshot with no `schedule:` emits `OnCalendar=` with an empty
# value. The unit loads, `systemctl enable` exits 0, the symlink is created,
# and the job never runs.
check "T11 a oneshot with no schedule is refused" src/porter/systemd.py \
  's = s.replace("    if not schedule:", "    if False:")' tests/test_oneshot.py
# Measured 2026-08-08: `OnCalendar=every tuesday-ish` is accepted all the way
# through `systemctl enable` (rc=0, symlink created) and rejected only when
# systemd loads the unit -- in the journal, on the client. Removed, a typo in
# the manifest ships.
check "T11 a schedule systemd cannot parse is refused" src/porter/systemd.py \
  's = s.replace("    if probe.returncode != 0:", "    if False:")' tests/test_oneshot.py
# Removed, a kind that emits no timer has its `schedule:` silently dropped: only
# the oneshot branch writes one, so a `service` runs continuously and a
# `command` does not run at all, while the manifest goes on claiming a calendar.
# The `command` half was ACCEPTED until the Task 11 review found it; the
# predicate reads `!= "oneshot"` now and covers both, which is why this entry's
# pattern changed with it -- a stale pattern SKIPs, and a SKIP scores as a
# failure that looks nothing like the quoting bug it usually is.
check "T11 a kind that emits no timer may not declare a schedule" src/porter/assemble.py \
  's = s.replace("    if component.kind != \"oneshot\" and component.schedule:", "    if False:")' \
  tests/test_oneshot.py

echo "════ Task 9-11 review — the fix round ════"
# THE finding of the review: `enable` LINKS a unit and starts nothing, and
# `try-restart` is a no-op on one that is not running -- which is the fresh
# install. So `dpkg -i` of a nightly job left the timer enabled, the package
# `install ok installed`, and NOTHING counting down until the next reboot.
#
# Scoped to tests/test_config.py and not to the nspawn test on purpose. `run()`
# above does not arm PORTER_REQUIRE_NSPAWN (consistent with ci.yml), so
# tests/test_oneshot.py's booted assertion would SKIP on a CI runner and this
# entry would report FAIL on healthy code. The container test is what proves it
# on a real client; this is what keeps the gate honest without one.
check "REV a fresh install arms the unit (enable starts nothing)" src/porter/config.py \
  's = s.replace("    if [ -z \"$2\" ]; then\n      {arm}\n    fi\n", "")' \
  tests/test_config.py 4
# Which unit gets armed is the component's kind, and assemble is the only thing
# that knows it. Mutated to a fixed "service", a job's postinst starts
# demo-job.SERVICE: the job runs once at install time, at whatever hour the
# operator was there, and the timer is still not counting down.
check "REV assemble tells the postinst which unit to arm" src/porter/assemble.py \
  's = s.replace("has_setup=bool(component.admin_keys), kind=component.kind)", "has_setup=bool(component.admin_keys))")' \
  tests/test_oneshot.py
# A migration runs as root; the service does not. `tmp.replace(state_path)` hands
# the client's state file to root:root and the static system user of rule 8 can
# no longer write it -- upgrade at rc=0, `install ok installed`, service broken
# on next start. Removed, the e2e reads STATE=root:root and WRITE_RC=2.
check "REV a migration leaves client state owned by the service user" \
  src/porter/migrate.py \
  's = s.replace("    if [ -d /var/lib/{pkg} ]; then\n      chown -R {pkg}:{pkg} /var/lib/{pkg}\n    fi\n", "")' \
  tests/test_migrate_e2e.py 4
# Nothing ran `sh -n` on anything until this. `migrations: script:` is spliced
# into the postinst verbatim, so one missing quote in porter.yaml built a .deb
# at rc=0 (measured: 20,672 bytes, dpkg-deb --info shows the unparseable
# postinst) that dies on the client. Covers /usr/bin/<pkg>-setup by the same
# branch -- porter writes every file it puts there.
check "REV every generated shell script must parse (sh -n)" src/porter/deb.py \
  's = s.replace("        if probe.returncode != 0:", "        if False:")' \
  tests/test_deb.py
# `sh -n` cannot see this one: `MY-KEY_value=$new` PARSES (rc=0, measured) and
# is a command named MY-KEY_value, which fails at run time on the client with
# rc=127. `A.B` also reaches `grep -vE '^(A.B)='`, where the dot matches any
# character and the wizard drops lines of the admin's file nobody named.
check "REV an admin_key that is not a shell identifier is refused" \
  src/porter/assemble.py \
  's = s.replace("        if not SHELL_IDENTIFIER.match(key):", "        if False:")' \
  tests/test_migrate.py
# assemble stages data: into /usr/share/<pkg>/ and THEN writes VERSION and
# env.example there. Removed, a corpus named VERSION is staged and silently
# replaced by the provenance stamp: rc=0, the path is still in --contents, the
# bytes are porter's.
check "REV a data: entry may not collide with a file porter writes" \
  src/porter/assemble.py \
  's = s.replace("        if Path(entry).name in RESERVED_SHARE_NAMES:", "        if False:")' \
  tests/test_bake.py
# TRAP 4 APPLIES. `timer()` refuses this too, so removing this branch does not
# let a scheduleless job ship -- what it loses is WHERE the refusal happens.
# assemble reaches timer() after vendor() has materialised ~97 MB and after
# <pkg>.service is on disk, so the test's own name ("before anything is staged")
# was false until this existed. It registers because that test now asserts the
# stage does not exist.
check "REV a oneshot with no schedule is refused BEFORE staging (TIMING -- see Trap 4)" \
  src/porter/assemble.py \
  's = s.replace("    if component.kind == \"oneshot\" and not component.schedule:", "    if False:")' \
  tests/test_oneshot.py
echo
echo "════ control: suite green again after every restore ════"
purge; final=$(run); echo "  restored rc=$final"
[ "$final" -eq 0 ] || { echo "  FAIL — harness left dirty"; fail=1; }
echo
echo "════ RESULT: $([ $fail -eq 0 ] && echo 'all guards still bite' || echo 'PROBLEMS FOUND') ════"
exit $fail