---
date: 2026-08-07
type: architecture
tags: [airgap, deployment, packaging, deb, porter]
status: design — approved in conversation 2026-08-07; implementation plan not yet written
supersedes: partially supersedes 2026-07-23-airgapped-appliance-pattern.md ("Docker is assumed")
---

# Unified airgapped installers: one `.deb` envelope, no container runtime at the client

*Reach for this when an app has to be handed to a client on a machine with no
internet. It replaces the four hand-built delivery paths in [[ainbox]],
[[transforma-cuba]], [[leyes-cuba]] and [[une-tools]] with one mechanism.*

## The problem

Four repos independently implemented the same pattern, and the cost shows up as
churn: **176 commits touching deploy paths since 2026-05-01** (ainbox 123,
une-tools 36, transforma-cuba 11, leyes-cuba 6).

`transforma-cuba/deploy/install.sh` and `leyes-cuba/deploy/install.sh` are the
same hundred lines with the nouns swapped — and they have already drifted:
leyes-cuba quotes its `.env` values and `chmod 600`s the file, transforma-cuba
does not. That fix never travelled back to its twin. The pattern was written down
([[2026-07-23-airgapped-appliance-pattern]]) but a checklist is not a program, so
each repo re-derives it by hand.

### Root cause: install, configure and update are the same script

This single conflation explains every symptom.

- **une-tools** updates by extracting a tarball *on top of* the installation. To
  survive that it grew `bin/_check-staged.sh` (rejects a package carrying `.env`,
  `bot.db`, `*.beaver`, `data/{db,kb,raw}/`), an `env.example`-never-`.env`
  contract pinned by `tests/test_release_contract.py`, a `reference/`-vs-`data/`
  overlay in `datapaths.py`, and `bin/smoke-update.sh`, which seeds eight state
  files and asserts they survive. **That is a hand-rolled dpkg conffile system
  with no file database.**
- **ainbox**'s `release/install.sh` is 970 lines and runs its beaver 1.x→2.x
  migration `ALWAYS, not just on update` (line 522) — because it cannot tell
  which one it is doing.
- `reference/` vs `data/`, `env.example` vs `.env`: `/usr/share` vs `/var/lib`
  vs `/etc`, re-derived from first principles, three times.

A package manager exists precisely to separate these three operations.

## The decision

**One `.deb` envelope. A vendored Python interpreter, not a container. Docker
remains a *build* dependency and disappears from the client entirely** — along
with `nvidia-container-toolkit`, the 92 MB `docker-offline-*.tar.gz` and the
8 MB nvidia-toolkit bundle that ainbox's kit ships today just to bootstrap the
thing that runs the thing.

Three claims that reversed during design, each on measurement rather than taste:

1. **Docker was the heavier prerequisite, not the lighter one.** It exists in the
   pattern doc to collapse the Python/native-library matrix. Pinning to
   Debian-family collapses that matrix anyway. une-tools' `sigere-api` is 13 MB
   native against leyes-cuba's 419 MB image for a comparable FastAPI-over-SQLite
   app.
2. **Going native makes the GPU story simpler.** `nvidia-container-toolkit`
   exists solely to inject host driver libraries into a container. Run natively
   and `llama-server` links `libcuda.so.1` from the host driver like any other
   program. The entire GPU-decision block at `release/install.sh:190-279`
   evaporates. The portability work is already done: the engine is built with
   `GGML_BACKEND_DL=ON`, `GGML_CPU_ALL_VARIANTS=ON`, `GGML_NATIVE=OFF` so one
   artefact serves Blackwell, Ampere and driverless CPU hosts — exactly the
   property a single `.deb` needs.
3. **AppImage is the wrong shape** and is rejected. No services, no `/etc`, no
   state directory, no update semantics without network zsync, and it assumes an
   unprivileged desktop user. The targets are VMs running systemd units on ports.
   The one thing it would buy — double-click launch — a `.deb` provides with a
   `.desktop` file, which ainbox's installer already hand-writes at line 868.

## Evidence

Every load-bearing claim below was measured on 2026-08-07. Probes live in
`.playground/porter-probe/` (throwaway; the findings are here).

| # | question | result |
|---|---|---|
| P1b | does a vendored-interpreter tree survive relocation? | **Yes — but only without a venv.** See below. |
| P6 | is Ubuntu 22.04 (glibc 2.35) a viable build floor? | **Yes.** One build ran on glibc 2.35 / 2.36 / 2.39 / 2.41 |
| P2/P2b/P2c | how should `/etc/<pkg>/env` survive an upgrade? | **Split it in two.** Single-file fails outright |
| P4/P4b | can `bwrap` replace the Docker sandbox? | **Yes — all six operations**, each behind a positive control |
| P5/P5b | does a 2 GB `.deb` work, and what does it cost? | **Yes.** Peak transient 2× payload, apt adds no cache cost |
| P7 | does an install complete with no TTY and stdin closed? | **Yes**, rc=0, no prompt, with a broken apt source and an edited conffile |
| P8/P8c | `DynamicUser` or a static user for client state? | **Static.** `/var/lib/private` is `700 root:root`; non-root cannot read or list |
| P9b | can a bundled browser relocate, and is `Depends:` derivable? | **Relocates** (runs from an arbitrary path); **`Depends:` derived automatically** — 24 packages from 32 sonames |

### P1b — no venv

`uv venv --relocatable` does **not** produce a relocatable tree. It writes
`home = /root/.local/share/uv/python/…` (the *build host's* cache) into
`pyvenv.cfg` even when `--python` points at a vendored copy beside it, and
`venv/bin/python` is an **absolute symlink** to that path. `--relocatable`
rewrites console scripts into `#!/bin/sh` wrappers; it does not touch that
symlink. Rewriting `pyvenv.cfg` does not help — the symlink is the broken thing.

What works: **no venv at all.** A vendored python-build-standalone interpreter
with packages installed into its own `site-packages`. Verified running from a
different path than it was built at, `--network none`, with no system
`python3.12`, importing `fastapi`, `uvicorn`, `pymssql` (bundled FreeTDS),
`pyogrio` (bundled GDAL) and `onnxruntime`.

Four build rules, each of which passes on a dev box and fails at a client:

1. **`cp -aL`** — uv's managed-python directory is a *symlink*; `cp -a` copies the
   link and vendors nothing.
2. **Delete `lib/python3.12/EXTERNALLY-MANAGED`** — uv's marker, added to protect
   its own cache. Removing it is the legitimate redistributor action.
3. **`--break-system-packages`** on the install.
4. **Invoke `python -m <module>`, never `bin/` console scripts** — their shebangs
   are absolute build paths. Free to comply with: that is how `ExecStart=` reads.

**Size:** interpreter + stdlib is **97 MB** before any dependency. Eight ainbox
services vendoring separately would duplicate ~780 MB of CPython, so the
interpreter is its own package that app packages `Depends:` on. That full probe
dep set is 337 MB (GDAL 80 MB, onnxruntime 53 MB, numpy 57 MB) — which is why
une-tools' per-component slimming took `sigere-api` from 140 MB to 13 MB. That
discipline carries over unchanged and matters more here, not less.

### P6 — the build floor

Built once on Ubuntu 22.04 (glibc 2.35), the tree imports all five native
extensions on Ubuntu 22.04 (2.35), Debian 12 (2.36), Ubuntu 24.04 (2.39) and
Debian 13 (2.41) — each `--network none`, no system `python3.12`, and each
asserting the build-host interpreter path is *absent* before running.

**Ubuntu 22.04 is the declared build floor.** It is also what ainbox's CUDA 12.8
base already forces for the engine, so every package shares one build base.

### P5b — scale and the update path

2 GB packages, built with `-Znone` (high-entropy model weights and compiled libs
do not compress; xz burns minutes to save nothing):

| | `dpkg -i` | `apt` from a `file:` repo |
|---|---|---|
| install | rc=0, 7 s, 2048 MB | rc=0, 2048 MB |
| upgrade | rc=0, 8 s | rc=0, old payload removed, version 1.0 → 2.0 |
| **peak transient** | **2× payload** | **2× payload** |
| `/var/cache/apt/archives` | n/a | **1 MB** |

**apt does not cache packages from a `file:` repo** — it installs in place. The
USB path costs the same as `dpkg -i`. A 2 GB package needs ~4 GB free to upgrade;
that is a stated precondition, not a redesign.

## Architecture

### The FHS contract

```
/usr/lib/<pkg>/               app code, native binaries           package-owned
/usr/lib/<pkg>/python/         the vendored interpreter (bundled)  package-owned
/usr/lib/<interp-pkg>/python/  the vendored interpreter (shared)   its OWN package
/usr/share/<pkg>/             baked data, models, static assets   package-owned
/etc/<pkg>/defaults           package-owned config       conffile
/etc/<pkg>/env                admin-owned config         NEVER shipped in the .deb
/var/lib/<pkg>/               client state   750 root:<pkg>  package NEVER writes here
/usr/lib/systemd/system/      units                      package-owned
```

**Interpreter packaging is a per-project decision, declared in `porter.yaml`.**
porter hardcodes neither a name nor a version — it ships whatever interpreter the
project asks for, under whatever package name the project chooses.

```yaml
python:
  version: "3.12"       # whatever this project needs
  package: bundled      # interpreter lives inside each component's own package
# or
python:
  version: "3.13"
  package: une-python313   # emitted as its own .deb; components Depends: on it
```

`bundled` is the default and the simpler artifact: one package, nothing to
resolve. Reach for `shared` only when two or more components would otherwise
each carry their own copy — the interpreter is 97 MB before any dependency, so
eight ainbox services bundling separately duplicate ~780 MB. Version-qualifying
the chosen name lets two interpreter generations coexist when one component
needs a different one.

There is no `syalia-`-prefixed anything in porter. A vendor prefix belongs to the
project that sets it.

This is une-tools' `reference/`-vs-`data/` split enforced by dpkg's file database
instead of by a guard script. Two hand-built things become generic: `_check-staged.sh`
becomes a build-time lint every component gets, and `smoke-update.sh` becomes the
generic upgrade gate.

### Config: two files, two owners

The defect in every single-file scheme is that one file has two owners. Measured:

| mechanism | admin untouched | admin edited |
|---|---|---|
| conffile, bare `dpkg -i` | ok, new key delivered | **rc=1**, upgrade fails, stray `.dpkg-new` |
| conffile, `--force-confold` | ok, new key delivered | ok, **no new key**, stray file |
| `postinst` copy-if-absent | ok, **never delivers a new key** | ok, no new key |
| **split (chosen)** | **ok** | **ok, edit kept, new key delivered** |

`DEBIAN_FRONTEND=noninteractive` does **not** suppress dpkg's conffile prompt —
that governs debconf only. So a single conffile would hang or fail exactly the
unattended upgrade this design promises.

```ini
EnvironmentFile=/etc/<pkg>/defaults
EnvironmentFile=-/etc/<pkg>/env      # admin last, wins
```

Verified through a real systemd unit with `/etc/<pkg>/env` at mode 600 root
while the service ran unprivileged — systemd reads `EnvironmentFile` as root
*before* dropping privileges, so a secret the service user cannot itself read
still reaches its environment. Hardening and secrets do not collide.

### Install ≠ configure ≠ update

- **`dpkg` installs bits.** `postinst` is strictly non-interactive: create the
  service user, `systemctl enable`, write `/etc/<pkg>/env` only if absent. It
  never asks a question, so it works unattended, over a dropped SSH session, and
  inside the gate.
- **`<app>-setup` configures the instance.** A first-run wizard the package
  ships — LLM endpoint picker, admin bootstrap, token issuance. Interactive,
  idempotent, re-runnable. Every good thing in today's `install.sh` lives here,
  entangled with nothing.
- **Migrations run exactly once**, via the standard idiom:
  ```sh
  if [ "$1" = configure ] && dpkg --compare-versions "$2" lt-nl 2.0; then
      migrate_beaver_1_to_2
  fi
  ```

**Operator-facing consequence: install and update are the same command, forever.**
dpkg knows which it is; the sysadmin does not have to.

### systemd replaces compose

Services become units; `depends_on` becomes `After=`/`Requires=`; volumes become
`/var/lib/<pkg>/`; `mem_limit`/`nano_cpus` become `MemoryMax=`/`CPUQuota=` (the
same cgroups); container isolation becomes `ProtectSystem=strict`, `PrivateTmp=`,
`NoNewPrivileges=`.

**Not `DynamicUser=yes`.** It looks like free hardening and is wrong for this
product. Measured 2026-08-07: it allocates a fresh UID per boot and redirects
`StateDirectory` to `/var/lib/private/<pkg>`, which is `700 root:root` — a
non-root operator can neither read nor list client state, so backups, monitoring
and support all require root. It also rewrites the ownership of admin-dropped
files as the UID rotates, which makes "the client owns `/var/lib/<pkg>`" false.
Services run as a **static system user** created in `postinst`
(`useradd --system --no-create-home --shell /usr/sbin/nologin <pkg>`), with state
at a real `/var/lib/<pkg>` mode `750 root:<pkg>`. That layout passed every
non-root access check the private one failed.

This matters concretely: UNE's sysadmin curates `data/kb/` and drops in their own
`deficit.csv` for the service to read. State the service can see but the operator
cannot is not a hardening win. The `network_mode: host` / container-localhost
trap (#5 in the pattern doc) disappears entirely — everything is on the host's
loopback.

**Stated behavioural change:** compose's `depends_on: condition: service_healthy`
has no systemd equivalent. Nine ainbox services converge via `Restart=on-failure`
rather than health-gated sequencing. This is how every distro runs interdependent
services and is arguably more robust than a one-shot gate, but it is a real
change and it is a decision, not an oversight.

### Autonomous installation

**The installer must complete with nobody watching.** That is a requirement, not
a mode: there is no interactive path to fall back to on an airgapped client.

- **`--yes` is the contract, and it accepts everything.** `install.sh` reaches no
  prompt by construction, verified under `setsid` with stdin closed.
- **Privilege: `sudo -n`, or an explicit refusal.** Not root and passwordless
  sudo unavailable → exit with the reason. It never blocks on a password, because
  a hidden password prompt in an unattended run is indistinguishable from a hang.
- **Environment guards that are real:** `DEBIAN_FRONTEND=noninteractive`,
  `NEEDRESTART_MODE=a`, `UCF_FORCE_CONFOLD=1`, `-o Dpkg::Use-Pty=0`, and
  `--force-confold`/`--force-confdef` as defence in depth for dependencies porter
  does not control. **`NEEDRESTART_SUSPEND` is not a real variable** — it is
  absent from needrestart 3.6's shipped code (measured) and must not be
  reintroduced.
- **`apt-get update` is scoped to our own list file**
  (`-o Dir::Etc::sourcelist=... -o Dir::Etc::sourceparts=-`). A client with a
  stale or unreachable network source in `sources.list.d/` must not be able to
  break an offline install. Verified with a deliberately broken source present.
- **needrestart is structurally unreachable**, not merely suppressed: porter
  packages upgrade no shared system libraries — they vendor their dependencies
  and `Depends:` only on other packages porter itself produced — so the "which
  services should be restarted" dialog has nothing to trigger it.
- **`postinst` restarts the service itself** (`systemctl try-restart`), so an
  upgrade needs no follow-up command.

### Near-native desktop (optional)

A web app served on localhost can present as a desktop application:
`chromium --app=http://127.0.0.1:PORT` gives a window with no tabs and no URL
bar, resizable, with its own taskbar identity via `--class=`.

**This is the one place the "nothing but glibc and systemd" rule bends**, and it
bends openly: any GUI browser needs the client's desktop stack — GTK, X11 or
Wayland, NSS, dbus. That is acceptable *because it is exactly the machine that
has a desktop*, but it must never contaminate a headless install.

So it is always a **separate package**, `<app>-desktop`, depending on `<app>`
plus the desktop libraries. On an airgapped box apt cannot fetch a missing GTK,
so a desktop dependency inside the core package would be fatal on a server. As
its own package, a missing desktop stack refuses an optional component instead
of failing the install.

**Launcher-first, bundle-optional.** Most desktop clients already have a browser,
so the default emits only a launcher that probes `google-chrome`, `chromium`,
`brave-browser`, `microsoft-edge` in order, uses `--app=` on the first hit, and
falls back to `xdg-open`. Bundling is for the two cases that defeat it: a desktop
with no browser at all, and an app whose UI needs a **pinned engine version** —
a real risk for a modern SPA against an ancient ESR on a client box, and the kind
of failure you end up debugging remotely.

**porter hardcodes no browser vendor, exactly as it hardcodes no interpreter.**
The supply is declared per project, as a URL and a checksum:

```yaml
desktop:
  name: AInBox
  url: http://127.0.0.1:8080
  icon: assets/ainbox.png
  wait_for_health: /health        # poll before opening, so a click right after
                                  # login does not show connection-refused
  browser: system                 # probe what the client has (default)
# or
  browser:
    source: https://.../chromium-linux64.tar.xz
    sha256: "..."
```

Chromium (BSD, freely redistributable) is the chosen engine. The exact build to
pin is an open item — Google publishes no stable Linux tarball for Chromium
proper, so the artifact comes from a third-party portable build and **must be
verified against its terms before it ships**, not assumed from this document.

Concretely: the tree used to validate the mechanism was Playwright's, and it
identifies itself as **`Google Chrome for Testing 149.0.7827.55`** — not Chromium.
It was a fine measurement subject and is *not* a shippable artifact, which is
exactly the confusion this paragraph exists to prevent.

Near-native is more than chromelessness: a `.desktop` entry and icon so it
appears in the app menu, an isolated profile under
`~/.local/share/<pkg>/browser-profile` so it never shares cookies or history with
the user's own browser, and `--no-first-run --no-default-browser-check`.

### `Depends:` is derived, never hand-written

Whenever porter bundles a native binary — a browser, `llama-server`, a Go or Rust
executable — it reads every ELF's `NEEDED` entries with `objdump -p`, resolves
the sonames through `ldconfig -p`, and maps them to owning packages with a single
`dpkg -S`, on the **target** distro, at build time.

Measured against a 380 MB Chromium tree: 6 ELF objects, 32 sonames, **24
packages** — `libnss3`, `libgbm1`, `libcups2t64`, `libatk-bridge2.0-0t64`,
`libxkbcommon0` and the rest of the GTK/X11 set nobody would enumerate correctly
by hand.

**Use `objdump`, not `ldd`.** `ldd` executes the dynamic loader once per file; run
over a browser tree it took long enough to look like a deadlock (it hung a probe
and had to be killed). `objdump` reads headers and returns in seconds.

A hand-maintained dependency list is precisely how a package installs cleanly and
then fails to open a window, and it goes stale silently the first time an
upstream build links something new. Deriving it also means the same mechanism
covers every future native payload without anyone remembering to update a list.

### The sandbox: bwrap, not Docker

`apps/sandbox` is the only place Docker is the *product*. Its API surface is six
operations, all covered (each verified behind a positive control proving the
probe detects the thing when isolation is off):

| today | native |
|---|---|
| `containers.run(image)` | rootfs from `docker export` at build time + `--bind` |
| `exec_run` | `bwrap … /bin/sh -c` |
| `put_archive` / `get_archive` | plain file copy into the bound workspace |
| private network, egress off | `--unshare-net` — host `127.0.0.1` unreachable, loopback only |
| `mem_limit="1g"` | `systemd-run --scope -p MemoryMax=` |
| container IP for preview | **changes shape**: preview-by-port, touching `main.py`'s proxy |

### GPU: nothing to install

No `nvidia-container-toolkit`. `llama-server` links the host driver directly.
The three CUDA math libraries currently hand-copied out of the devel image
(`libcublas`, `libcublasLt`, `libcudart`) plus `libnccl` ship as package payload
exactly as they ship as image layers today.

## `porter` — the tool

*(Name provisional; it is one string in a config file.)*

A small repo whose CLI reads a `porter.yaml` per repo and emits a signed local
apt repo plus a USB tree. Declarative by default; **any component may declare
`build: <script>`** and take over the *assemble* stage while still getting FHS
layout, lint, packaging, the gate and the repo for free. Without that escape
hatch the tool becomes a second thing to fight; without the shared parts, the
copy-paste drift returns.

**The `porter.yaml` schema is deliberately not specified here.** It should be
derived from the four real repos during the first migration rather than invented
up front — the fields that matter are the ones `sigere-api`, then
transforma-cuba, then ainbox actually need. Fixing a schema before the first
consumer exists is how the escape hatch becomes the default path.

### Pipeline

1. **bake** — repo-provided hook: ETL, frontend build, manual render. WAL
   checkpointing and provenance stamping live here (see the pattern doc).
2. **assemble** — on the glibc floor (Ubuntu 22.04) in a container: vendored
   interpreter, app code, native binaries, baked data, FHS layout.
3. **lint** — nothing in client-owned paths; no `.env`, `.beaver`, `.git`,
   `__pycache__`; every key declared in `porter.yaml` present in `defaults`.
4. **package** — `dpkg-deb -Znone --build --root-owner-group`. No debhelper: a
   hand-written `DEBIAN/{control,conffiles,postinst,prerm,postrm}` is ~40 lines.
5. **gate** — below.
6. **publish** — flat repo index generated from `dpkg-deb --field` + `stat` +
   `sha256sum` (**not** `dpkg-scanpackages`: it lives in `dpkg-dev`, which is
   absent on demos and on the Debian base image, and the index is six lines to
   emit), plus `Release` and a detached signature.

### The gate, and the rule behind it

Install the **actual `.deb`** into a clean `systemd-nspawn --private-network
--ephemeral` of each target distro, start the units, exercise the endpoints; then
install v_prev, seed client state, upgrade, and assert it survived byte-identical.

**Every assertion carries a positive control or a magnitude check.** This is not
a style preference. During this design, five probes reported passes that were
false — a dangling symlink resolving to the build host's own interpreter, a
network probe using bash-only `/dev/tcp` under dash, an interface count where the
binary was absent, a memory-limit check satisfied by *command not found*, and a
truncated 12 KB package reported as built. Each was caught only because something
downstream contradicted it. The airgap failures that matter are exactly the ones
that look like passes on the build host.

## Delivery and update

```
<vendor>-<app>-<version>/
  repo/{Packages,Packages.gz,Release,Release.gpg,*.deb}
  install.sh        # adds the repo + key, apt install, then <app>-setup
  README.txt
```

Because config is split, no conffile can ever conflict, so no `--force-confold`
dance is required. Rollback is keeping the previous `.deb` in the repo.

**Multi-machine deployments become metapackages.** UNE's two boxes:
`une-tecnologica` → `Depends: une-sigere-api, une-scheduler`; `une-corporativa`
→ `Depends: une-directivos`. One name per machine; dpkg resolves the rest from
the same USB. See [[reference_une_despliegue_dos_maquinas]].

## What this retires

- transforma-cuba + leyes-cuba: both copies of the installer, and their drift.
- une-tools: `_check-staged.sh`, `smoke-update.sh`, the `env.example` contract and
  its test, the `reference/`-vs-`data/` overlay, four hand-written `check-env`
  Makefiles.
- ainbox: the 970-line `install.sh`, `install-docker.sh` (11 KB),
  `install-nvidia-toolkit.sh` (6.7 KB), `docker-offline-29.7.1.tar.gz` (92 MB),
  `nvidia-toolkit-offline-1.19.1.tar.gz` (8 MB), the GPU-decision block, and the
  unconditional inline beaver migration.
- The entire "which Docker / compose / nvidia-ctk version does the client have"
  surface.

## Open risks

Named rather than resolved, with the evidence that exists:

- **`slirp4netns` egress is unproven.** Only the isolation direction was tested
  (host `127.0.0.1` unreachable from the sandbox netns — the security-relevant
  half). The "egress on, but cannot see ainbox services" case is the fiddly one.
  Scoped inside the sandbox rewrite.
- **GPU-native `libcuda` linking is untested on real hardware.** Strongly implied
  by `GGML_BACKEND_DL` and by native being the *ordinary* way llama.cpp runs, but
  smaug sits behind the UH proxy at ~700 KB/s and the trade was judged poor
  against the residual risk.
- **Node runtime unproven.** JS ships as pre-built static assets today
  (une-tools already does this) so no runtime is needed; if one ever is, Node's
  official static Linux tarballs take the same vendoring path as CPython.
- **The 2 GB figure is synthetic.** Measured with `/dev/urandom` payloads, not
  the real engine image.
- **needrestart's interactive dialog was never reproduced.** Its apt hook is
  live on Ubuntu 24.04 and `NEEDRESTART_MODE` is present in the shipped code, but
  a container has no outdated daemons, so the prompt path never fired. The
  structural argument above (we upgrade no shared libraries) is what the design
  actually rests on; the env var is belt and braces. Re-test on a real Ubuntu
  host when one is available.
- **Rendering from the relocated tree is unproven.** The binary runs and reports
  its version from an arbitrary path, but the headless `--dump-dom` check against
  a local page failed for reasons not chased down. Relocation of the *executable*
  is established; rendering is not.
- **The chromeless window has never been looked at.** Chromium ignores unknown
  flags silently, so "the flags were accepted" is weak evidence. Whether `--app=`
  plus `--class=` actually yields a window that reads as native needs a display
  and a human, and that check has not been done.
- **The Chromium build to ship is unpinned.** Chromium is BSD and redistributable,
  but the concrete portable Linux artifact and its terms must be verified before
  slice 3 rather than inferred.
- **`sudo -n` refuses rather than prompting**, by choice. A sysadmin running
  interactively as a normal user with password-gated sudo gets an explicit error,
  not a password prompt.

## Migration order

Thinnest end-to-end slice first, hardest client first: **une-tools `sigere-api`**.
It is already native (13 MB relocatable tree), already has an update contract and
an update smoke test, and ships to a real client on a real schedule — so
converting it proves conffile handling, apt-repo update and systemd against the
hardest real customer with the smallest payload, and retires the most hand-rolled
machinery per line of code. Then transforma-cuba/leyes-cuba (one shared shape,
two consumers), then une-tools' remaining components, then ainbox last — it needs
the sandbox rewrite and is the only one that gains a new subsystem rather than
losing one.

## See also

- [[2026-07-23-airgapped-appliance-pattern]] — the predecessor. Its WAL-checkpoint,
  provenance-stamp and prove-it-offline disciplines survive intact; its "Docker is
  assumed" section is superseded here.
- [[reference_une_release_airgapped_tres_componentes]], [[reference_une_delivery_route]]
- [[ainbox]], [[une-tools]], [[transforma-cuba]], [[leyes-cuba]], [[geocoder]]
