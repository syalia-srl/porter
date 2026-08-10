# AGENTS.md — porter

Read this first. It is the door: orientation, the rules that are not negotiable,
and an index into `know-how/`.

## What porter is

**porter builds airgapped `.deb` installers for Debian-family clients.** One
command turns a repo into a signed local apt repository on a USB tree; the client
runs one command to install it, and the *same* command to update it.

It exists because four repos ([[ainbox]], [[transforma-cuba]], [[leyes-cuba]],
[[une-tools]]) each hand-built the same delivery path, and the copies drifted —
176 commits on deploy paths in three months. Full reasoning, and the measurements
behind every decision, in `docs/design-spec.md`.

## The one idea

**Nothing is inherited from the client OS except glibc, systemd, and — optionally
— the NVIDIA driver.** No Docker, no container runtime, no `nvidia-container-toolkit`,
no system Python. The interpreter is vendored; native binaries are built on the
glibc floor; everything else is package payload.

**And the install runs with nobody watching.** An airgapped client has no
interactive fallback, so a prompt is not an inconvenience — it is a hang. Every
path is non-interactive by construction, not by flag.

Docker is a **build** dependency and never a client one. That asymmetry is the
whole design; do not erode it.

## Rules that are not negotiable

These were each paid for with a measurement. `docs/design-spec.md` has the
evidence; this is the short form.

1. **No venv, ever.** `uv venv --relocatable` writes an absolute symlink to the
   build host's interpreter and dies at the client. Vendor a
   python-build-standalone tree and install into its own `site-packages`.
2. **Dereference the interpreter's root directory** when materialising it —
   `uv python find` resolves through a symlinked directory
   (`cpython-3.12-linux-x86_64-gnu` -> `cpython-3.12.13-...`), and a copy that
   preserves that link vendors nothing while still working on any host that has
   uv. In a shell that means `cp -aL`, never `cp -a`. Note that Python's
   `shutil.copytree` follows a symlinked *source root* regardless of its
   `symlinks=` argument, so the hazard is specific to `cp` and friends —
   verified 2026-08-07. Do not restate the two as equivalent.
3. **`python -m <module>`, never `bin/` console scripts** — their shebangs are
   absolute build paths.
4. **Config is two files.** `/etc/<pkg>/defaults` (conffile, package-owned) and
   `/etc/<pkg>/env` (admin-owned, never shipped). A single file either fails the
   unattended upgrade or silently withholds new keys forever.
5. **`postinst` never asks a question.** Interactive configuration lives in
   `<app>-setup`, a separate first-run wizard.
6. **The package never writes to `/var/lib/<pkg>/`.** That is the client's.
7. **Ubuntu 22.04 is the build floor.** Verified to run on glibc 2.35 → 2.41.
8. **Static system user, never `DynamicUser=yes`.** The latter redirects state to
   `/var/lib/private/<pkg>` at `700 root:root` — a non-root operator can neither
   read nor list it, so backups, monitoring and support all require root, and
   admin-dropped files change owner as the UID rotates.
9. **The install reaches no prompt.** `sudo -n` or an explicit refusal; never
   block on a password. `NEEDRESTART_SUSPEND` is not a real variable — it is
   absent from needrestart 3.6's code. Only `NEEDRESTART_MODE` is.
10. **porter hardcodes no interpreter name or version.** Which Python, and
    whether it is bundled per component or emitted as its own package, is
    declared in each project's `porter.yaml`. No vendor prefix belongs in porter.
    The same rule governs the optional bundled browser: the project declares the
    URL and checksum, porter picks no vendor. A shared interpreter package is
    versioned by the **CPython version its tree reports**, not by the project's,
    and components depend on it with `(= <that version>)` — `>=` would let a
    client keep an older tree and run wheels compiled against a newer ABI.
11. **`Depends:` is derived from `ldd`, never hand-written.** Any bundled native
    binary gets its libraries mapped to target-distro packages at build time. A
    hand-kept list is how a package installs cleanly and then cannot open a
    window, and it goes stale silently on the next upstream build.
12. **A desktop dependency never enters the core package.** GUI needs GTK/X11/NSS
    from the client, which apt cannot fetch on an airgapped box — so it lives in
    a separate `<app>-desktop` package and can never block a headless install.

## The gate rule

**Every assertion in the gate carries a positive control or a magnitude check.**

Not style. During the design, five probes reported passes that were false: a
dangling symlink resolving to the build host's own interpreter; a network probe
using bash-only `/dev/tcp` under dash; an interface count where the binary was
absent; a memory-limit check satisfied by *command not found*; a truncated 12 KB
package reported as built. Each was caught only because something downstream
contradicted it.

The airgap failures that matter are exactly the ones that look like passes on the
build host. So: before asserting isolation, prove the probe *detects* the thing
when isolation is off. Before trusting an artefact, assert its magnitude. And
**never pipe a gate** — `cmd | tail` hands `&&` the exit code of `tail`.

## Language policy

**English for everything, with no exceptions.** Code, comments, identifiers,
error strings, log messages, commit messages, CLI flags and help text, tests,
docs — and equally the files porter *generates*: `install.sh`, `README.txt`,
every banner and every prompt.

porter ships no Spanish. If a component needs operator text in another language,
it passes that text in as data (`readme=`, a component `description`); porter
never bakes a language into its own output. A tool whose audience is
Spanish-speaking is still an English tool.

## Running the tests

```bash
PORTER_REQUIRE_UV=1 PORTER_REQUIRE_DOCKER=1 PORTER_REQUIRE_SYSTEMD=1 \
  PORTER_REQUIRE_CC=1 PORTER_REQUIRE_NSPAWN=1 uv run --extra dev pytest
```

**Always set `PORTER_REQUIRE_UV=1`.** Most of the suite needs `uv` on PATH to
vendor an interpreter; without that variable those tests *skip* and pytest still
exits 0 — a green run that tested almost nothing. The variable turns the skip
into an error. It is off by default so a contributor without uv gets skips rather
than a wall of failures; it must be on anywhere a green result is trusted, and it
is the first thing to wire into CI when CI exists.

**`PORTER_REQUIRE_DOCKER=1` is the same bargain**, for the same reason: the
`docker`-marked tests are the only ones that *install* a package rather than
merely build one, so a run that skips them has verified nothing about the
client.

**`PORTER_REQUIRE_SYSTEMD=1` is the third**, and the one added last.
`systemd-analyze verify` is the only check that a directive in the emitted unit
is one systemd actually *knows* — a misspelled key is not an error to systemd,
it is a log line and a service that starts anyway with none of its config. Note
it reports keys it does not recognise and never keys that are *missing*, so it
does not subsume `test_unit_carries_the_hardening_and_restart_directives_...`;
the two are orthogonal and both are needed (measured 2026-08-08: deleting
`ProtectSystem=strict` leaves `verify` perfectly clean).

**`PORTER_REQUIRE_CC=1` is the fourth**, added with Task 13. `native_binaries:`
is proved against a *real* compiled object — the point of the feature is that
the ELF header is read rather than described — so on a host with no C compiler
every test covering rule 11's derivation skips, and a skipped test is green.

**`PORTER_REQUIRE_NSPAWN=1` is the fifth**, added with Task 14. The nspawn gate
holds the only tests in porter that *start* a unit with systemd, and
`systemd-nspawn` is not on a runner by default — it lives in `systemd-container`
— so without the variable the one check that the hardening is real would be a
skip, which is green. It was armed on a **measurement** and not a hope: 33 s end
to end on `ubuntu-latest` with `systemd-container` installed. Arming a gate that
cannot be satisfied turns every run red for a reason unrelated to the change
under test, which is its own kind of uninformative.

**All five CI-armed.** They are in `.github/workflows/ci.yml`'s `env:` block.
Adding a sixth such variable means adding it there too, or the gate can go
green having skipped exactly the thing the variable exists to force.
Note `scripts/reverify-guards.sh` sets only the first three explicitly and
inherits the rest from the environment — which is why CI's workflow-level
`env:` block is what makes the sharded guard run honest.

**Purge `__pycache__` after any edit-run-restore cycle** (mutation testing, a
bisect, a quick experiment). CPython invalidates bytecode on source mtime *and
size*: an edit that preserves the byte count and is reverted inside the same
second leaves a stale `.pyc` that Python considers current. Measured here
2026-08-08 — swapping the unit's two `EnvironmentFile` lines is byte-identical
in length, and after `git checkout` the *mutated* module kept loading against a
clean tree. `find src tests -name __pycache__ -type d -exec rm -rf {} +`.

## Documents

| Path | What it is |
|---|---|
| `docs/design-spec.md` | The design, with the measurements behind each decision. Canonical. |
| `docs/2026-08-10-removal-path-design.md` | The removal path — `prerm`/`postrm`, purge semantics, `uninstall.sh`. Extends the spec, which covers install/configure/update and is silent on removal. Approved, not yet implemented. |
| `docs/2026-08-10-build-floor-design.md` | Where a package is built and how its `Depends:` are proven to resolve on each target. Makes `build_floor:` real — today the "build on the floor" rule is enforced by nothing. Approved, not yet implemented. |
| `docs/2026-08-10-pyproject-as-source-of-truth-design.md` | porter packages a **uv-managed project**: `pyproject.toml` + `uv.lock` are the truth, `porter.yaml` declares only deployment shape and the system boundary. Retires hand-written `requirements:`. Approved, not yet implemented. |
| `docs/2026-08-10-cli-surface-design.md` | The six verbs (`init` plus the `lint`→`build`→`check`→`gate`→`publish` ladder), the manifest completion that lets them exist, and the agent contract (`--json`, four exit codes). Approved, not yet implemented. |
| `docs/plans/` | Implementation plans, one per slice. |
| `know-how/` | Procedure docs. Match the task against each *when to reach for it* line. |

## Know-how

| Doc | When to reach for it |
|---|---|
| `know-how/mutation-testing-a-guard.md` | You added or changed a guard and must show it bites; or you are reviewing someone else's mutation evidence; or you are cutting a release. Carries the five traps that have cost real time here. |
| `know-how/baking-a-sqlite-payload.md` | A package's payload includes a SQLite database built by a `bake:` step — a corpus, a search index, a beaver store. Also when a client reports empty results, missing recent rows or `attempt to write a readonly database`, none of which reproduce on the build host. |

## State

**Slice 1 complete — released as 0.1.0 on 2026-08-10.**
Plan: `docs/plans/2026-08-07-slice-1-example-gallery.md`. What the release is,
and explicitly is not, is in `CHANGELOG.md`; read that before believing any
capability claim below is complete.

**345 tests, 168 guard entries, 11 gallery examples**, all five
`PORTER_REQUIRE_*` variables armed in CI. `porter build` is the only CLI verb;
`usb_tree`/`write_index`/`sign_release`/`gate`/`nspawn_gate` are the library
API — see *Not in 0.1.0*.

- **Task 1 done:** `src/porter/interpreter.py` — `vendor()` materialises a
  relocatable python-build-standalone tree; `install()` puts packages in its own
  `site-packages`. 13 tests. The guard refuses any root that is a virtualenv, is
  not under `uv python dir`, or lacks the stdlib — because the pre-guard code
  returned *successfully* with the wrong tree while every test stayed green.
- **Task 2 done** (fix rounds 1 and 2 applied)**:** `src/porter/deb.py` —
  `build_deb()` turns a staged tree into a `.deb` through a hand-written
  `DEBIAN/`. The stage's top level is an allowlist — `usr`, `etc`, `opt`,
  `lib`, `var`, nothing else — and beneath it the lint refuses a stage that
  writes to `/var/lib` or `/var/log`, carries `/etc/<pkg>/env` as a file *or a
  link*, ships anything under `/etc/` without declaring it a conffile, carries
  an absolute symlink or one escaping the stage, carries `.venv`/`.git`/`.env`,
  or pre-stages its own `DEBIAN/` (refused, never deleted — a silent drop of
  the caller's `triggers` is the failure class this module exists to stop).
  Control values are folded onto Debian continuation lines. 29 tests, each
  asserted against the built artefact. `__pycache__` is *not* treated as
  residue: a vendored interpreter ships 35 such directories.
- **Task 3 done** (fix round 1 applied)**:** `src/porter/config.py` (`split`, `env_postinst`) and
  `src/porter/systemd.py` (`unit`), plus the gallery's first entry,
  `examples/service-fastapi/`. 16 tests. The postinst runs its `systemctl` block
  only `if [ -d /run/systemd/system ]`, and then **without `|| true`** — that
  directory is the only thing distinguishing "no systemd here, skip it" from
  "systemd is real and `enable` failed", and the old blanket `|| true` reported
  success for both. The second is a service that is simply gone after the next
  reboot, with the install having exited 0.

  The example's `porter.yaml` is the
  only place its package name, interpreter version, requirements, ExecStart and
  env template are written — the fixtures read it, so an example that stops
  parsing or building takes the suite red. Four container tests run
  `--network none` against a base image asserted to have **no** `python3`:
  the service answers HTTP under the unit's own `ExecStart`/`WorkingDirectory`;
  a failing `systemctl enable` fails the install on a faked booted host and is
  correctly ignored without one; an upgrade keeps `/etc/demo-app/env` at
  `600 root:root` while delivering both a corrected value and a brand-new
  package-owned key; and the same upgrade with the conffile locally edited is
  refused by dpkg (`end of file on stdin at conffile prompt`, rc=1, with
  `DEBIAN_FRONTEND=noninteractive` set) — the measurement rule 4 rests on,
  reproduced as a positive control.
- **Task 4 done** (fix round 1 applied)**:** `src/porter/assemble.py` — a `Component` becomes a staged
  tree `build_deb` packages unmodified, plus `src/porter/types.py` (the
  dataclasses, which Task 7's `spec.py` will absorb a validator for) and a real
  `porter build`. 25 tests. **Task 7 must extend `porter/spec.py`, not replace
  it:** it re-exports `types.py`'s two dataclasses today, and
  `tests/test_types.py` asserts they are the *same objects*, so a second
  definition of `Component` takes the suite red rather than quietly giving the
  gallery two schemas to drift between. This is the composition that did not exist: Tasks
  1–3 were primitives and every staged tree in the suite was hand-built in a
  fixture. `/usr/lib/<pkg>` is both the payload root and the unit's
  `WorkingDirectory`, so a `source_paths` entry lands under its own basename
  *in* it — `["src/app.py"]`, never `["src"]`, or the module is one directory
  below the import root and the service dies at its first request.
  `conffiles` are derived from the staged tree and returned, never re-derived
  by the caller. Refusals follow deb.py: **refuse, never repair** — `oneshot`
  (no `Type=`, no `.timer`, and `env_postinst` enables `<pkg>.service`
  unconditionally), an unknown kind, a non-bundled interpreter, a command
  carrying config, a non-empty stage root. And the staged interpreter must be
  able to *import* what the package runs: requirements omitted or
  misspelled otherwise builds, lints and installs at rc=0 and dies at ExecStart
  on a client with no network to fix it from. That takes **three** checks and
  each is blind to the others' failure — `module` (the runner, `uvicorn`), the
  payload's own unconditional imports (`fastapi`, which `module` never names
  because `app:app` is uvicorn's *argument*), and `source: ["src"]`, which only
  a check on the tree can catch: `find_spec("src")` succeeds on a directory
  with no `__init__.py` — it is a namespace package — while nothing inside it
  is importable.

  **`porter build` is the entry point and it now has tests of its own**
  (`tests/test_cli.py`). It did not run: the CLI's `--stage` default is the
  relative `build`, and every one of the 18 assemble tests passed an absolute
  `tmp_path`, so nothing in the suite could see it. The CLI also **removes the
  stage it creates**, pass or fail — refusing a non-empty stage is right for a
  tree a caller pre-staged and wrong for one the CLI invents, and leaving it
  meant the command succeeded exactly once. Anything invoking porter's own
  entry point belongs in that file, run from a scratch cwd with every path
  relative, and run **twice**.
- **Tasks 9–11 done:** `bake.py` (data built before packaging, WAL-checkpointed
  and magnitude-asserted), `migrate.py` (migrations keyed on dpkg's `$2`, so they
  run exactly once, plus `<pkg>-setup`), and `systemd.py` ordering (`after:` →
  `After=`/`Requires=`, cycles refused) with `kind: oneshot` (`Type=oneshot`, a
  `.timer`, the timer enabled rather than the service).
- **The gallery is eleven examples:** `service-fastapi`, `command`,
  `oneshot-timer`, `stateful-service`, `baked-data`, `multi-service`, `suite`,
  `shared-interpreter`, `native-binary`, `desktop-app`, `custom-build`. Each
  builds from a clean tree — verified again 2026-08-10, 21 `.deb`s with
  `rm -rf build dist` between every one (22 counting
  `stateful-service/porter-previous.yaml`, the v_prev half of that example).
  The gallery *is* the schema: a field no example exercises does not exist.
- **Run `scripts/reverify-guards.sh` before a release.** In one process the 168
  entries measured 4111 s and timed out; CI shards it six ways, and
  `--check-patterns` catches the whole dead-pattern class in about a second
  before a runner is spent on it.
- **Tasks 5–8 and 12 done:** the USB apt repo and autonomous `install.sh`, the
  Docker gate with mutation bundles, `Depends:` from ELF headers, the
  `<app>-desktop` split, and the `build:` escape hatch. Nine examples build.
- **Task 13 done:** `python: {package: <name>}` emits a **separate interpreter
  package** and components `Depends:` on it by exact version — one 97 MB tree
  instead of one per component (ainbox: ~780 MB saved across ten services).
  Its version is the **CPython version the staged tree reports**, not the
  project's: components may carry different `version:` values, and a
  project-keyed exact pin would be unsatisfiable for whichever disagreed.
  A shared-interpreter component's requirements go to `/usr/lib/<pkg>/` with
  `uv pip install --target` and **never** into the interpreter's own
  `site-packages` — that directory belongs to a package every component
  installs, so two components wanting `fastapi` there is a dpkg file conflict
  at the client. The payload root is already `WorkingDirectory` and the
  wrapper's `PYTHONPATH`, so nothing about sys.path had to be invented and
  `systemd.py` is untouched. `bundled` is unchanged.
  `native_binaries:` stages compiled payload under `/usr/lib/<pkg>/` with the
  mode intact and derives `Depends:` from its ELF headers; a binary linking a
  soname neither the payload nor the build host provides is **refused before
  anything is staged**, naming the sonames. `examples/shared-interpreter`
  (3 `.deb`s: interpreter + service + command) and `examples/native-binary`
  (a `cc`-built program beside its own private `.so`) are the gallery entries;
  `PORTER_REQUIRE_CC=1` is the fourth armed variable.
- **Task 14 done:** the **nspawn gate** and repo signing. This is where the
  emitted unit is finally *started* by systemd, and it closes the limit that
  stood for the whole of Slice 1. `nspawn_gate()` boots the target rootfs with
  systemd as PID 1, installs the real `.deb`, starts the unit, and reads back
  what systemd **loaded** rather than what porter wrote: `User=` off the main
  PID's uid, `StateDirectory=` off the directory systemd created for the unit,
  `Restart=on-failure` off `NRestarts` climbing after a kill, and
  `ProtectSystem=strict` off a `systemd-run` positive control proving the
  directive has teeth on this kernel. `PORTER_REQUIRE_NSPAWN=1` is the fifth
  armed variable, and it was armed on a **measurement** — 33 s end to end on
  `ubuntu-latest` — rather than on a hope, because a gate that cannot be
  satisfied turns every run red for a reason unrelated to the change under test.
  The container gate keeps its own job: it reads `ExecStart=` and
  `WorkingDirectory=` out of the installed unit and runs the first from the
  second, with a control showing the same ExecStart does *not* answer from `/`,
  so `WorkingDirectory=` is proven to be what puts the app on `sys.path`.
- **Task 15 done:** CI green on `main`, and 0.1.0 cut. The red was **not** the
  `t64` rename: `ldconfig -p` and dpkg disagree about `/lib` vs `/usr/lib` on
  ubuntu 24.04 and debian 13 (they agree on 22.04, debian 12 and 26.04), and
  `dpkg -S` matches only the string it recorded — so porter could derive
  `Depends:` on its build floor and on the newest release and refused *every*
  package on the two in between. `packages_owning` now asks in both spellings.
  Read that as the standing warning it is: **zion is 26.04 and `ubuntu-latest`
  is 24.04, so a green local suite is not evidence about the runner.**

The `porter.yaml` schema is defined by the example gallery: each example is a
manifest that must parse and build, so a field with no example exercising it does
not exist. Writing the schema in prose first is how the `build:` escape hatch
quietly becomes the default path.
