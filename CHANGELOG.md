# Changelog

## 0.1.0 — 2026-08-10

First release. Facts below are measured, with the run or the measurement behind
them. Anything not yet true is under *Not in 0.1.0* or *Known limits*, not
omitted.

### What porter does

Builds airgapped `.deb` installers for Debian-family clients. One command turns a
repo into a signed local apt repository on a USB tree; the client runs one command
to install it, and the *same* command to update it — dpkg knows which, so the
sysadmin does not have to.

**Nothing is inherited from the client OS except glibc, systemd and — optionally —
the NVIDIA driver.** No Docker, no container runtime, no `nvidia-container-toolkit`,
no system Python. Docker is a *build* dependency and never a client one.

### Measured

| Claim | Measurement |
|---|---|
| One build runs on glibc 2.35 → 2.41 | Vendored on Ubuntu 22.04; executed on 22.04, Debian 12, 24.04, Debian 13 under `--network none`, no system `python3.12`. Verified **per push** by the `glibc-floor` CI job, first green run 31241859265 |
| A 2 GB package upgrades in 2× payload | Peak transient disk 4096 MB for a 2 GB payload, `dpkg -i` and `apt` alike |
| `apt` from a `file:` repo adds no cache cost | `/var/cache/apt/archives` = 1 MB after installing a 2 GB package — apt installs in place, so the USB path costs the same as `dpkg -i` |
| Config must be two files | A single conffile fails an unattended upgrade with `rc=1` once the admin has edited it, and `DEBIAN_FRONTEND=noninteractive` does *not* suppress dpkg's conffile prompt. A `postinst`-copy file never delivers a newly added key to an existing client, ever. Split — package-owned `defaults`, admin-owned `env` — passes all six properties |
| No venv | `uv venv --relocatable` writes an absolute symlink to the build host's interpreter; relocated venvs returned `rc=127` on every target. A vendored python-build-standalone tree returns 0 |
| The interpreter costs 97 MB | Before any dependency. A full `fastapi`+`uvicorn`+`pymssql`+`pyogrio`+`onnxruntime` set is 337 MB |
| A shared interpreter is worth a package of its own | `examples/shared-interpreter` emits `porter-example-shared-python` at 101.7 MB once, beside a 10.4 MB service and a 20 KB command. Bundled, those two would each carry the 97 MB tree again; ainbox's ten services would carry it ten times |
| `Depends:` is derivable | 6 ELF objects → 32 sonames → 24 packages from a 380 MB Chromium tree, via `objdump -p` + `ldconfig -p` + one `dpkg -S`. Use `objdump`, not `ldd`: `ldd` executes the loader per file and hung a probe long enough to look like a deadlock |
| The install reaches no prompt | Completes `rc=0` under `setsid` with stdin closed, with a broken network apt source present and an admin-edited conffile — with a control proving a bare `read` *does* fail under the same harness |
| systemd starts the unit, and the hardening is real | `nspawn_gate()` boots the target rootfs with systemd as PID 1, installs the `.deb` and starts the service: `User=` read off the main PID's uid, `StateDirectory=` off the directory systemd created, `Restart=on-failure` off `NRestarts` climbing after a kill, `ProtectSystem=strict` off a `systemd-run` control proving the directive bites on this kernel. Measured on `ubuntu-latest` in 33 s end to end, and armed in CI as `PORTER_REQUIRE_NSPAWN=1` |
| The gallery is the acceptance test | All 11 examples build from a clean tree, `rm -rf build dist` between every one: 21 `.deb`s, plus a 22nd from `stateful-service/porter-previous.yaml` — the v_prev half of the upgrade example. 2026-08-10 on zion |

### Fixed in this release

- **`Depends:` derivation was impossible on Ubuntu 24.04 and Debian 13.**
  `ldconfig -p` and dpkg's database disagree about `/lib` versus `/usr/lib`, and
  `dpkg -S` matches only the string it recorded. Measured 2026-08-10, one
  container per release:

  | release | `ldconfig -p` | dpkg recorded | |
  |---|---|---|---|
  | ubuntu 22.04 | `/lib/…` | `/lib/…` | agree |
  | debian 12 | `/lib/…` | `/lib/…` | agree |
  | ubuntu 24.04 | `/lib/…` | `/usr/lib/…` | **disagree** |
  | debian 13 | `/lib/…` | `/usr/lib/…` | **disagree** |
  | ubuntu 26.04 | `/usr/lib/…` | `/usr/lib/…` | agree |

  So porter derived correctly on its build floor and on the newest release and
  refused *every* package on the two in between — a total refusal, which is the
  honest half of the failure, but still a build porter could not do on two of
  the five releases it claims. `packages_owning` now puts both spellings into
  the same single `dpkg -S`, and refuses only what neither name owns; a library
  under `/usr/local` or `/opt` still has exactly one name, so the
  hand-installed refusal is intact and has its own test under an aliased root.
  This is why CI was red on `ubuntu-latest` while zion (26.04) was green.

### How this is kept honest

- **345 tests, 168 guard entries, 11 examples.** The suite runs with all five
  `PORTER_REQUIRE_*` variables armed — `UV`, `DOCKER`, `SYSTEMD`, `CC`,
  `NSPAWN` — because a green run that skipped most of itself is the exact
  failure porter exists to prevent. Armed in CI's `env:` block; a sixth such
  variable means adding it there too.
- **`scripts/reverify-guards.sh`** disables each guard at its use site and
  requires the suite to go red. A guard with no entry there is unverified by the
  only test that matters. It runs in a detached worktree, so it cannot make a
  concurrent reviewer read a mutant, and it is sharded six ways in CI — 168
  entries in one process measured 4111 s and timed out before the end.
  `--check-patterns` applies every mutation in memory and reports the dead ones
  in about a second, which is the entire SKIP class caught before a runner is
  spent on it.
- **`know-how/mutation-testing-a-guard.md`** records the five traps that have
  cost real time here, including a reverted mutation that kept running (stale
  `.pyc` after an equal-length edit) and a mutation that mutated nothing.

### Not in 0.1.0

Named, not omitted:

- **`gate` and `publish` as CLI verbs.** `porter build` is the only verb;
  `usb_tree()`, `write_index()`, `sign_release()`, `gate()` and `nspawn_gate()`
  are the library API and are driven by `tests/test_repo.py`,
  `test_signing.py`, `test_gate.py` and `test_nspawn_gate.py`. They are not
  verbs because the inputs they need — a README body, a health URL, a seed set,
  a gate image — are not manifest fields, and by the gallery rule a field with
  no example exercising it does not exist. Adding them means adding the example
  that defines their shape first.
- **Adopting porter in any repo.** That is each repo's work, done there.
- **A bundled Chromium build.** `browser: system` ships and is the only shape
  emitted; `browser: {source, sha256}` is **refused by name**, not silently
  ignored. Pinning an artifact waits on verifying its terms — Chromium is BSD
  and redistributable, but Google publishes no stable Linux tarball for it.
- **The bwrap sandbox and GPU-native paths** — consumer-side consequences of
  dropping Docker, designed and executed in the repos that need them.

### Known limits

- **`systemd-analyze verify` reports unrecognised keys, never missing ones.**
  Deleting `ProtectSystem=strict` leaves it green, which is why direct directive
  assertions and the nspawn gate exist alongside it.
- **The nspawn gate shares the host kernel.** It proves the unit composes with
  systemd, on *this* kernel. A client kernel lacking a namespace feature the
  hardening relies on is a different result, and porter has not run on one.
- **`Depends:` maps to the build host's package names.** Ubuntu 22.04 is the
  build floor precisely so that mapping is meaningful downstream (`libc6` and
  `libcrypt1` are spelled the same on 22.04 through 26.04), but a build host
  newer than the client's distro can still name a package the client's apt has
  never heard of — the `t64` transition renamed many — and nothing in the ELF
  headers would reveal it. Build on the floor.
- **`sh -n` covers the shell porter *generates*, not shell it ships.** Maintainer
  scripts and `<pkg>-setup` are syntax-checked at build time, and an `admin_keys`
  entry that is not a valid shell identifier is refused. But a conffile that a
  shipped tool `source`s is not checked: an unquoted apostrophe in a component
  description produced a clean rc=0 build and install, then rc=2 at runtime
  (found 2026-08-08 writing `examples/custom-build`). Guessing which `/etc` files
  a payload sources is the wrong fix; the claim is simply narrower than it reads.
- **`sudo -n` refuses rather than prompting**, by choice: a hidden password prompt
  in an unattended run is indistinguishable from a hang.
- **The 2 GB figure is synthetic** — `/dev/urandom` payloads, not a real image.
- **needrestart's interactive dialog was never reproduced.** Its apt hook is live
  on Ubuntu 24.04 and `NEEDRESTART_MODE=a` is in the shipped `install.sh`, but a
  test container has no outdated daemons, so the prompt path never fired. What
  the design rests on is structural — porter upgrades no shared libraries — and
  the variable is belt and braces.
- **The desktop launcher has never been looked at by a human.** Chromium ignores
  unknown flags silently, so "the flags were accepted" is weak evidence; whether
  `--app=` plus `--class=` yields a window that reads as native needs a display
  and an eye. `tests/test_desktop.py` runs the launcher against fake browsers,
  which proves it picks the right one and passes the flags — not what the result
  looks like.
