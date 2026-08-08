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
    URL and checksum, porter picks no vendor.
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
PORTER_REQUIRE_UV=1 uv run --extra dev pytest
```

**Always set `PORTER_REQUIRE_UV=1`.** Most of the suite needs `uv` on PATH to
vendor an interpreter; without that variable those tests *skip* and pytest still
exits 0 — a green run that tested almost nothing. The variable turns the skip
into an error. It is off by default so a contributor without uv gets skips rather
than a wall of failures; it must be on anywhere a green result is trusted, and it
is the first thing to wire into CI when CI exists.

## Documents

| Path | What it is |
|---|---|
| `docs/design-spec.md` | The design, with the measurements behind each decision. Canonical. |
| `docs/plans/` | Implementation plans, one per slice. |
| `know-how/` | Procedure docs. Match the task against each *when to reach for it* line. |

## Know-how

*(Empty — porter is a scaffold. Write an entry when a procedure survives being
done twice.)*

## State

**Slice 1 in progress** — `docs/plans/2026-08-07-slice-1-example-gallery.md`.

- **Task 1 done:** `src/porter/interpreter.py` — `vendor()` materialises a
  relocatable python-build-standalone tree; `install()` puts packages in its own
  `site-packages`. 13 tests. The guard refuses any root that is a virtualenv, is
  not under `uv python dir`, or lacks the stdlib — because the pre-guard code
  returned *successfully* with the wrong tree while every test stayed green.
- **Task 2 done:** `src/porter/deb.py` — `build_deb()` turns a staged tree into a
  `.deb` through a hand-written `DEBIAN/`, refusing any stage that writes to
  `/var/lib` or `/var/log`, carries `/etc/<pkg>/env`, or carries `.venv`/`.git`/
  `.env`. 15 tests, each asserted against the built artefact. `__pycache__` is
  *not* treated as residue: a vendored interpreter ships 35 such directories.
- **Next:** Task 3 — stage a vendored interpreter plus app code and package it.

The `porter.yaml` schema is defined by the example gallery: each example is a
manifest that must parse and build, so a field with no example exercising it does
not exist. Writing the schema in prose first is how the `build:` escape hatch
quietly becomes the default path.
