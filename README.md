# porter

Airgapped `.deb` installers for Debian-family clients. One command builds a
signed local apt repository onto a USB tree; the client runs one command to
install it, and the same command to update it — dpkg knows which, so the
sysadmin does not have to.

**Nothing is inherited from the client OS except glibc, systemd and — optionally
— the NVIDIA driver.** No Docker, no container runtime, no
`nvidia-container-toolkit`, no system Python. The interpreter is vendored and
native binaries are built on a glibc floor, so one artifact runs on Ubuntu 22.04
through Debian 13. Docker is a *build* dependency and never a client one.

## Install

```bash
uv tool install git+https://github.com/syalia-srl/porter        # or
uv sync --extra dev                                             # in a checkout
```

Build host needs: `uv`, `dpkg-deb`, `objdump` (binutils), `docker` (for the
gate), `systemd-analyze` and `systemd-nspawn` (for the nspawn gate), and a C
compiler if a manifest declares `native_binaries:`. The client needs none of
them.

## The flow, end to end

### 1. Describe the component — `porter.yaml`

The smallest useful manifest is a service. Copy `examples/service-fastapi/`
rather than reading a schema; the gallery *is* the schema.

```yaml
package: demo-app
version: "1.0"
description: porter's example FastAPI service
kind: service
source: ["src/app.py"]
python: {version: "3.12", package: bundled}
requirements: ["fastapi", "uvicorn"]
exec: {module: uvicorn, args: ["app:app", "--host", "127.0.0.1", "--port", "8099"]}
```

### 2. Build the `.deb`s — `porter build`

```bash
porter build examples/service-fastapi/porter.yaml --out dist
```

One `.deb` per component and one per metapackage. `--stage` (default `build`) is
scratch that porter removes again; `--out` (default `dist`) is where the
packages land. `porter build --help` lists both. The stage is assembled on the
glibc floor, linted against the FHS contract, and `Depends:` is derived from the
ELF headers of everything staged — never hand-written.

### 3. Lay out the USB tree — `porter.repo`

```python
from pathlib import Path
from porter.repo import usb_tree

usb_tree([Path("dist/demo-app_1.0_amd64.deb")],
         out=Path("/media/usb/demo-app-1.0"),
         app="demo-app",
         readme="Run: sudo bash install.sh",
         sign_key="releases@example.com")   # omit for an unsigned dev tree
```

That writes `repo/{Packages,Packages.gz,Release[,Release.gpg,*.asc],*.deb}`,
an `install.sh` and a `README.txt`. Signing mode is decided here, at build time,
and baked into `install.sh` — a script that could fall back to `[trusted=yes]`
would turn a key file that failed to copy into an install that verifies nothing
and exits 0.

### 4. Prove the bundle before it ships — `porter.gate`

```python
from porter.gate import gate, nspawn_gate, nspawn_root

r = gate(usb, app="demo-app", image="debian:bookworm-slim",
         health_url="http://127.0.0.1:8099/health",
         seed={"/var/lib/demo-app/client.db": "..."})
assert r.ok, r.failures
```

`gate()` installs v_prev into a networkless container with no system Python,
lets the client seed its own state, upgrades with the *same* command, and
asserts what survived. `nspawn_gate()` is the other half: it boots the target
rootfs with systemd as PID 1 and **starts the unit**, which is the only way to
learn that `User=`, `StateDirectory=` and `ProtectSystem=strict` compose with a
program that actually runs.

Both need ≥2 versions in the index — an upgrade is the thing being gated.

### 5. The client — one command, twice

```bash
sudo bash /media/usb/demo-app-1.0/install.sh              # install
sudo bash /media/usb/demo-app-1.0/install.sh              # update, later
sudo bash /media/usb/demo-app-1.0/install.sh --version 1.0   # pin
```

No prompt is reachable from that script. `sudo -n` or an explicit refusal, never
a password prompt; `DEBIAN_FRONTEND=noninteractive`, `NEEDRESTART_MODE=a`,
`UCF_FORCE_CONFOLD=1`; and the config split means no conffile can conflict, so
no `--force-confold` dance is required.

**In 0.1.0 the CLI exposes `build` only.** Steps 3 and 4 are the library API,
driven by `tests/test_repo.py`, `test_signing.py`, `test_gate.py` and
`test_nspawn_gate.py`. They are not CLI verbs yet because the inputs they need
(a readme, a health URL, a seed set, a gate image) are not manifest fields, and
by the gallery rule a field with no example exercising it does not exist. See
*Known limits* in [`CHANGELOG.md`](CHANGELOG.md).

## The gallery

Eleven examples, each proving one shape. An adopter starts by copying the row
that matches theirs; if no row matches, the gap is porter's to close.

| Example | The shape it proves |
|---|---|
| `service-fastapi` | The base case: a headless HTTP service with a systemd unit and split config. A client with no Python, no Docker and no network installs one `.deb` and gets a service — then upgrades without losing its own configuration. |
| `command` | A CLI tool and nothing else: a binary on PATH, no unit, no `/etc`, no state, no system user. |
| `oneshot-timer` | A scheduled job. `Type=oneshot` plus a `.timer`, with the **timer** enabled rather than the service — the three failure modes here are each silent. |
| `stateful-service` | Client-owned state across an upgrade, and a migration keyed on dpkg's `$2` so it runs exactly once. Ships two manifests (1.9 and 1.10) so the upgrade itself is readable without opening a test. |
| `baked-data` | A payload that does not exist until the bake stage builds it. WAL-checkpointed and magnitude-asserted: rows committed without a checkpoint live in `<db>-wal`, so packaging `<db>` alone ships an empty database at rc=0. |
| `multi-service` | Several components from one manifest, with `after:` ordering. systemd has no `condition: service_healthy`, so the dependent restarts until its upstream answers — booted under real systemd to prove it converges. |
| `suite` | One name per machine role. `metapackages:` turns six components split across a technical and a corporate box into a single `apt install` per box, resolved from the same USB. |
| `shared-interpreter` | One 97 MB interpreter package that every component `Depends:` on by exact CPython version, instead of one vendored tree per component (~780 MB saved across ten services). |
| `native-binary` | Compiled payload. `Depends:` derived from ELF headers — the library the client provides is named, the one the payload ships is not — and a binary linking a soname nothing provides is refused before anything is staged. |
| `desktop-app` | One manifest, **two** packages. The GUI's GTK/X11/NSS dependencies live in `<app>-desktop` so they can never block a headless install on a box whose apt has no network. |
| `custom-build` | The escape hatch. `build:` names a script that writes the whole staged tree; porter's assembler never runs, and the package still gets FHS layout, lint, packaging, the gate and the repo. Here it packages a component with no Python in it at all. |

## Running the tests

```bash
PORTER_REQUIRE_UV=1 PORTER_REQUIRE_DOCKER=1 PORTER_REQUIRE_SYSTEMD=1 \
  PORTER_REQUIRE_CC=1 PORTER_REQUIRE_NSPAWN=1 uv run --extra dev pytest
```

Those five variables turn a *skip* into a failure. Without them a host missing
`uv`, docker, `systemd-analyze`, a C compiler or `systemd-nspawn` silently skips
most of the suite and pytest still exits 0 — a green badge over a run that
verified almost nothing, which is the exact failure porter exists to prevent.
They are off by default so a contributor gets skips rather than a wall of
failures, and armed everywhere a green result is treated as evidence.

`scripts/reverify-guards.sh` is the other half: it disables each guard at its use
site and requires the suite to go red. A guard with no entry there is unverified
by the only test that matters.

## Where to read next

- [`AGENTS.md`](AGENTS.md) — the door: the rules that are not negotiable, and an
  index into `know-how/`.
- [`docs/design-spec.md`](docs/design-spec.md) — the design, with the
  measurement behind each decision.
- [`CHANGELOG.md`](CHANGELOG.md) — what 0.1.0 is, what it is not, and the known
  limits.
