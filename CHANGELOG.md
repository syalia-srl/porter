# Changelog

## Unreleased — 0.1.0

**In progress.** Facts below are measured, with the run or commit that measured
them. Anything not yet true is under *Not in 0.1.0*, not omitted.

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
| `Depends:` is derivable | 6 ELF objects → 32 sonames → 24 packages from a 380 MB Chromium tree, via `objdump -p` + `ldconfig -p` + one `dpkg -S`. Use `objdump`, not `ldd`: `ldd` executes the loader per file and hung a probe long enough to look like a deadlock |
| The install reaches no prompt | Completes `rc=0` under `setsid` with stdin closed, with a broken network apt source present and an admin-edited conffile — with a control proving a bare `read` *does* fail under the same harness |

### How this is kept honest

- **`scripts/reverify-guards.sh`** disables each guard at its use site and requires
  the suite to go red. A guard with no entry there is unverified by the only test
  that matters. Runs in a detached worktree, so it cannot make a concurrent
  reviewer read a mutant.
- **CI runs with `PORTER_REQUIRE_UV`, `PORTER_REQUIRE_DOCKER` and
  `PORTER_REQUIRE_SYSTEMD` all armed**, because a green run that skipped most of
  the suite is the exact failure porter exists to prevent.
- **`know-how/mutation-testing-a-guard.md`** records the five traps that have cost
  real time here, including a reverted mutation that kept running (stale `.pyc`
  after an equal-length edit) and a mutation that mutated nothing.

### Not in 0.1.0

Named, not omitted:

- **Adopting porter in any repo.** That is each repo's work, done there.
- **A bundled Chromium build.** `browser: system` ships; pinning an artifact waits
  on verifying its terms. Chromium is BSD and redistributable, but Google
  publishes no stable Linux tarball for it.
- **The bwrap sandbox and GPU-native paths** — consumer-side consequences of
  dropping Docker, designed and executed in the repos that need them.

### Known limits

- **No systemd unit has ever been *started* by systemd.** `User=`,
  `StateDirectory=`, `ProtectSystem=strict` and the rest are assertions on the
  emitted file plus `systemd-analyze verify`; the end-to-end tests run the service
  in a container with no PID 1. The unit is valid and the program runs — that they
  compose is not yet demonstrated.
- **`systemd-analyze verify` reports unrecognised keys, never missing ones.**
  Deleting `ProtectSystem=strict` leaves it green, which is why direct directive
  assertions exist alongside it.
- **`porter build` emits `service` and `command` components only.** `oneshot`,
  and therefore every scheduled job, is *refused by name* rather than emitted:
  `systemd.unit()` takes no `Type=` and writes no `.timer`, and the postinst
  enables `<pkg>.service` unconditionally, so a oneshot pushed through them
  would install at rc=0 and run as a permanently-restarting service.
  `examples/oneshot-timer` is what unblocks it. `examples/command` does not
  exist yet either — the kind is implemented and tested, but the gallery entry
  that would define its manifest shape is still owed.
- **`sudo -n` refuses rather than prompting**, by choice: a hidden password prompt
  in an unattended run is indistinguishable from a hang.
- **The 2 GB figure is synthetic** — `/dev/urandom` payloads, not a real image.
