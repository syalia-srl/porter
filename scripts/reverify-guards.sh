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
check() {  # check <label> <file> <python-replace-expr> <expect-tests>
  local label=$1 file=$2 expr=$3 scope=$4
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
  's = s.replace("if [ -d /run/systemd/system ]", "if [ -d / ]")' tests/test_service_e2e.py
# Presence of the hardening directives, which systemd-analyze cannot see: it
# reports keys it does not recognise, never keys that are absent.
check "T3 the unit carries its hardening directives" src/porter/systemd.py \
  's = s.replace("ProtectSystem=strict\n", "")' tests/test_config.py
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
  's = s.replace("chmod 600", "chmod 644")' tests/
check "T3 admin env is read, and read last" src/porter/systemd.py \
  's = s.replace("EnvironmentFile=-/etc/{pkg}/env", "EnvironmentFile=/etc/{pkg}/defaults")' tests/test_config.py

echo "════ Task 4 — assemble ════"
# `oneshot` is refused BY NAME purely to say why and what unblocks it. Trap 4:
# the generic SUPPORTED_KINDS check below refuses it too, so removing this alone
# does not let a oneshot through -- what it loses is the message. The test
# therefore matches "oneshot-timer", which only this branch emits.
check "T4 oneshot is refused by name, with the reason (MESSAGE ONLY -- see Trap 4)" \
  src/porter/assemble.py \
  's = s.replace("if component.kind == \"oneshot\":", "if False:")' tests/test_assemble.py
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
  's = s.replace("from porter.types import Component, Python", "class Component: pass\nclass Python: pass")' \
  tests/test_types.py

echo
echo "════ control: suite green again after every restore ════"
purge; final=$(run); echo "  restored rc=$final"
[ "$final" -eq 0 ] || { echo "  FAIL — harness left dirty"; fail=1; }
echo
echo "════ RESULT: $([ $fail -eq 0 ] && echo 'all guards still bite' || echo 'PROBLEMS FOUND') ════"
exit $fail
