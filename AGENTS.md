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

Docker is a **build** dependency and never a client one. That asymmetry is the
whole design; do not erode it.

## Rules that are not negotiable

These were each paid for with a measurement. `docs/design-spec.md` has the
evidence; this is the short form.

1. **No venv, ever.** `uv venv --relocatable` writes an absolute symlink to the
   build host's interpreter and dies at the client. Vendor a
   python-build-standalone tree and install into its own `site-packages`.
2. **`cp -aL`, not `cp -a`**, when materialising that interpreter — uv's managed
   directory is a symlink, and `cp -a` vendors nothing.
3. **`python -m <module>`, never `bin/` console scripts** — their shebangs are
   absolute build paths.
4. **Config is two files.** `/etc/<pkg>/defaults` (conffile, package-owned) and
   `/etc/<pkg>/env` (admin-owned, never shipped). A single file either fails the
   unattended upgrade or silently withholds new keys forever.
5. **`postinst` never asks a question.** Interactive configuration lives in
   `<app>-setup`, a separate first-run wizard.
6. **The package never writes to `/var/lib/<pkg>/`.** That is the client's.
7. **Ubuntu 22.04 is the build floor.** Verified to run on glibc 2.35 → 2.41.

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

**Scaffold. No implementation yet.** The design is settled and measured; the
first slice is `docs/plans/2026-08-07-slice-1-sigere-api.md`.

The `porter.yaml` schema is deliberately unspecified — it gets derived from real
repos during the first migrations, not invented up front. Fixing a schema before
a consumer exists is how the `build:` escape hatch becomes the default path.
