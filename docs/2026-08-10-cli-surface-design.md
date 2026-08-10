---
date: 2026-08-10
type: design
status: approved in conversation 2026-08-10; implementation plan not yet written
extends: docs/design-spec.md
depends_on: [docs/2026-08-10-pyproject-as-source-of-truth-design.md, docs/2026-08-10-build-floor-design.md]
siblings: [docs/2026-08-10-removal-path-design.md]
---

# The CLI: six verbs, and the manifest work that makes them possible

*Reach for this when adding a verb, changing exit codes or output, or wondering
why `gate` and `publish` were library-only. It depends on the pyproject and
build-floor designs; read those first.*

## The work is not the subcommands

`porter build` is the only verb today, and `CHANGELOG.md` records exactly why:

> They are not verbs because the inputs they need — a README body, a health URL,
> a seed set, a gate image — are not manifest fields, and by the gallery rule a
> field with no example exercising it does not exist.

So this is not an argparse exercise. **A verb exists when its inputs live in the
manifest**, and the work is completing the manifest. The subcommands fall out for
free afterwards. Adding `porter gate` while its gate image still arrives as a
function argument would produce a verb that only porter's own tests can call.

The second force is that **an agent is a first-class caller.** Not a nicety: the
adoption path for every repo porter serves is an agent driving it. That means
machine-readable output, exit codes that carry meaning, no TTY anywhere, and
refusals that name the fix rather than only the fault.

## The ladder

`init` scaffolds and then steps out of the way. The other five form a ladder:
each rung is roughly an order of magnitude more expensive than the last and
proves correspondingly more. A caller stops at the rung its change warrants.

| Verb | Cost | Proves | Needs |
|---|---|---|---|
| `lint` | ~1 s | the manifest, lock and pyproject agree | nothing |
| `build` | seconds–minutes | it assembles, passes the FHS lint, and packages | uv; Docker only if compiling |
| `check` | seconds | the artefact's contents, magnitude, and that `Depends:` resolve per target | target images |
| `gate` | minutes | it installs, upgrades, runs, and comes off — on a client-shaped machine | Docker, systemd-nspawn |
| `publish` | seconds | a USB tree an operator can carry | the built `.deb`s |

## `porter init`

**Refuses without a `pyproject.toml`**, naming what it wanted. porter packages
uv-managed projects; there is nothing to derive otherwise.

Reads what the project already states — `name`, `version`, `description`,
`authors`, `requires-python`, `[project.scripts]` — asserts `uv.lock` exists and
is current, and detects whether this is a standalone project or a workspace
member. ainbox has both shapes, so detection is not optional.

**`--kind` is asked, never inferred.** Guessing `service` from a `fastapi`
dependency yields a plausible manifest that is wrong, and a wrong manifest that
looks right costs more than a question. On a TTY it prompts; `--kind` plus
`--non-interactive` covers agents and CI.

**It scaffolds by copying the matching gallery example**, substituting what it
derived — not by rendering a template. The gallery *is* the schema, so a
template would be a second definition of it, free to drift. What init cannot
infer (`env:`/`admin_keys:`, `health:`, `schedule:`, `native_binaries:`) arrives
as that example's own commented block, which is already the best documentation
those fields have.

## `porter lint`

No build, no container, about a second. This is the loop a human and an agent
actually iterate in, and its speed is a feature to defend.

It proves the **triple agrees** — `pyproject.toml`, `uv.lock`, `porter.yaml`:

- the lock exists and is current (`uv lock --check`)
- no retired `requirements:`; no unknown keys at any level
- kind-specific needs: `oneshot` has `schedule:`, `command` has `bin_name`
- `admin_keys` are valid shell identifiers and do not collide with `env:`
- `after:` resolves against declared siblings, with no cycles
- `native_binaries:` present implies `build_floor:` present
- an interpreter version resolves from `requires-python`
- declared source paths exist

**`lint` must state what it does not cover.** It cannot tell you whether the
module imports, whether `Depends:` resolve on a client, or whether the unit
starts — those need an artefact. A fast check that is quietly mistaken for a
complete one is how a green run stops meaning anything.

## `porter build`

**`build` runs `lint` first and there is no flag to skip it.** A fast gate that
can be skipped before a slow one is a gate that gets skipped, and the failures
lint catches are precisely the cheap ones. `--no-lint` is not an option porter
offers.

**The common case does not use Docker.** A vendored interpreter and Python
source are already distro-independent — the `glibc-floor` job proves that on
every push across 2.35 → 2.41 — so a container buys nothing and costs the
developer loop. **Docker enters only when the component compiles something**
(`native_binaries:`, or a `build:` hook invoking a compiler), and then the build
runs inside the declared `build_floor:` image, because that is where glibc
actually matters. Docker remains a build dependency and never a client one.

Per component: bake → export the lock (`--frozen --no-editable`) → vendor the
interpreter → install dependencies → stage source → emit unit / wrapper / split
config / maintainer scripts → **FHS lint** → derive `Depends:` → `dpkg-deb`.

The FHS lint is a refusal inside `build`, not a report. A stage that writes to
`/var/lib`, ships `/etc/<pkg>/env`, carries an absolute symlink or pre-stages its
own `DEBIAN/` does not become a package.

**Emits `dist/*.deb`** — one per component, plus metapackages.

## `porter check`

Artefact-level proofs that boot nothing: contents and **magnitude** (a truncated
payload installs cleanly and is useless), the provenance stamp, and the
cross-target **`Depends:` resolve check** — `apt-get install --simulate` against
each target's real package database, which queries without installing.

Kept separate from `gate` deliberately: this is an apt query and `gate` boots a
machine. A caller wants to pay this one often.

## `porter gate`

Two engines, proving different things, both required:

- **Docker, `--network none`** — the payload runs, an upgrade preserves client
  state, the install reaches no prompt under `setsid` with stdin closed, and
  nothing was fetched from the network.
- **systemd-nspawn** — real PID 1. The unit *starts*; `User=`,
  `StateDirectory=`, `Restart=on-failure` and `ProtectSystem=strict` are read
  back off what systemd **loaded** rather than what porter wrote; and the
  package comes off cleanly, with no dangling symlink.

## `porter publish`

Emits the deliverable:

```
usb/
  install.sh          install OR update -- the same command, dpkg knows which
  uninstall.sh        remove; --purge for the destructive one
  README.txt
  repo/
    <pkg>_<version>_amd64.deb ...
    Packages  Packages.gz  Release
    Release.gpg  <app>.gpg      (when signed)
```

## The manifest work

What each library-only input becomes. **Each row needs the gallery example that
defines its shape** — that is the rule that has kept the schema honest, and it
is what makes this a real slice rather than a rename.

| Input, today | Becomes | Defined by |
|---|---|---|
| `usb_tree(readme=…)` — a string body | `readme:`, a **path** to a file in the project | any gallery entry |
| `usb_tree(app=…)` | derived: the metapackage, or the sole component | `examples/suite` |
| `gate(image=…)`, nspawn rootfs | `targets:` — shared with `check`'s resolve list | `examples/service-fastapi` |
| `gate(health_url=…)` | **derived**, not a new field: `http://127.0.0.1:${PORT}` + the component's existing `health:` | `examples/service-fastapi` |
| `gate(seed=…)` — client state to plant | `gate: {seed: {…}}` | `examples/stateful-service`, which already is that shape |
| `sign_key`, `gpg_home` | `signing: {key: <uid>}` — the **identity only** | `examples/suite` |

`readme:` is a path rather than an inline string so the operator-facing text is
a reviewable file in the project's own language — porter passes it through and
bakes in no language of its own.

**Signing names an identity and never key material.** The keyring stays in the
environment (`--gpg-home`, or the caller's default). A manifest is committed;
a private key is not, and a schema that accepts one is an invitation.

## The agent contract

- **`--json` on every verb.** A stable object per verb: `{ok, verb, findings[],
  artifacts[]}`, each finding carrying `code`, `message`, `where` and — for a
  refusal — `fix`. An agent parsing prose is a defect waiting to land.
- **Exit codes carry meaning**, so control flow needs no output parsing:

  | Code | Meaning |
  |---|---|
  | 0 | the thing porter was asked to prove holds |
  | 1 | a **refusal** — a real defect in the project or artefact |
  | 2 | usage error: bad flags, missing manifest |
  | 3 | **the environment cannot satisfy this check** — no Docker for `gate`, no target images for `check` |

  Code 3 exists because *skipping is not passing*. It is the same argument as
  the five `PORTER_REQUIRE_*` variables: a check that could not run must not be
  reported as one that ran and was happy. An agent reads 3 as "get me a better
  machine", never as success.
- **No TTY anywhere.** Every verb completes under `setsid` with stdin closed —
  the same harness the install itself is held to.
- **Refusals name the fix.** porter's refusals are already good at this; the
  contract is that the CLI carries it into JSON rather than flattening it to a
  message.

## Properties to prove

1. `init` in a directory with no `pyproject.toml` refuses, and names it.
2. `init` output **builds without hand-editing** for each kind — the scaffold is
   a working manifest, not a form.
3. `init` derives name, version, description, maintainer and interpreter version
   from `pyproject.toml`, and restates none of them as literals.
4. `build` runs `lint` first: a manifest lint refuses is refused by `build` too,
   with the same finding, and **no flag bypasses it**.
5. A component that compiles builds inside `build_floor:`; one that does not
   builds on the host and starts no container.
6. Every verb emits valid JSON under `--json`, including on refusal.
7. The four exit codes are distinct and correct — in particular 3 rather than 0
   when Docker is absent for `gate`.
8. Every verb completes under `setsid` with stdin closed, **with a control**
   showing a bare `read` fails under that harness.
9. `gate` and `publish` run **from the manifest alone**, with no argument that
   is not a path or an override.

## Refusals

- `init` without `pyproject.toml`, or with a missing or stale lock.
- `init` without `--kind` on a non-TTY.
- Any verb whose environment cannot satisfy it — exit 3, never a skip.
- `signing:` carrying key material rather than an identity.
- `readme:` naming a file that does not exist.

## Known limits

- **`--json` is a compatibility surface.** Once an agent parses it, changing a
  field breaks callers porter cannot see. It gets versioned from the first
  release, and the shape is part of the acceptance criteria rather than an
  implementation detail.
- **`check`'s targets are a list porter is told, not one it discovers.** A
  client on a release nobody listed is unverified, silently. The default is the
  four the glibc floor already claims.
- **`init` cannot infer the config split.** Which values an operator owns is a
  deployment decision with no signal in the code, so `admin_keys:` arrives as
  commented scaffolding and an un-edited scaffold ships an empty split. `lint`
  cannot catch that either — it is a real gap, and naming it is the honest
  treatment.

## Out of scope

- **`porter adopt`** — a verb that migrates an existing hand-rolled deploy path.
  Tempting, and it is a research project wearing a verb's clothes.
- **A TUI or any interactive mode beyond `init`'s single question.**
- **Emitters other than `.deb`.** The verbs are named for intent (`build`,
  `check`, `gate`, `publish`) rather than for dpkg, so another emitter would not
  rename them — but nothing here is built for one.
