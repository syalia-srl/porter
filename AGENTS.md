# porter

porter builds airgapped `.deb` installers for Debian-family clients. One command
turns a repo into a signed local apt repository on a USB tree; the client runs one
command to install it, and the same command to update it.

It exists because four repos ([[ainbox]], [[transforma-cuba]], [[leyes-cuba]],
[[une-tools]]) each hand-built the same delivery path, and the copies drifted: 176
commits on deploy paths in three months. Its users are the maintainers of projects
like those, shipping to clients that have no internet and nobody watching the
install.

**Read this file, then DESIGN.md, then the know-how doc for the job in front of
you.** This file changes when porter's goals change. Nothing in it should be made
false by a commit that adds code, an example or a test.

## What done means

A capability is done when:

1. an example in `examples/` exercises it and builds from a clean tree;
2. `make test` passes with all five gates armed, here and in CI;
3. every guard it adds is in the guard registry, and `scripts/reverify-guards.sh`
   shows each one bites;
4. its CHANGELOG entry states what was measured, not what was intended.

A capability that works on zion and has no example, or no measurement, is not done.

## Where everything lives

Each place changes at a different rate. Put a fact in the one that matches what
would make it false.

| | holds | changes when |
|---|---|---|
| `AGENTS.md` | what porter is, who it is for, what done means | the goals change |
| `DESIGN.md` | the asymmetry, the twelve rules, refuse-never-repair, the gallery as schema, the gate rule | the architecture changes |
| `docs/design-spec.md`, `docs/*-design.md` | the measurements behind each rule, and approved extensions | a design is made or superseded |
| `TASKS.md` | what to build next, in order | work is picked up or lands |
| `CHANGELOG.md` | what shipped, with its measurement | a release |
| `know-how/` | how to do one job | a procedure changes |
| `Makefile`, `.rift.yaml`, CI | every mechanical check | a gate is added or dropped |
| code, tests, examples | everything else | constantly |

Nothing derivable is written down: counts, status and file lists are one command
away. Nothing mechanical is restated: `make` lists the gates, and each gate says
what it wants.

## Working here

Start with `TASKS.md` if you are picking up work, and read `DESIGN.md` before
changing a module.

`make` lists the gates. Run `make test` for any green result you intend to trust.
`make know-how` prints the procedure docs, one `when:` line each; read the ones
that match the task. `make lint-docs` checks these docs.

Commits follow the workspace convention: conventional commits, English, one
logical change, named paths only.
