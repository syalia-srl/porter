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
cd "$(dirname "$0")/.." || exit 2
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

echo
echo "════ control: suite green again after every restore ════"
purge; final=$(run); echo "  restored rc=$final"
[ "$final" -eq 0 ] || { echo "  FAIL — harness left dirty"; fail=1; }
echo
echo "════ RESULT: $([ $fail -eq 0 ] && echo 'all guards still bite' || echo 'PROBLEMS FOUND') ════"
exit $fail
