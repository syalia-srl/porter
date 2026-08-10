---
date: 2026-08-10
type: design
status: approved in conversation 2026-08-10; implementation plan not yet written
extends: docs/design-spec.md
sibling: docs/2026-08-10-removal-path-design.md
---

# The build floor: where a package is built, and proving it installs where it must

*Reach for this when changing `Depends:` derivation, adding a compiled payload,
or asking whether porter needs per-distro builds. It extends
`docs/design-spec.md`, whose "build on the floor" rule is currently enforced by
nothing.*

## The question that prompted this

*"Do we need a build matrix — one artifact per target distro — so `Depends:`
names the packages the client actually has?"*

**No.** But the fear behind the question is real, and it is **two** fears wearing
one coat. They have different causes, different blast radii and different fixes,
and conflating them produces a matrix that is expensive and still does not
close the gap.

| Fear | Cause | Fix | Who needs it |
|---|---|---|---|
| `Depends:` names a package the client's apt never heard of | Package **names** differ across releases (`t64`) | Cross-target **resolve check** | Any package with derived deps |
| The payload's compiled code will not run on the client | **glibc** and linked sonames differ | Build compiled payload on the **floor** | Only `native_binaries:` / compiling `build:` hooks |

Separating them is the whole design. The rest of this document is the two
mechanisms and the boundary between them.

## Why a matrix is the wrong shape

A porter package's payload is distro-independent by construction: the
interpreter is vendored, nothing is inherited from the client but glibc,
systemd and optionally the NVIDIA driver. CI's `glibc-floor` job already vendors
on 22.04 and *executes* on ubuntu 22.04, debian 12, ubuntu 24.04 and debian 13
on every push.

And the dependency surface is tiny. `tests/test_depends.py` records the
measurement: a bundled-interpreter service is **4 ELF objects → 9 sonames → two
packages**, `libc6` and `libcrypt1`, both spelled identically from 22.04 through
26.04.

So per-distro builds of a typical component would emit byte-identical payloads
carrying an identical `Depends:`. The matrix multiplies artifacts — and every
artifact is a thing to store on a stick, name, track and get wrong — without
changing a single field.

The surface only grows for `native_binaries:` (ainbox's `llama-server` plus its
CUDA libraries) and the `<app>-desktop` split (GTK/X11/NSS from the client):
roughly 24 packages from 32 sonames for a Chromium tree. That is where `t64`
lives, and it is a minority of components rather than the default case.

## The hole this actually found

**`build_floor:` does not exist.**

The slice-1 plan put `build_floor: ubuntu:22.04` at the top of every example
manifest. It never landed. `spec.py` refuses unknown top-level keys, so no
manifest carries it today, and **`porter build` runs on the host, never in a
container.**

So the rule the entire `Depends:` story rests on — *build on the floor* — is
enforced by discipline alone. zion is 26.04. Building ainbox's desktop or
native-binary packages there today derives 26.04 package names, writes them into
a control file, and ships them to a 24.04 client. The build is green, the
package installs or fails at the client, and nothing between those two moments
says a word.

This is the same failure class as the bug fixed in `e02d1e1`, where `ldconfig -p`
and dpkg disagreed about `/lib` versus `/usr/lib` and porter refused every
package on the two releases in between. That one was caught because CI ran on a
different release than zion — accidentally, not by design. The standing warning
in `AGENTS.md` says it outright: *zion is 26.04 and `ubuntu-latest` is 24.04, so
a green local suite is not evidence about the runner.*

## Mechanism 1: the cross-target resolve check

**The insight that shrinks this whole problem: if the derived names resolve on
every target, it does not matter which host derived them.** Verification
subsumes the floor discipline for names. A build matrix tries to make the
derivation environment match the client; a resolve check just asks the client's
package database the question directly, which is both cheaper and stronger.

`resolve_check(deb, targets) -> ResolveResult` takes a **built** `.deb` and, for
each target image, asserts that every name in its `Depends:` exists and is
installable there. `apt-get install --simulate` against the real package
database of that release — no payload is unpacked, nothing is downloaded, and
the answer is apt's own.

**Properties to prove:**

1. A package whose `Depends:` resolve on all targets passes.
2. A package naming a package that exists on none of them is **refused, naming
   the target and the unresolvable name**. Not a warning: a refused build is the
   honest outcome, and it is the one that does not reach a client.
3. A package naming something present on *some* targets is refused **for those
   it fails on, naming them** — that is the `t64` shape and the shape of the
   `e02d1e1` bug, and a check that collapses it to one boolean loses the
   information the operator needs.
4. **Positive control: the check must detect a name that cannot resolve.** Inject
   a fabricated dependency and assert the check goes red. Per the gate rule, a
   probe that has never been shown to detect the thing proves nothing about a
   package that passes it — and this probe is exactly the kind that silently
   passes when the target has no package lists at all.

**Where it runs.** In `gate()`, so an adopter gets it on their own bundle, and in
CI over the gallery. Targets default to the four the `glibc-floor` job already
uses (ubuntu 22.04, debian 12, ubuntu 24.04, debian 13) and are overridable.

**A component with no derived `Depends:` skips it trivially and is not an
error** — commands and metapackages carry nothing to resolve.

## Mechanism 2: `build_floor:`, for compiled payloads only

`build_floor:` becomes a real top-level manifest key naming an image
(`ubuntu:22.04`). It governs **where compiled payload is produced**, and nothing
else.

**It is required only for components that compile something**: those declaring
`native_binaries:`, or a `build:` hook that invokes a compiler. For everything
else the payload is a vendored interpreter and Python source — already
floor-independent, already proven across 2.35 → 2.41 on every push — and forcing
those builds through Docker would buy nothing and cost the dev loop.

**Properties to prove:**

1. A component with `native_binaries:` **builds inside the declared floor image**,
   not on the host.
2. A component with neither `native_binaries:` nor a compiling `build:` hook
   builds on the host as it does today. No change, no container, no slowdown.
3. Every built package **records the distro it was built on** in its provenance
   stamp — `usr/share/<pkg>/VERSION`, which `bake` already writes. A client fault
   report should name the build environment, not merely the commit.
4. A manifest declaring `native_binaries:` **with no `build_floor:` is refused**
   at parse. The failure this exists to prevent is silent; the refusal must not
   be.
5. Docker remains a **build** dependency and never a client one. This mechanism
   must not add a single thing to what a client needs.

## Testing

- **`tests/test_resolve.py`** — the four resolve-check properties, including
  the fabricated-dependency positive control, against real target images.
  Marked `docker`.
- **`tests/test_build_floor.py`** — parse-level refusals (property 4), the
  host-build regression (property 2), and the provenance stamp (property 3).
- **Guard entries in `scripts/reverify-guards.sh`** for each new refusal: the
  unresolvable-name refusal, the per-target reporting, and the
  `native_binaries:`-without-`build_floor:` parse refusal. A guard with no entry
  is unverified by the only test that matters.

## Known limits

- **The resolve check needs network at build time.** Target images ship with
  their apt lists cleaned, so the check runs `apt-get update` in each container.
  That is fine — Docker is already a build dependency and the build host is not
  the airgapped machine — but it means the check cannot run on an airgapped
  *build* host, and it must **refuse rather than skip** when it cannot reach a
  target. A resolve check that silently passes because it could not ask is the
  precise failure the gate rule exists to prevent.
- **`--simulate` proves the name resolves, not that the library is ABI-compatible.**
  A package can be installable and still be the wrong version for a binary
  compiled against a newer soname. That is mechanism 2's job, and the two are
  orthogonal.
- **Targets are a list porter is told, not a list it discovers.** A client on a
  release nobody put in the list is unverified, and will stay that way silently.
  The default four are the ones the glibc floor already claims.

## Refused by design

- **No per-distro artifacts.** If mechanism 1 ever reports that one target
  genuinely needs different names, the payload is still identical and only the
  control file differs — that is a *re-control*, not a rebuild, and it is a
  smaller feature than a matrix. It waits until a real divergence is measured,
  rather than being built against a hypothetical one.
- **`build_floor:` does not become mandatory for every component.** Requiring
  Docker for every `porter build` would make the common case — a vendored
  interpreter and some Python — pay a container for a guarantee it already has
  from the glibc floor job.

## Out of scope

- **Cross-architecture builds.** Everything here is `amd64`. arm64 is a
  different design and needs its own floor.
- **Choosing the floor.** Ubuntu 22.04 stays the floor for the reason the spec
  gives; this document makes the floor *declarable and enforced*, not different.
- **The `Depends:` derivation algorithm itself.** `objdump` + `ldconfig -p` +
  `dpkg -S` in both usr-merged spellings is settled and is not reopened here.
