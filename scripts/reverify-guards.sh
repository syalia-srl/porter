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
# Anchored on `_build_one`'"'"'s trailing `return deb`: `if ours:` is the same
# idiom in three functions now (component, metapackage, desktop launcher), and an
# unanchored pattern rewrites all three -- Trap 6, a verdict about something
# other than the guard named.
check "T4 the CLI removes the stage it created, pass or fail" src/porter/cli.py \
  's = s.replace("        if ours:\n            shutil.rmtree(stage_dir, ignore_errors=True)\n    return deb", "        if False:\n            shutil.rmtree(stage_dir, ignore_errors=True)\n    return deb")' \
  tests/test_cli.py
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
  's = s.replace("for c, py in parsed.components]", "for c, py in parsed.components[:1]]")' \
  tests/test_ordering.py
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

echo "════ Task 5 — the flat apt repo, the USB tree and the upgrade path ════"
# Several of these mutate a line of INSTALL_SH rather than a branch. That is not
# Trap 2: INSTALL_SH is not a constant the code reads and re-derives, it IS the
# artefact shipped to the client, and changing a line of it changes what runs
# there. Each entry is scoped to tests/test_repo.py.
check "T5 an empty repo directory is refused BEFORE anything is written" \
  src/porter/repo.py \
  's = s.replace("    if not debs:", "    if False:")' tests/test_repo.py
# Unchecked, a failure from dpkg-deb contributes an EMPTY stanza: the index
# still writes at rc=0, carrying Filename/Size/SHA256 for a package with no name.
check "T5 the rc of dpkg-deb --field is read directly" src/porter/repo.py \
  's = s.replace("        if proc.returncode != 0:\n            raise RuntimeError(\n                f\"dpkg-deb", "        if False:\n            raise RuntimeError(\n                f\"dpkg-deb")' \
  tests/test_repo.py
# TRAP 4 APPLIES, and what this buys is TIMING plus a legible message. Every
# name it refuses is also absent from the index, so the membership check below
# still catches it -- after the .debs are copied and the index written, which
# for a real payload is gigabytes, and reporting "not in the index" for what is
# actually a malformed name. The test asserts nothing was created.
check "T5 an app name that is not a Debian package name is refused (TIMING -- see Trap 4)" \
  src/porter/repo.py \
  's = s.replace("    if not PACKAGE_NAME.match(app):", "    if False:")' \
  tests/test_repo.py
# install.sh names exactly ONE package. Built for an app whose .deb is not on
# the stick, the tree copies fine, boots fine, and tells the client `Unable to
# locate package <app>` -- on a machine with no network to fix it from.
check "T5 the app must appear in the index of its own USB tree" src/porter/repo.py \
  's = s.replace("    if not re.search(rf\"^Package:", "    if False and re.search(rf\"^Package:")' \
  tests/test_repo.py
# deb.py's lesson, one module over. No *input* can break the shipped template
# (the app name is validated first), so the test injects a broken INSTALL_SH by
# monkeypatch -- without that this branch could never fire and would be worth
# less than nothing. Mutating it here therefore also checks that test.
check "T5 the generated install.sh must parse" src/porter/repo.py \
  's = s.replace("\n    if proc.returncode != 0:\n        raise RuntimeError(f\"generated", "\n    if False:\n        raise RuntimeError(f\"generated")' \
  tests/test_repo.py
# Our sources entry names a path on the USB stick. Left behind, every later
# `apt-get update` on that client exits 100 on a repository that was unplugged
# weeks ago -- porter breaking apt for good, on a machine nobody can ssh into.
check "T5 the apt sources entry is removed on the way out" src/porter/repo.py \
  's = s.replace("rm -f \"$LIST\"", "true")' tests/test_repo.py
# The re-exec must not eat the caller's argv. Parsing the flags before lifting
# to root -- the obvious order -- consumes them all, so the deployer who asked
# for `--version 1.0` silently gets 2.0, at rc=0.
check "T5 the sudo re-exec keeps the caller's argv" src/porter/repo.py \
  's = s.replace("exec sudo -n -E bash \"$0\" \"$@\"", "exec sudo -n -E bash \"$0\"")' \
  tests/test_repo.py
# A client's stale `file:` source (a previous stick, long gone) makes an
# unscoped `apt-get update` exit 100, taking the offline install down with it.
# An unreachable *mirror* does not: apt prints a warning and exits 0 (measured),
# which is why the test's control uses the file: form.
check "T5 apt-get update is scoped to our own list file" src/porter/repo.py \
  's = s.replace("Dir::Etc::sourceparts=\"-\"", "Dir::Etc::sourceparts=\"sources.list.d\"")' \
  tests/test_repo.py

echo
echo "════ Task 7 — the manifest validator, and the metapackage ════"
# `_refuse_unknown_keys` is ONE guard with FIVE call sites, so it gets six
# entries: one for the helper and one per site. Mutating the helper alone would
# leave the wiring unverified -- a deleted call for `exec:` is invisible to a
# mutation of the branch inside it -- and mutating only the sites would leave
# the branch unverified. Each site is disabled by widening its allowlist to
# whatever the manifest happens to carry, which is a real behaviour change and
# not Trap 2's `() or (...)`.
check "T7 unknown keys are refused at all (the helper)" src/porter/spec.py \
  's = s.replace("    if unknown:", "    if False:")' tests/test_spec.py
# `metapackage:` for `metapackages:` -- every component builds, no role does,
# and the sysadmin's one-name runbook names a package not on the USB.
check "T7 an unknown TOP-LEVEL key is refused" src/porter/spec.py \
  's = s.replace("_refuse_unknown_keys(str(path), doc, TOP_LEVEL_KEYS)", "_refuse_unknown_keys(str(path), doc, TOP_LEVEL_KEYS | frozenset(doc))")' \
  tests/test_spec.py
# The expensive one: `admin_key:` leaves the key in `env:`, so split() files the
# client's secret into /etc/<pkg>/defaults -- package-owned, a conffile, shipped
# inside the .deb and replaced on every upgrade.
check "T7 an unknown key in a COMPONENT entry is refused" src/porter/spec.py \
  's = s.replace("_refuse_unknown_keys(label, entry, COMPONENT_KEYS)", "_refuse_unknown_keys(label, entry, COMPONENT_KEYS | frozenset(entry))")' \
  tests/test_spec.py
# `verson:` silently falls back to the default interpreter, so the .deb ships a
# Python one release below what the payload was written against.
check "T7 an unknown key in the python: block is refused" src/porter/spec.py \
  's = s.replace("entry.get(\"python\", {}), PYTHON_KEYS)", "entry.get(\"python\", {}), PYTHON_KEYS | frozenset(entry.get(\"python\", {})))")' \
  tests/test_spec.py
# `arg:` drops the module'"'"'s own arguments: `-m uvicorn app:app --port ...`
# becomes a bare `-m uvicorn`, which exits on its usage message and restarts.
check "T7 an unknown key in the exec: block is refused" src/porter/spec.py \
  's = s.replace("entry.get(\"exec\", {}), EXEC_KEYS)", "entry.get(\"exec\", {}), EXEC_KEYS | frozenset(entry.get(\"exec\", {})))")' \
  tests/test_spec.py
# `depends_on:` for `depends:` builds a role with an EMPTY Depends: apt installs
# it, reports success, and delivers not one component.
check "T7 an unknown key in a METAPACKAGE is refused" src/porter/spec.py \
  's = s.replace("_refuse_unknown_keys(label, entry, METAPACKAGE_KEYS)", "_refuse_unknown_keys(label, entry, METAPACKAGE_KEYS | frozenset(entry))")' \
  tests/test_spec.py
# INCIDENTAL, and labelled so. Without these the manifest still fails -- as a
# bare KeyError from `from_manifest` whose entire message is the word the
# adopter did not type, with no path and no component name. The guard buys a
# legible refusal, not a caught silent success.
check "T7 a component missing a required key (incidental -- KeyError without it)" \
  src/porter/spec.py \
  's = s.replace("_refuse_missing_keys(label, entry, REQUIRED_COMPONENT_KEYS)", "_refuse_missing_keys(label, entry, ())")' \
  tests/test_spec.py
check "T7 a metapackage missing a required key (incidental -- KeyError without it)" \
  src/porter/spec.py \
  's = s.replace("_refuse_missing_keys(label, entry, REQUIRED_METAPACKAGE_KEYS)", "_refuse_missing_keys(label, entry, ())")' \
  tests/test_spec.py
# The headline refusal. dpkg-deb resolves nothing, so a one-character typo in
# `depends:` builds at rc=0 and the USB looks complete; `apt install <role>`
# then reports unmet dependencies on the client, with no network to fix it.
check "T7 a role naming a package the manifest does not build is refused" \
  src/porter/spec.py \
  's = s.replace("            if name not in built:", "            if False:")' \
  tests/test_spec.py
check "T7 a role that depends on nothing is refused" src/porter/spec.py \
  's = s.replace("        if not meta.depends:", "        if False:")' \
  tests/test_spec.py
# build_deb names the artefact `<package>_<version>_<arch>.deb`, so two packages
# with one name are one file: the second overwrites the first, both at rc=0, and
# the USB carries three packages where the manifest declared four.
check "T7 two packages sharing one name are refused" src/porter/spec.py \
  's = s.replace("    if duplicates:", "    if False:")' tests/test_spec.py
# The metapackage's stage. build_deb'"'"'s lint refuses top-level paths porter does
# not own, and `usr` is one porter DOES own -- so a leftover component tree here
# is packaged into the role at rc=0, turning a 20 KB metapackage into a 91 MB
# one that owns another package'"'"'s /usr/lib and conflicts with it on the client.
check "T7 a metapackage refuses a non-empty stage" src/porter/cli.py \
  's = s.replace("    if stage_dir.exists() and any(stage_dir.iterdir()):", "    if False:")' \
  tests/test_examples.py

echo "════ Task 8 — derived Depends and the desktop split ════"
# Rule 11. Every refusal in depends.py exists because the obvious implementation
# returns a SHORTER LIST instead: the build is green, the lint passes, dpkg is
# satisfied, and the binary dies at its first exec on a client with no network.
# So each mutation below must reproduce the short list, not merely an exception.
#
# `objdump -p` exits non-zero and prints nothing on an object it cannot parse.
# Restored to `if False`, a truncated or corrupt payload derives NO dependencies
# at all and the package ships with an empty Depends:.
check "T8 an object objdump cannot read is not a dependency-free one" \
  src/porter/depends.py \
  's = s.replace("    if proc.returncode != 0:\n        raise RuntimeError(\n            f\"objdump could not read", "    if False:\n        raise RuntimeError(\n            f\"objdump could not read")' \
  tests/test_depends.py 4
# The silent-drop shape, written out in full: skip what ldconfig cannot resolve
# and return the rest. That is the ORIGINAL bug -- a Depends: short by exactly
# the library the build host does not have, which is the one the client will not
# have either. Two lines because reproducing the symptom needs both halves.
check "T8 a soname the build host cannot resolve is refused" src/porter/depends.py \
  's = s.replace("    if missing:", "    sonames = [so for so in sonames if so in cache]\n    if False:")' \
  tests/test_depends.py 4
# A library hand-installed under /usr/local resolves perfectly here and maps to
# no package at all. Removed, `packages_owning` returns the other entries and
# says nothing about the one apt cannot deliver.
check "T8 a library no package owns is refused" src/porter/depends.py \
  's = s.replace("    if unowned:", "    if False:")' tests/test_depends.py
# The vendored interpreter ships libpython3.12.so.1.0 and links it as
# `$ORIGIN/../lib/libpython3.12.so.1.0` -- a PATH, not a bare soname. Without
# the basename comparison the tree'"'"'s own library is read as a system
# dependency, is unresolvable, and every porter package fails to build.
check "T8 a library the payload ships itself is not a system dependency" \
  src/porter/depends.py \
  's = s.replace("    external = sorted(so for so in needed if PurePosixPath(so).name not in own)", "    external = sorted(needed)")' \
  tests/test_depends.py
# Rule 11 for the core package, and the reason `libcrypt1` is in it: nothing in
# any manifest names it, it arrives through the single dynamically-linked
# extension in the vendored interpreter, and a hand-written list would not have
# it. Scoped to one nodeid -- the fixture builds two real .debs.
check "T8 the core package derives Depends from what it stages" \
  src/porter/assemble.py \
  's = s.replace("    depends = derive_depends(stage)", "    depends = []")' \
  "tests/test_desktop_e2e.py::test_the_core_package_declares_the_dependencies_it_derived"
# Rule 12. A desktop dependency in the core package cannot be satisfied on an
# airgapped headless client -- apt has no network and the GUI libraries are not
# installed -- so the server install fails outright, at the client.
check "T8 a desktop dependency in the core package is refused" src/porter/desktop.py \
  's = s.replace("    if leaked:", "    if False:")' tests/test_desktop.py
# THE launcher bug, and the reason those tests RUN the script instead of
# grepping it. `command -v X && BROWSER=X && break` is an AND-list whose failure
# is the last command of the loop body, so under `set -e` the launcher exits 1
# the moment the first candidate is absent -- every client without Chrome, with
# no window and no message. The broken form contains every substring the working
# one does, so no text assertion can tell them apart.
check "T8 the launcher survives a browser candidate being absent" src/porter/desktop.py \
  's = s.replace("  if command -v \"$candidate\" >/dev/null 2>&1; then\n    BROWSER=\"$candidate\"\n    break\n  fi", "  command -v \"$candidate\" >/dev/null 2>&1 && BROWSER=\"$candidate\" && break")' \
  tests/test_desktop.py 5
# A click right after login arrives while systemd is still starting the unit.
# Without the wait the window opens on connection-refused, which reads as a
# broken install rather than a slow one.
check "T8 the launcher waits for the service before opening" src/porter/desktop.py \
  's = s.replace("  if curl -fsS -o /dev/null \"$HEALTH\"; then\n    break\n  fi", "  break")' \
  tests/test_desktop.py 4
# TRAP 4 APPLIES. The hicolor-size check catches a GIF too -- its bytes 16:24
# are zeros, so it reports 0x0 rather than "not a PNG". What the magic check
# uniquely covers is a real image in another format at a plausible size; the
# entry still registers because the message changes.
check "T8 an icon that is not a PNG is refused (see Trap 4)" src/porter/desktop.py \
  's = s.replace("    if head[:8] != b\"\\x89PNG\\r\\n\\x1a\\n\":", "    if False:")' \
  tests/test_desktop.py
# `desktop.url` and `env.PORT` are one number written twice, and the drift that
# happens is an author bumping one. The launcher then waits its whole timeout on
# a health check nothing answers -- built, linted and installed at rc=0.
check "T8 a desktop URL and the service port must agree" src/porter/desktop.py \
  's = s.replace("    if in_url.isdigit() and in_url != str(declared):", "    if False:")' \
  tests/test_desktop.py
# build_deb'"'"'s lint allows `usr/`, so a core payload left in the desktop stage
# is packaged into the LAUNCHER at rc=0 -- and its libraries land in the desktop
# package'"'"'s derived Depends:, which is rule 12 running backwards.
check "T8 the desktop package refuses a non-empty stage" src/porter/desktop.py \
  's = s.replace("    if stage.exists() and any(stage.iterdir()):", "    if False:")' \
  tests/test_desktop.py
# `browser: bundled` accepted-and-ignored emits a launcher that probes the
# client'"'"'s browser under a manifest saying it pins one.
check "T8 a browser porter cannot ship is refused" src/porter/desktop.py \
  's = s.replace("        if spec.browser != \"system\":", "        if False:")' \
  tests/test_desktop.py
# A space in the name reaches --class= as two argv entries and StartupWMClass=
# as one string, so the window never groups under its own icon -- silently, with
# the app working perfectly.
check "T8 a name that is not an X11 WM_CLASS is refused" src/porter/desktop.py \
  's = s.replace("    if not WM_CLASS.match(name):", "    if False:")' \
  tests/test_desktop.py

echo "════ Task 6 ════"
# Task 6 — the gate. Guard entries for scripts/reverify-guards.sh.
#
# Written to a separate file because a peer is running: concurrent appends to
# reverify-guards.sh lose blocks. The controller merges these in.
#
# Every expression below quotes with Python DOUBLE quotes escaped as \", and
# never with a single quote: `check` passes the expression as a single-quoted
# shell word, so one apostrophe inside it ends the word and the rest of the
# mutation reaches python as shell syntax. (`\x27` does not help -- it is an
# escape only *inside* a Python string literal, and these appear outside one.)
#
# Two kinds of entry live here and the distinction matters.
#
#   1. ASSERTIONS. Disable the check, and one of the mutation bundles in
#      tests/test_gate.py stops being caught. The bundle is the mutant; the
#      entry proves the check is what catches it.
#   2. CONTROLS. A positive control never fires against a healthy bundle, so
#      disabling it turns nothing red -- which would make it look like a guard
#      that does not bite. These entries therefore break the PROBE instead, and
#      assert the control notices. Dropping `--network none` is the gate rule
#      executed on the gate: before asserting isolation, prove the probe detects
#      the thing when isolation is off.
#
# Scope is tests/test_gate.py for every entry (~36 s per run, six tests).
# ---- assertions -------------------------------------------------------------
# The failure une-tools' smoke-update.sh exists to catch. Without this line the
# state-eating bundle installs, upgrades, answers HTTP and passes -- with the
# client's data gone.
check "T6 an upgrade that eats client state is caught" src/porter/gate.py \
  's = s.replace("        r.check(after == want,", "        r.check(True,")' \
  tests/test_gate.py
# The control for the line above, and the reason the seed is hashed on the way
# IN. If seeding silently does nothing, both digests are empty, empty equals
# empty, and the gate reports state perfectly preserved.
check "T6 a seed that never landed is not read as intact state" src/porter/gate.py \
  's = s.replace("        r.check(before == want,", "        r.check(True,")' \
  tests/test_gate.py
# A 40 KB stub in which EVERY path assertion passes. This is the 30,912-byte
# package that was reported as built during the design.
check "T6 a truncated payload is caught by magnitude" src/porter/gate.py \
  's = s.replace("    r.check(payload_prev >= min_payload_kb,", "    r.check(True,")' \
  tests/test_gate.py
# Overlaps the entry above ON THE SAME BUNDLE (trap 4) and asks a different
# question: `du` asks how big the tree is, this asks whether the thing at the
# shipped ExecStart is the interpreter the package carries. A dangling symlink
# to the build host's own python is large AND wrong.
check "T6 a stub interpreter cannot report itself as sys.executable" src/porter/gate.py \
  's = s.replace("    r.check(_marker(out, \"PYEXE\") == interpreter and interpreter != \"\",", "    r.check(True,")' \
  tests/test_gate.py
# `1.9` sorts after `1.10` as a string and before it to dpkg. Lexically, the
# gate installs the NEWER package, "upgrades" to the older one, finds every
# seeded file untouched, and passes for an upgrade that never happened.
check "T6 the upgrade path is ordered by dpkg, not lexically" src/porter/gate.py \
  's = s.replace("    return sorted(set(found), key=functools.cmp_to_key(_dpkg_cmp))", "    return sorted(set(found))")' \
  tests/test_gate.py
# `health_url` is interpolated into the gate's own shell. `...; true` exits 0,
# so the gate would report a healthy service with nothing listening -- a false
# pass manufactured by the gate itself.
check "T6 a health URL that could run shell is refused" src/porter/gate.py \
  's = s.replace("    if not HEALTH_URL.match(health_url):", "    if False:")' \
  tests/test_gate.py
# ---- controls: break the probe, and assert the control notices --------------
# THE GATE RULE, executed on the gate. Remove the airgap and the isolation
# assertions must go red. Without this entry, `IFACES=lo` and `DNS=blocked`
# would be two lines nothing has ever shown capable of failing.
check "T6 CONTROL the isolation probe detects a container with a network" src/porter/gate.py \
  's = s.replace("[\"docker\", \"run\", \"--rm\", \"--network\", \"none\",", "[\"docker\", \"run\", \"--rm\",")' \
  tests/test_gate.py
# The apt-source count taken BEFORE the wipe is what makes "no network source
# remained" mean anything. Point the probe at a path that does not exist and it
# reports a clean client either way -- the blind probe the count exists to
# detect.
check "T6 CONTROL a blind apt-source probe is not read as a clean client" src/porter/gate.py \
  's = s.replace("echo \"NETSRC_BEFORE=$(cat /etc/apt/sources.list ", "echo \"NETSRC_BEFORE=$(cat /nonexistent ")' \
  tests/test_gate.py
# The install is bounded by `timeout`, and a hang is the failure that matters
# most on an airgapped client -- at 3am it is indistinguishable from an install
# still running. A `timeout` that does not fire makes that bound decorative.
check "T6 CONTROL a timeout that does not fire is caught" src/porter/gate.py \
  's = s.replace("timeout 1 sleep 5; echo \"TIMEOUTCTL=$?\"", "timeout 5 sleep 1; echo \"TIMEOUTCTL=$?\"")' \
  tests/test_gate.py
# "The install reached no prompt" is worthless under a harness that cannot
# notice one. Discard the read probe's own result and the harness must declare
# itself blind rather than report a clean install.
check "T6 CONTROL a harness where an interactive read succeeds is refused" src/porter/gate.py \
  's = s.replace("< /dev/null 2>/dev/null && echo \"TTYCTL=blind\"", "; true && echo \"TTYCTL=blind\"")' \
  tests/test_gate.py
# The negative control on the health URL: nothing may answer before the service
# is started. Hand the gate a 0 there and it must refuse to treat the later 0 as
# evidence that the shipped ExecStart did anything.
check "T6 CONTROL a health URL answering before the start is refused" src/porter/gate.py \
  's = s.replace("echo \"PREHEALTH_RC=$?\"", "echo \"PREHEALTH_RC=0\"")' \
  tests/test_gate.py
# Every marker check reads a transcript. Truncate it and they would all be
# reading a log that stopped early, with "the marker is absent" meaning "the
# container died" rather than "the assertion failed".
check "T6 CONTROL a transcript that stops early is not read as a pass" src/porter/gate.py \
  's = s.replace("echo \"DONE=yes\"", "echo \"DONE=no\"")' \
  tests/test_gate.py

echo "════ Task 12 ════"
# Task 12 -- the `build:` escape hatch. Guard entries to be merged into
# scripts/reverify-guards.sh by the controller (this wave's protocol keeps
# several implementers out of that one file).
#
# NOTHING TO DELETE. The existing entry
#
#   check "T4 a non-empty stage root is refused, never emptied" src/porter/assemble.py \
#     's = s.replace("if stage.exists() and any(stage.iterdir()):", "if False:")' tests/test_assemble.py
#
# still matches: that block moved into `assemble._open_an_empty_stage` and is
# now shared by both paths, but its text is unchanged and there is still exactly
# one copy of it. The T12 entry at the end of this file mutates the same line
# with the hatch's own tests as the scope, which is the point of keeping both:
# the mutation has to be red from either side or the helper is only guarded for
# whichever caller happens to have a test.
#
# Every scope below is tests/test_escape_hatch.py, which needs no uv, no docker
# and no interpreter download -- a hook component vendors nothing -- so these
# entries are the cheapest in the registry (~3 s each).
echo "════ Task 12 — the hatch runs, and the assembler does not ════"
# Removed, `build:` is read by the loader, accepted, and then ignored: porter
# assembles the component itself. Today that is loud (a hook component's kind
# defaults to `custom`, which is not in SUPPORTED_KINDS, so the ordinary path
# refuses it by name) -- but the symptom under test is the one that matters and
# is checked directly: the hook's payload is not in the .deb. Trap 3: the
# message this produces is incidental, the red is not.
check "T12 a build: hook takes over the assemble stage" src/porter/assemble.py \
  's = s.replace("    if component.build:", "    if False:")' \
  tests/test_escape_hatch.py
# Removed, a hook that fails AFTER writing part of its tree builds at rc=0 and
# the truncated payload ships. The magnitude check below cannot see it -- the
# stage is full of real bytes -- so this is the one guard with a test written
# specifically to leave it alone in the room
# (`test_a_hook_that_fails_after_writing_a_partial_tree_is_still_refused`).
check "T12 the hook's rc is read, and a partial tree does not ship" \
  src/porter/assemble.py \
  's = s.replace("    if proc.returncode != 0:", "    if False:")' \
  tests/test_escape_hatch.py
# Removed, porter reports its own rc and swallows the script's diagnostic. The
# build still fails, so this is a MESSAGE-ONLY mutation (Trap 4): what goes red
# is the assertion that the hook's own stderr survived into porter's error, and
# that is the whole of the brief's "non-zero rc aborts with its stderr
# surfaced". A hook fails for reasons only the hook knows.
check "T12 the hook's stderr is surfaced (MESSAGE ONLY -- see Trap 4)" \
  src/porter/assemble.py \
  's = s.replace("{proc.stderr.rstrip()}", "")' \
  tests/test_escape_hatch.py
echo "════ Task 12 — the magnitude check: a hook cannot produce nothing ════"
# The whole check, removed at its call site. This is the entry that reproduces
# the original symptom rather than an incidental error: porter packages an
# empty tree into a .deb that installs cleanly at rc=0 and delivers no payload
# at all, on a client with no network to notice from.
check "T12 an empty stage from a hook is refused" src/porter/assemble.py \
  's = s.replace("    _refuse_a_hook_that_produced_nothing(component, stage)", "    pass")' \
  tests/test_escape_hatch.py
# Magnitude, not existence -- the half that carries the standing rule. Removed,
# a stage of files that all exist and none of which has anything in it builds
# at rc=0. That is what a render step which failed halfway leaves behind, and
# it passes every check that asks whether a path is there.
check "T12 a stage of empty files is refused by bytes, not by entries" \
  src/porter/assemble.py \
  's = s.replace("    if total == 0:", "    if False:")' \
  tests/test_escape_hatch.py
# OVERLAPPING GUARD (Trap 4), reported rather than glossed. Removed on its own,
# an empty stage is STILL refused -- by the byte check above, which a stage with
# no files also fails. What goes red is the message: the adopter is told their
# files total zero bytes when there are no files at all. Keep the entry, and do
# not read it as evidence that this branch is load-bearing by itself.
check "T12 a stage with no files at all names that (MESSAGE ONLY -- see Trap 4)" \
  src/porter/assemble.py \
  's = s.replace("    if not files:", "    if False:")' \
  tests/test_escape_hatch.py
echo "════ Task 12 — the guarantees the hatch does NOT bypass ════"
# Removed, a hook's /etc files are not declared to dpkg. Today deb.py's lint
# catches that and refuses the build (Trap 4: the two guards overlap and the
# package does not ship either way), so what this mutation proves is that the
# derivation and the lint agree. They have to: an undeclared conffile is an
# admin's edited config replaced on every upgrade, with no prompt, no
# .dpkg-dist and no record.
check "T12 conffiles are derived from the tree a hook wrote" \
  src/porter/assemble.py \
  's = s.replace("    conffiles = _conffiles(stage)", "    conffiles = []")' \
  tests/test_escape_hatch.py
# Rule 11 through the hatch. Removed, a hook that stages a native binary ships
# a package with no `Depends:` at all: it installs on the build host's twin and
# fails to start on a client whose libc came from anywhere else.
#
# maxlines 4 DELIBERATELY: this line is textually identical to the assembler's
# own, so the mutation disables both call sites. The scope is the hatch's tests,
# which never reach the other one, so the verdict is still about the hook's --
# but it is not a one-guard mutation and Trap 6 says to say so.
check "T12 Depends: is derived from what a hook staged" src/porter/assemble.py \
  's = s.replace("    depends = derive_depends(stage)", "    depends = []")' \
  tests/test_escape_hatch.py 4
# The stage a hook is handed is empty, from the hatch's side. Same line as the
# T4 entry above and deliberately duplicated with a different scope: a shared
# helper guarded only through one caller's tests is a helper that stops being
# guarded the day that caller changes.
check "T12 a hook does not build on top of someone else's tree" \
  src/porter/assemble.py \
  's = s.replace("if stage.exists() and any(stage.iterdir()):", "if False:")' \
  tests/test_escape_hatch.py
echo "════ Task 12 — the FHS lint, reached THROUGH a hook ════"
# These six mutate deb.py, which Task 2 already guards, and they are here
# anyway with a different scope. That is the whole claim of this task: the hatch
# bypasses assembly, NOT the guarantees. A lint entry scoped only to
# tests/test_deb.py proves the lint bites when deb.py is called directly; it says
# nothing about whether a `build:` component reaches it at all. If any of these
# goes green, `build:` is a way around the lint rather than a way around the
# assembler -- and an adopter reaches for it exactly when their shape is unusual,
# which is when the lint matters most.
#
# Verified by hand 2026-08-08 that the pairing is real and not assumed: with the
# top-level allowlist disabled, `porter build` on a hook manifest wrote a .deb
# carrying ./home/apiad/.ssh/id_rsa at rc=0.
check "T12 the top-level allowlist applies to a hook's tree" src/porter/deb.py \
  's = s.replace("if entry.name not in ALLOWED_TOP_LEVEL:", "if False:")' \
  tests/test_escape_hatch.py
check "T12 /etc/<pkg>/env is refused in a hook's tree" src/porter/deb.py \
  's = s.replace("if p.exists() or p.is_symlink():", "if False:")' \
  tests/test_escape_hatch.py
check "T12 /var/lib is refused in a hook's tree" src/porter/deb.py \
  's = s.replace("if p.exists() and any(p.rglob(\"*\")):", "if False:")' \
  tests/test_escape_hatch.py
check "T12 an absolute symlink is refused in a hook's tree" src/porter/deb.py \
  's = s.replace("if target.is_absolute():", "if False:")' \
  tests/test_escape_hatch.py
check "T12 sh -n reads a hook's /usr/bin scripts" src/porter/deb.py \
  's = s.replace("if probe.returncode != 0:", "if False:")' \
  tests/test_escape_hatch.py
# OVERLAPPING (Trap 4), and Task 2 recorded the same overlap: a pre-staged
# DEBIAN/ is also caught by the top-level allowlist, so removing this alone
# still refuses the build. The message is what goes red -- and it is the useful
# half, because it tells a hook author to pass scripts= rather than that
# /DEBIAN is a path porter does not own.
check "T12 a hook may not stage DEBIAN/ (MESSAGE ONLY -- see Trap 4)" \
  src/porter/deb.py \
  's = s.replace("if debian.exists() or debian.is_symlink():", "if False:")' \
  tests/test_escape_hatch.py
echo "════ Task 12 — the keys the hatch makes porter stop reading ════"
# Removed, `source:`/`env:`/`admin_keys:` and the rest are accepted beside a
# `build:` and read by nothing. `admin_keys` is the expensive one: no
# <pkg>-setup is written, /etc/<pkg>/env is never created, and an operator is
# left looking for a wizard the manifest promised. All of them build, lint and
# install at rc=0.
check "T12 assembler keys beside a hook are refused (loader)" src/porter/spec.py \
  's = s.replace("    declared = sorted(set(written) & ASSEMBLER_ONLY_KEYS)", "    declared = []")' \
  tests/test_escape_hatch.py
# The same refusal one layer in, and it is not redundant: most of this suite
# builds a `Component` in Python and never opens a YAML file, so a refusal that
# lived only in the loader is one every in-process caller walks past -- and then
# the behaviour the suite pins is not the behaviour the tool has.
check "T12 assembler keys beside a hook are refused (in-process)" \
  src/porter/assemble.py \
  's = s.replace("    if declared:", "    if False:")' \
  tests/test_escape_hatch.py
# `custom` is what a hook component's absent `kind:` defaults to. Added to
# SUPPORTED_KINDS it stops being refused on the ORDINARY path, so a component
# that reaches the assembler carrying it is staged as whichever branch porter
# guessed -- a payload with neither a unit nor a wrapper.
check "T12 'custom' is not a kind porter assembles" src/porter/assemble.py \
  's = s.replace("SUPPORTED_KINDS = (\"service\", \"command\", \"oneshot\")", "SUPPORTED_KINDS = (\"service\", \"command\", \"oneshot\", \"custom\")")' \
  tests/test_escape_hatch.py
# Removed, `build: bulid.sh` reaches bash, which reports rc=127 about an
# absolute path with no hint of what it was resolved against. OVERLAPPING
# (Trap 4): the build still fails, via the rc check. Message only -- but the
# message is the whole value, since `build:` is relative to the manifest's
# directory and nothing else in porter says so.
check "T12 a missing hook script is named (MESSAGE ONLY -- see Trap 4)" \
  src/porter/assemble.py \
  's = s.replace("    if not script.is_file():", "    if False:")' \
  tests/test_escape_hatch.py
echo "════ Task 13 ════"
# Task 13 -- the shared interpreter package, and bundled native binaries.
# Guard entries to be merged into scripts/reverify-guards.sh by the controller
# (this wave's protocol keeps several implementers out of that one file).
#
# NOTHING TO DELETE, ONE TO REWRITE. The existing entry for Task 4's
# non-bundled refusal
#
#   check "T4 an interpreter porter does not bundle is refused" ...
#
# does not exist in the registry today (Task 4's guards covered the kind,
# stage-root and import-probe refusals), so nothing has to be removed. But
# `tests/test_assemble.py::test_refuses_an_interpreter_it_does_not_bundle` is
# gone: `python.package: <name>` is a real shape now. Its replacement is
# `test_refuses_a_shared_interpreter_nobody_built`, guarded below.
#
# Scopes. Two files, and both are cheap relative to what they cover:
#   tests/test_shared_interpreter.py  ~1 interpreter vendored, session-scoped,
#                                     plus one `porter build` of the example and
#                                     two containers  (~25 s)
#   tests/test_native_binary.py       cc twice per test, one vendored tree per
#                                     assembling test, one container  (~20 s)
# tests/test_assemble.py is used for the one refusal that lives there.
#
# TRAP 3 NOTE, applying to every entry below: porter's characteristic bug is
# silent success, so each mutation is chosen to reproduce THAT and not merely to
# raise somewhere. The two exceptions are called out where they occur.
echo "════ Task 13 — the shared interpreter is a package, and it is depended on ════"
# THE entry. Removed, the component builds and installs perfectly and does not
# ask for the interpreter at all: apt resolves nothing, /usr/lib/<interp>/ is
# absent on the client, and ExecStart names a path that does not exist. The
# offline e2e is what goes red -- `dpkg-query` cannot find the interpreter
# package -- which is the original symptom exactly.
check "T13 a component Depends: on the shared interpreter, by exact version" \
  src/porter/assemble.py \
  's = s.replace("depends.insert(0, interpreter.exact_dependency)", "pass")' \
  tests/test_shared_interpreter.py
# Removed, a component's requirements are installed into the SHARED
# interpreter's site-packages. That BUILDS and RUNS for one component -- which
# is why it needs a guard rather than a test that happens to notice: the failure
# only appears with a second component wanting the same wheel (a dpkg file
# conflict at the client) or with a rebuild that changes them (97 MB churn).
check "T13 requirements go to the payload root, never the shared interpreter" \
  src/porter/assemble.py \
  's = s.replace("install(python_bin, component.requirements, target=libdir)", "install(python_bin, component.requirements)")' \
  tests/test_shared_interpreter.py
# Removed, uv's console scripts ship. Their shebangs are absolute build-host
# paths (rule 3), so /usr/lib/<pkg>/bin/uvicorn is a file that looks runnable on
# the client and is not.
check "T13 uv's console scripts and lock do not ship" src/porter/assemble.py \
  's = s.replace("shutil.rmtree(libdir / \"bin\", ignore_errors=True)", "pass")' \
  tests/test_shared_interpreter.py
# Removed, a caller that declares a shared interpreter and builds none falls
# through to... nothing coherent. Before Task 13 this was refused as "not
# implemented"; the shape is real now and the refusal guards the incoherent half.
check "T13 a shared interpreter nobody built is refused" src/porter/assemble.py \
  's = s.replace("if interpreter is None:", "if False:")' \
  tests/test_assemble.py
# Removed, a component declaring `bundled` and handed a shared interpreter
# bundles anyway: a package that works and is 97 MB larger than the manifest
# asked for, with every sibling depending on a package nothing produced.
check "T13 bundled beside a shared interpreter is refused" src/porter/assemble.py \
  's = s.replace("if python.bundled and interpreter is not None:", "if False:")' \
  tests/test_shared_interpreter.py
# Removed, the component Depends: on one package and ExecStarts the path of
# another -- an install apt resolves happily and a service that cannot start.
check "T13 an interpreter built for another package is refused" src/porter/assemble.py \
  's = s.replace("if interpreter.package != python.package:", "if False:")' \
  tests/test_shared_interpreter.py
check "T13 an interpreter built for another version is refused" src/porter/assemble.py \
  's = s.replace("if interpreter.python_version != python.version:", "if False:")' \
  tests/test_shared_interpreter.py
# Removed, uv is asked for 3.12, answers with whatever it has, and the whole
# project's exact Depends: is built on the wrong number at once.
check "T13 the staged tree really is the declared version" src/porter/assemble.py \
  's = s.replace("if not full.startswith(f\"{declared}.\"):", "if False:")' \
  tests/test_shared_interpreter.py
# Removed, one interpreter package name with two versions builds whichever was
# staged last and the other component's ExecStart names a python that is not in
# it. dpkg-deb resolves nothing, so the build is happy.
check "T13 one interpreter name may not hold two versions" src/porter/spec.py \
  's = s.replace("if seen is not None and seen.version != python.version:", "if False:")' \
  tests/test_shared_interpreter.py
# Removed, the interpreter package and a component write to one .deb filename
# and the second overwrites the first, silently, at rc=0.
check "T13 an interpreter name a component claims is refused" src/porter/spec.py \
  's = s.replace("if python.package in package_names:", "if False:")' \
  tests/test_shared_interpreter.py
echo "════ Task 13 — native binaries, and the Depends: derived from them ════"
# Removed, a native binary linking a soname NOTHING provides builds at rc=0 with
# a Depends: short by exactly the entry that mattered, and dies at its first
# exec on a client with no network to fix it from.
check "T13 a native binary whose libraries do not resolve is refused" \
  src/porter/depends.py \
  's = s.replace("\n        if missing:", "\n        if False:")' \
  tests/test_native_binary.py
# Removed, the payload's OWN libraries reach Depends: -- apt on an airgapped
# client is asked for `libporterprobe.so.1`, a package no mirror has ever
# carried. TRAP 3 APPLIES: on this fixture the mutation goes red as a build-time
# resolve failure rather than as a bad Depends:, because the build host cannot
# resolve the payload's private soname either. The shipped symptom (an
# unsatisfiable Depends:) needs a soname that DOES resolve on the build host and
# not on the client, which no fixture here can stage. The exclusion is real
# either way; the failure mode observed is not the shipped one.
check "T13 sonames the payload ships itself are excluded from Depends:" \
  src/porter/depends.py \
  's = s.replace("external = sorted(so for so in needed if PurePosixPath(so).name not in own)", "external = sorted(needed)")' \
  tests/test_native_binary.py
# Removed, a binary staged 644 installs at rc=0 and fails with "Permission
# denied" at its first exec, file present and exactly the right size.
check "T13 a non-executable native binary is refused" src/porter/assemble.py \
  's = s.replace("if \".so\" not in path.name and not path.stat().st_mode & 0o111:", "if False:")' \
  tests/test_native_binary.py
# Removed, copy2 becomes copyfile and porter itself drops the exec bit -- the
# same client symptom, arriving through porter rather than through the adopter.
check "T13 the exec bit survives staging" src/porter/assemble.py \
  's = s.replace("is the difference between a program and a file.\n        shutil.copy2(src, dest)", "is the difference between a program and a file.\n        shutil.copyfile(src, dest)")' \
  tests/test_native_binary.py
# Removed, a shell script declared as a native binary contributes no NEEDED
# entries -- truthfully -- and everything it really needs goes undeclared. Rule
# 11's failure with every check apparently passing.
check "T13 a native binary that is not an ELF object is refused" \
  src/porter/assemble.py \
  's = s.replace("if fh.read(4) != ELF_MAGIC:", "if False:")' \
  tests/test_native_binary.py
# Removed, `native_binaries: [build/probe]` before the step that compiles it is
# a manifest whose payload does not exist, reported as a FileNotFoundError from
# shutil naming a staging path the adopter has never seen.
check "T13 a native binary that is not there is refused" src/porter/assemble.py \
  's = s.replace("if not path.is_file():", "if False:")' \
  tests/test_native_binary.py
# Removed, four different things write into /usr/lib/<pkg>/ and a basename
# collision between any two of them is silent: copy2 overwrites, and onto a
# DIRECTORY it writes the file inside -- so `source: [python]` lands in the
# interpreter tree and the module is not importable at all.
check "T13 two things staged under one basename are refused" src/porter/assemble.py \
  's = s.replace("if dest.exists():", "if False:")' \
  tests/test_native_binary.py

echo "════ Task 14 ════"
# Task 14 -- the nspawn gate and the signed repo. Guard entries to be merged
# into scripts/reverify-guards.sh by the controller (this wave's protocol keeps
# several implementers out of that one file).
#
# NOTHING TO DELETE. No existing entry mutates src/porter/gate.py's nspawn half
# or src/porter/repo.py's signing half -- neither existed before this task.
#
# TWO SCOPES, and they cost very differently:
#
#   tests/test_signing.py      ~6 s   (tiny .debs, docker, no interpreter)
#   tests/test_nspawn_gate.py  ~180 s (three systemd boots + a 97 MB package)
#
# The nspawn entries are the most expensive in the registry. They are also the
# only ones whose subject is a unit systemd actually started, so they are not
# optional. Budget ~20 minutes for the eight of them.
#
# Every nspawn entry below neuters ONE `r.check(...)` inside `nspawn_gate` by
# replacing its condition with `True`. That is the use site, not a constant
# (trap 2): the check still runs, still formats its message, and simply cannot
# fail -- which is precisely the shape of the bug this registry exists to find.
echo "════ Task 14 — the signed repo ════"
# The whole of property 3. Force the unsigned template and a signed tree's
# install.sh goes back to trusting its own source: apt then accepts a rewritten
# index, and test_a_rewritten_index_..[True] installs the attacker's payload.
check "T14 a signed tree never emits the unverified-source template" \
  src/porter/repo.py \
  's = s.replace("trust = (TRUST_SIGNED if sign_key is not None else TRUST_UNSIGNED).format(", "trust = (TRUST_UNSIGNED).format(")' \
  tests/test_signing.py
# `gpg --export <pattern>` with a pattern that matches nothing exits 0 and
# writes NOTHING. Without the magnitude floor the stick ships a zero-byte
# keyring, usb_tree returns happily, and the install dies at the client with
# `no valid OpenPGP data found` -- on a machine with no network to fix it from.
check "T14 the exported public key must be real, not zero bytes" \
  src/porter/repo.py \
  's = s.replace("if exported.returncode != 0 or len(exported.stdout) < 100:", "if False:")' \
  tests/test_signing.py
# Removed, sign_release ships a signature it never checked against the key it
# ships beside it. test_a_keyring_that_does_not_match_the_signature_is_refused
# calls the verifier directly with a second key's export, which is the only way
# to make this fail -- on the build path the two always match, which is the
# point of the check.
check "T14 the shipped key must verify the shipped signature" \
  src/porter/repo.py \
  's = s.replace("        if ok.returncode != 0:", "        if False:")' \
  tests/test_signing.py
# A signature over a Release that no longer exists is worse than none: the
# directory looks signed to anybody who lists it and verifies for nobody.
check "T14 re-indexing unsigned removes the stale signature" \
  src/porter/repo.py \
  's = s.replace("for residue in (\"Release.gpg\", \"InRelease\", KEYRING_NAME):", "for residue in ():")' \
  tests/test_signing.py
echo "════ Task 14 — the nspawn gate ════"
# THE claim of the whole task: the unit reached active. Neutered, MUTATION 1 --
# a bundle whose ExecStart names a module that is not there -- installs at rc=0
# and the gate reports a pass on a service that never ran.
check "T14 the unit must reach active" src/porter/gate.py \
  's = s.replace("r.check(_marker(o, \"ACTIVE\") == \"active\",", "r.check(True,")' \
  tests/test_nspawn_gate.py
# ActiveState alone cannot tell a service that came up from one in a restart
# loop: with no Type=, systemd reports `active (running)` the instant the fork
# succeeds. Measured 2026-08-08 -- MUTATION 1's process lives ~25 ms and the
# gate's wait loop DID break on that transient.
check "T14 SubState must be running, not auto-restart" src/porter/gate.py \
  's = s.replace("r.check(_marker(o, \"SUBSTATE\") == \"running\",", "r.check(True,")' \
  tests/test_nspawn_gate.py
check "T14 the unit must not already be flapping before the kill" \
  src/porter/gate.py \
  's = s.replace("r.check(_int(o, \"NRESTARTS_PRE\") == 0,", "r.check(True,")' \
  tests/test_nspawn_gate.py
# The four directives systemd-analyze verify CANNOT check, because it reports
# keys it does not recognise and never keys that are missing (measured
# 2026-08-08: deleting ProtectSystem=strict leaves it clean). MUTATION 2 is a
# unit with each of these removed that installs, starts and answers.
check "T14 systemd must have loaded User=" src/porter/gate.py \
  's = s.replace("r.check(_marker(o, \"SHOW_USER\") == app,", "r.check(True,")' \
  tests/test_nspawn_gate.py
check "T14 systemd must have loaded ProtectSystem=strict" src/porter/gate.py \
  's = s.replace("r.check(_marker(o, \"SHOW_PROTECT\") == \"strict\",", "r.check(True,")' \
  tests/test_nspawn_gate.py
check "T14 systemd must have loaded Restart=on-failure" src/porter/gate.py \
  's = s.replace("r.check(_marker(o, \"SHOW_RESTART\") == \"on-failure\",", "r.check(True,")' \
  tests/test_nspawn_gate.py
# ...and that User= took EFFECT, not merely parsed. Read off /proc/<mainpid>,
# which the kernel owns by the process's real user.
check "T14 the running process must belong to the service user" src/porter/gate.py \
  's = s.replace("r.check(_marker(o, \"MAINUSER\") == app,", "r.check(True,")' \
  tests/test_nspawn_gate.py
# Restart=on-failure demonstrated rather than declared: SIGKILL the main process
# and systemd must put a DIFFERENT one back. A unit file saying on-failure and a
# unit systemd actually restarts are identical on disk.
check "T14 systemd must actually replace a killed process" src/porter/gate.py \
  's = s.replace("    r.check(newpid > 0 and newpid != mainpid,", "    r.check(True,")' \
  tests/test_nspawn_gate.py
# ---------------------------------------------------------------------------
# DELIBERATELY NOT ENTERED, and why. Reporting these matters more than padding
# the count: an implementer who lists them as guarded leaves the next reader
# believing a check is load-tested when it is not.
#
# * `PROTECT_BLOCKS_RC` / `PROTECT_CONTROL_RC` -- the pair that proves
#   ProtectSystem=strict blocks a write on THIS kernel. Neutering either leaves
#   every test green: no bundle in the suite can make a loaded `strict` fail to
#   be enforced, because that would take a broken kernel or a broken nspawn.
#   The pair is self-controlling at run time instead (the control must pass on a
#   transient unit with no sandboxing while the subject fails), which is the
#   best available and is not the same as a registry entry.
#
# * `_verify_with_the_shipped_key`'s tamper control (`if control.returncode == 0`)
#   -- it can only fire if gpgv fails open. Nothing in the suite can arrange
#   that without replacing gpgv, so it has no entry. It is a control on a
#   control and is documented as such.
#
# * The magnitude floor on the nspawn payload (`PAYLOAD_KB`) -- OVERLAPPING
#   (trap 4). A package small enough to trip it has no interpreter, so its
#   service never starts and the ACTIVE check goes red first. The isolated
#   guard for payload magnitude is the docker gate's `truncated_usb`, which
#   truncates v_prev only so the upgrade repairs the tree and size is the sole
#   red. That entry already exists; this one would duplicate it badly.
#
# * The three before-controls (`PREHEALTH_RC`, `PRESTATE`, `PREUSER`) -- same
#   shape. They assert that the base image does NOT already have the thing, and
#   no bundle can make a clean Debian root grow a demo-app user before install.
#   They are what make the after-checks meaningful and they are unfalsifiable
#   from inside the suite; a registry entry would report PASS for the wrong
#   reason or FAIL always.
echo
echo "════ control: suite green again after every restore ════"
purge; final=$(run); echo "  restored rc=$final"
[ "$final" -eq 0 ] || { echo "  FAIL — harness left dirty"; fail=1; }
echo
echo "════ RESULT: $([ $fail -eq 0 ] && echo 'all guards still bite' || echo 'PROBLEMS FOUND') ════"
exit $fail