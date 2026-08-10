# porter — Tasks / Next

Working roadmap. Shipped history lives in `CHANGELOG.md`; the design and its
measurements live in `docs/`. This file is the priority list — keep it terse and
current.

Current release: **v0.1.0** (2026-08-10). Slices 1 and 2 executed; 345 tests,
168 mutation-verified guards, 11 gallery examples, CI green with all five
`PORTER_REQUIRE_*` gates armed.

## State: four approved designs, zero implementation plans

Everything below was designed and approved on 2026-08-10 and **none of it is
built**. Each item's first step is therefore the same: write the implementation
plan into `docs/plans/`, using `superpowers:writing-plans`. The two slice plans
already in `docs/plans/` are the model for shape and detail.

Read `docs/design-spec.md` and `AGENTS.md` first — the non-negotiable rules and
the gate rule govern all four.

---

## 1. pyproject as the source of truth — **do this first**

**Spec:** `docs/2026-08-10-pyproject-as-source-of-truth-design.md`

porter packages a **uv-managed project**: `pyproject.toml` + `uv.lock` are the
truth for what code and which dependencies ship. `porter.yaml` keeps only the
deployment shape and the system boundary. Hand-written `requirements:` is
retired.

**Why first.** Largest blast radius, and upstream of item 3. It changes the
manifest schema, so anything built on the current schema gets rewritten. It is
also the only item that unblocks a real adopter: **six of ainbox's eight apps
cannot be expressed at all today** — they consume `warden-client` through a path
source, and `requirements:` can only name index packages.

**Consequences to plan for, not discover:**

- **All eleven gallery examples carry a `requirements:` block** that this
  retires. The gallery *is* the schema, so rewriting it is part of the work, not
  follow-up.
- **`docs/design-spec.md` documents `requirements:` as a manifest field.** Its
  affected sections need reconciling in the same batch.
- **The editable trap is the sharp edge.** Measured 2026-08-10: installing
  peacock's export as-is yields a finder shim holding
  `/home/apiad/Workspace/repos/ainbox/packages/prism/prism` and *no prism
  source*. `uv export --no-editable` fixes it — but the guard must assert the
  **importable module directory**, not glob `prism*`, because
  `prism-0.3.1.dist-info` matches that glob and is exactly what the broken tree
  contains. Positive control required: the check must go red against an editable
  install.
- Interpreter version = **lowest satisfying minor of `requires-python`, latest
  patch**. Recorded in the provenance stamp. No `requires-python` and no
  override is a refusal, not a default.

---

## 2. The build floor — independent of 1, upstream of `porter check`

**Spec:** `docs/2026-08-10-build-floor-design.md`

Two mechanisms for two different fears: a **cross-target resolve check** (do the
derived `Depends:` names exist on each target?) and **`build_floor:`** as a real
field governing where *compiled* payload is produced.

**Why here.** It introduces `targets:` and `resolve_check()`, which item 3's
`porter check` verb drives. Doing it before the CLI means that verb has
something to be a surface over.

**The hole it closes:** `build_floor:` does not exist. The slice-1 plan put it in
every example manifest; it never landed, `spec.py` refuses unknown keys, and
`porter build` runs on the host, never in a container. So "build on the floor" —
the rule the entire `Depends:` story rests on — is enforced by nothing. zion is
26.04.

**Traps:** the resolve check needs network at build time (target images ship
with apt lists cleaned) and must **refuse rather than skip** when it cannot
reach a target. `--simulate` proves a name resolves, not that the ABI matches —
that is mechanism 2's job and neither covers the other.

---

## 3. The CLI surface — needs 1 and 2

**Spec:** `docs/2026-08-10-cli-surface-design.md`

Six verbs: `init`, plus the `lint` → `build` → `check` → `gate` → `publish`
ladder. `--json` on all of them, four meaningful exit codes.

**Why last of the three.** `init` reads `pyproject.toml` (item 1) and `check`
drives the resolve check (item 2). Built before them, it would be a surface over
things that do not exist yet.

**The work is not the subcommands.** `CHANGELOG.md` records why `gate` and
`publish` are library-only: *their inputs are not manifest fields.* The slice is
mostly manifest completion — `readme:`, `targets:`, `gate.seed:`, `signing:` —
and by the gallery rule each needs the example that defines its shape. The
subcommands fall out afterwards.

**Decisions already made, do not re-litigate:**

- `check` and `gate` stay **separate** — one is an apt query, the other boots a
  machine.
- `build` runs `lint` first and **there is no `--no-lint`**. A fast gate that can
  be skipped before a slow one is a gate that gets skipped.
- **Exit 3 = the environment cannot satisfy this check** (no Docker for `gate`),
  never 0. Skipping is not passing — the same argument as the five
  `PORTER_REQUIRE_*` variables.
- `signing:` names an **identity**, never key material.

**Known gap, already written down:** `init` cannot infer the config split, so
`admin_keys:` arrives as commented scaffolding and an un-edited scaffold ships an
empty split. `lint` cannot catch it either.

---

## 4. The removal path — fully independent, parallelisable

**Spec:** `docs/2026-08-10-removal-path-design.md`

`prerm` / `postrm`, purge semantics, and `uninstall.sh` on the USB tree. porter
emits **only** `postinst` today, so a package installs and updates but never
comes off.

**Position.** Depends on nothing above and nothing above depends on it — it can
run first, last, or in parallel with any of them by a second agent. It is listed
fourth only because the other three unblock an adopter and this one does not.

**Decisions already made:** `uninstall.sh` defaults to `remove`, `--purge` is
explicit (and cannot prompt — rule 9 holds here too, so the flag is the entire
safety margin). Purge deletes `/etc/<pkg>/env` and `/var/lib/<pkg>` and nothing
else; there is **no `purge_paths:` field**, because that is a manifest key that
expands to `rm -rf` on a client's disk. Disable happens in `prerm` while the
unit file still exists. No new gallery example — removal is behaviour every
`service`/`oneshot` package gets, so the existing eleven are its coverage.

---

## Standing rules for whoever picks this up

- **Every new guard needs an entry in `scripts/reverify-guards.sh`.** A guard
  with no entry is unverified by the only test that matters. Run
  `--check-patterns` first; it catches the dead-pattern class in about a second.
- **Purge `__pycache__` after any edit-run-restore cycle.** An equal-length edit
  reverted inside the same second leaves bytecode Python considers current.
- **zion is 26.04 and `ubuntu-latest` is 24.04**, so a green local suite is not
  evidence about the runner.
- **`uv export`/`uv lock` flags used by these designs were verified on uv
  0.11.29** (2026-08-10): `--frozen`, `--no-editable`, `--no-emit-project`,
  `--no-hashes`, `--package`, `--extra`, `--group`, `--only-group`, `--no-dev`,
  and `uv lock --check`. Re-verify if the pinned uv moves.

## Not on this list

- **Adopting porter in any repo.** That is each repo's work, done there — but
  item 1 is what makes ainbox adoptable at all.
- **`.rpm` or any second emitter.** The verbs and the manifest are named for
  intent rather than for dpkg, so it stays possible; nothing is built for it.
- **A bundled Chromium build**, per `CHANGELOG.md`'s *Not in 0.1.0*.
