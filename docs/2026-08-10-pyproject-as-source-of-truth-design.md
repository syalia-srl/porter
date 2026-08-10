---
date: 2026-08-10
type: design
status: approved in conversation 2026-08-10; implementation plan not yet written
extends: docs/design-spec.md
siblings: [docs/2026-08-10-removal-path-design.md, docs/2026-08-10-build-floor-design.md]
---

# porter packages a uv-managed Python project

*Reach for this before touching `requirements:`, the interpreter version, or
anything about how a component's code and dependencies reach the stage. It
replaces the part of the schema where porter restated what `pyproject.toml`
already says.*

## What porter is

**porter's input is a uv-managed Python project.** `pyproject.toml` and
`uv.lock` are the source of truth for what code ships and which dependencies
come with it. `porter.yaml` declares only what a Python project *cannot*: the
deployment shape, and the system boundary Python has no vocabulary for.

Today it is the other way round. `requirements:` is a hand-written list of
package names in `porter.yaml`, passed straight to `uv pip install` — unpinned,
disconnected from the project's own lock, and restated per component.

That is the drift porter exists to eliminate, one layer up. The spec's opening
argument is that four repos hand-built the same delivery path and the copies
drifted; a manifest that restates a dependency list already written and locked
next door reproduces exactly that, with the same mechanism (two files, one
truth, no one reconciling them) and the same outcome.

### The concrete case: ainbox

Not one large `pyproject.toml` — the root is **10 lines**. A uv workspace with
two members, plus **eight colocated independent projects** deliberately excluded
from it, each with its own `pyproject.toml` and its own lock. **Nine lockfiles.**
Six of the apps consume `warden-client` through a path source; peacock also
takes `prism` that way.

Against today's schema, ainbox cannot be expressed at all. `requirements:` names
index packages; `warden-client` has never been published anywhere. Six of eight
apps are unpackageable, and the two that are would ship whatever the build host
resolved that morning rather than what their lock pins.

## What porter reads from `pyproject.toml`

| Field | Becomes |
|---|---|
| `name` | the Debian package name, unless `porter.yaml` overrides it |
| `version` | the package version |
| `description` | `Description:` |
| `authors` | `Maintainer:` |
| `requires-python` | the vendored interpreter version — see below |
| `[project.scripts]` | a `command` component's entry point *target*, never the script itself |
| `dependencies` | **nothing directly.** The lock is what porter reads |

`[project.scripts]` needs care: rule 3 bans shipping console scripts, whose
shebangs are absolute build-host paths. porter takes the *target* — the
`module:function` a script points at — and emits its own `python -m` wrapper.
The name and the target come free from the project; the mechanism stays
porter's.

## Dependencies come from the lock, never from the manifest

porter obtains a component's dependencies by exporting the project's own
lockfile:

```
uv export --frozen --no-emit-project --no-editable --format requirements-txt
```

run from the project directory, and installs the result into the staged
interpreter. Measured on uv 0.11.29 against `ainbox/apps/peacock`:

- Path dependencies export as **relative** paths (`../../packages/warden-client`),
  not absolute `file://` URLs, so they resolve from the project directory and
  survive being built somewhere else.
- `--frozen` left `uv.lock` **untouched** (`git status` clean afterwards). A
  packaging step must never mutate the project it is packaging.
- `--no-emit-project` drops the project's own `-e .` entry; porter stages the
  project's source itself, through `source:`.

**`--frozen` is not optional.** A build that re-resolves is a build that ships a
dependency set the developer never ran. On a client with no network, the first
person to discover the difference cannot do anything about it.

**A lock that does not match its `pyproject.toml` is refused**, not silently
used. `uv lock --check` reports staleness without rewriting anything (verified:
it resolved peacock's 65 packages in 4 ms and left the tree clean). A stale lock
is a manifest describing something other than what will ship.

Scoping to a workspace member or a dependency subset uses uv's own flags —
`--package`, `--extra`, `--group`, `--only-group`, `--no-dev` — surfaced in
`porter.yaml` rather than reinvented. Dev dependencies never ship: `--no-dev` is
the default and turning it off is not a thing porter offers.

## The editable trap

**Measured 2026-08-10.** peacock's lock carries `prism` as an editable path
dependency. Installing that export as-is into a target tree produces:

```
__editable__.prism-0.3.1.pth
__editable___prism_0_3_1_finder.py
prism-0.3.1.dist-info
```

and the finder holds
`'/home/apiad/Workspace/repos/ainbox/packages/prism/prism'` — **the build
host's absolute path. prism's own source is nowhere in the tree.**

A `.deb` built from that installs cleanly on the client and dies at the first
import, pointing at a directory that exists on one laptop. This is the P1b venv
symlink and the rule-3 console-script shebang in a third costume: a build-host
absolute path, vendored by accident, invisible until the client.

**`--no-editable` is the fix**, verified on the same export: `-e ../../packages/prism`
becomes `../../packages/prism`, which uv builds into a real wheel and installs as
ordinary content. It is not optional and porter does not offer to turn it off.

**The guard must assert the importable module directory is present** — not glob
for `prism*`. `prism-0.3.1.dist-info` matches that glob and is precisely what
the *broken* tree contains, so an existence check passes on the failure it was
written to catch. Assert on what can be imported from the staged tree, with a
positive control proving the check goes red against an editable install.

## The interpreter version

`requires-python` is a **range**, and vendoring needs one interpreter.

**porter takes the lowest satisfying minor version, at its latest patch.**
`>=3.12` gives 3.12 at whatever 3.12.x uv installs. Lowest, because
`requires-python` is a floor claim the project is making about itself and the
package should not quietly demand more than the project says it needs. Latest
patch, because pinning a patch means never receiving a security fix.

- The resolved version is **recorded in the provenance stamp**, so a client
  fault report names the interpreter rather than the range.
- `python: {version: …}` in `porter.yaml` still overrides, for when a project's
  floor is not what should ship.
- **A project with no `requires-python` and no `python:` override is refused.**
  Defaulting silently is how a client gets an interpreter nobody chose.

## What remains in `porter.yaml`

Only what `pyproject.toml` has no way to say:

- **Deployment shape** — `kind:`, the unit's exec/`schedule:`/`after:`/`health:`
- **The config split** — `env:` and `admin_keys:`. This is deployment, not code:
  which values an operator owns is not a property of the Python package
- **The system boundary** — `native_binaries:`, the `<app>-desktop` split, and
  anything else Python's metadata cannot declare
- **Debian-specific naming** — when the package name must differ from the PyPI name
- **`bake:`, `migrations:`, `build_floor:`** — packaging concerns with no
  upstream equivalent

## Properties to prove

1. A component pointing at a project directory builds using that project's
   locked dependencies, with **no `requirements:` in `porter.yaml`**.
2. Two builds of the same commit, a week apart, produce the same dependency set.
   Reproducibility is the point of reading the lock.
3. A **path dependency's source is present and importable in the staged tree** —
   with a positive control proving the check fails against an editable install.
4. A **stale lock is refused**, naming the project.
5. A **workspace member** is exportable by name and packages only its own subset.
6. `[project.scripts]` yields a working `command` component whose wrapper is
   porter's `python -m`, and **no console script is staged**.
7. The interpreter version is the lowest satisfying minor, recorded in the
   provenance stamp, and overridable.
8. A project with no `requires-python` and no override is refused at parse.

## Refusals

Following the house rule — **refuse, never repair**:

- `requirements:` and a project directory declared together. Two sources of
  truth is the defect; picking one silently is worse than stopping.
- A missing or stale lock.
- An editable path dependency reaching the stage (belt and braces behind
  `--no-editable`: assert the tree, do not trust the flag).
- No resolvable interpreter version.
- A `[project.scripts]` entry porter cannot turn into a `module:function` target.

## Known limits

- **uv-managed projects only.** poetry, pdm, pip-tools and a bare
  `requirements.txt` are not read. porter's whole interpreter story is already
  uv-shaped; pretending otherwise would mean owning four resolvers.
- **Exporting needs the project's build backend for path dependencies.** uv
  builds a real wheel from the sibling source, which can need network at build
  time. That is fine — Docker and network are build dependencies, never client
  ones — but an airgapped *build* host cannot do it, and must be refused rather
  than silently skipped.
- **The lock pins Python dependencies, not system libraries.** `Depends:` still
  comes from ELF headers, and its cross-target correctness is the sibling
  spec's problem.
- **`--no-dev` means test dependencies never ship**, so a component whose
  runtime genuinely needs something declared as a dev dependency is a project
  whose `pyproject.toml` is wrong. porter says so rather than working around it.

## Out of scope

- **`.rpm` and other emitters.** Naming this constraint is the point: because
  the manifest now describes *intent* — a service, a schedule, a config split —
  rather than Debian specifics, an emitter for another format is a later
  addition and not a rewrite. Nothing here is built for it.
- **Non-uv Python projects**, per the limit above.
- **Publishing to an index.** porter packages for a client, not for PyPI.
- **The CLI surface** — `init`, `lint`, `check` — which this design makes
  substantially smaller and which is its own sibling spec.
