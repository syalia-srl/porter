# porter Slice 1 — the example gallery

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** porter builds, gates and publishes a gallery of in-repo example projects covering every packaging shape we care about — a FastAPI service, a plain command, a scheduled oneshot, a multi-component suite, and a near-native desktop app — each installable and upgradable on an airgapped Debian-family box with no Docker and no system Python.

**Architecture:** Seven tasks, vertically sliced. Task 1 vendors a relocatable CPython; Task 2 turns a staged tree into a `.deb`; **Task 3 is the end-to-end moment** — `examples/service-fastapi` answering HTTP from an installed package under a real systemd unit; Task 4 adds the USB apt repo and the upgrade path; Task 5 adds the gate; Task 6 fills in the rest of the gallery; Task 7 adds the desktop shape and the derived-`Depends:` mechanism it needs.

**Why examples before a real app.** The gallery is a shipped feature, not scaffolding: it is porter's documentation *and* its regression suite. Every example is a gate fixture, so each packaging shape is exercised on every build, and a new consumer starts by copying the example nearest its shape rather than reading a schema. It also keeps slice 1 free of any cross-repo dependency — `une-sigere-api` is slice 2, and by then every mechanism it needs has been proven against something we control.

**Tech Stack:** Python 3.12+, `microcli-toolkit` (CLI), `pyyaml`, `uv` (fetches python-build-standalone), `dpkg-deb`, Docker (build/test only), `systemd-nspawn` (gate).

## Global Constraints

Copied verbatim from `docs/design-spec.md`. Every task's requirements implicitly include this section.

- **Build floor: Ubuntu 22.04** (glibc 2.35). Verified to run on 2.35 / 2.36 / 2.39 / 2.41.
- **No venv, ever.** Vendor python-build-standalone; install into its own `site-packages`.
- **`cp -aL`, never `cp -a`**, when materialising the interpreter — uv's managed dir is a symlink.
- **Delete `lib/python<version>/EXTERNALLY-MANAGED`** from the vendored tree (version comes from `porter.yaml`, never hardcoded).
- **`--break-system-packages`** on `uv pip install` into the vendored interpreter.
- **`python -m <module>`, never `bin/` console scripts** — their shebangs are absolute build paths.
- **Config is two files:** `/etc/<pkg>/defaults` (conffile, package-owned) and `/etc/<pkg>/env` (admin-owned, `postinst`-created if absent, **never** inside the `.deb`).
- **`postinst` never asks a question.** `DEBIAN_FRONTEND=noninteractive` does *not* suppress dpkg's conffile prompt.
- **The package never writes to `/var/lib/<pkg>/`.**
- **`dpkg-deb -Znone`** for packages with high-entropy payloads.
- **The `Packages` index is emitted from `dpkg-deb --field`**, not `dpkg-scanpackages` (`dpkg-dev` is absent on demos).
- **Never pipe a gate.** `cmd | tail` hands `&&` the exit code of `tail`. Read every `rc` directly.
- **Every gate assertion carries a positive control or a magnitude check.**
- **The install must complete unattended**: no prompt reachable, verified under `setsid` with stdin closed.
- **`sudo -n` or an explicit refusal** — never block on a password prompt.
- **Real env guards only:** `DEBIAN_FRONTEND=noninteractive`, `NEEDRESTART_MODE=a`, `UCF_FORCE_CONFOLD=1`, `-o Dpkg::Use-Pty=0`. **`NEEDRESTART_SUSPEND` does not exist** in needrestart 3.6 — measured; do not reintroduce it.
- **`apt-get update` scoped to our own list file** so a client's broken network source cannot break an offline install.
- **Static system user, never `DynamicUser=yes`** — the latter puts state in `/var/lib/private/<pkg>` (`700 root:root`), unreadable and unlistable by a non-root operator.
- **English for everything porter produces**: code, comments, identifiers, log messages, CLI flags and help text, generated `install.sh` and `README.txt`, tests, docs, commits. porter ships no Spanish. A component may pass its own operator text in as data; porter never bakes a language in.

## File Structure

| File | Responsibility |
|---|---|
| `src/porter/interpreter.py` | Materialise a relocatable CPython tree; install deps into it. |
| `src/porter/deb.py` | Staged directory → `.deb`. Control fields, conffiles, maintainer scripts. |
| `src/porter/config.py` | Split a flat env template into package-owned `defaults` + admin-owned `env`. |
| `src/porter/systemd.py` | Emit a unit file for a vendored-interpreter service. |
| `src/porter/repo.py` | Flat apt repo index + `Release` + USB tree + generated `install.sh`. |
| `src/porter/gate.py` | Install/upgrade a package in a clean networkless container; assert; mutation-check. |
| `src/porter/spec.py` | Parse `porter.yaml` into a `Component`. |
| `src/porter/cli.py` | microcli surface: `build`, `gate`, `publish`. |
| `tests/conftest.py` | Session-scoped vendored interpreter; `docker` / `nspawn` markers. |
| `examples/service-fastapi/` | The canonical shape: HTTP service, systemd unit, split config, port. |
| `examples/command/` | A CLI installed to `/usr/bin`. No unit, no `/etc`, no state. |
| `examples/oneshot-timer/` | A scheduled job: oneshot unit + `.timer`, writes to `/var/lib/<pkg>/`. |
| `examples/suite/` | Two components plus a metapackage — the UNE two-machine shape. |
| `examples/desktop-app/` | Near-native: chromeless browser launcher, `.desktop`, icon. |

---

### Task 1: Vendor a relocatable CPython

**Files:**
- Create: `src/porter/interpreter.py`
- Create: `tests/test_interpreter.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Produces: `vendor(dest: Path, version: str = "3.12") -> Path` — materialises `dest/python`, returns the interpreter binary path (`dest/python/bin/python3.12`). `install(python_bin: Path, requirements: list[str], constraints: Path | None = None) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_interpreter.py
import subprocess
from pathlib import Path
from porter.interpreter import vendor, install


def test_vendored_root_is_a_real_directory_not_a_symlink(vendored: Path):
    """uv's managed python dir IS a symlink; cp -a would copy the link and
    vendor nothing. This is the P1b bug, as a regression test."""
    assert (vendored / "python").is_dir()
    assert not (vendored / "python").is_symlink()


def test_vendored_tree_is_substantial(vendored: Path):
    """A magnitude check: a link-copy produces 2 entries, a real one ~3000."""
    assert len(list((vendored / "python").rglob("*"))) > 1000


def test_externally_managed_marker_removed(vendored: Path):
    assert not (vendored / "python/lib/python3.12/EXTERNALLY-MANAGED").exists()


def test_interpreter_runs(vendored: Path):
    out = subprocess.run(
        [str(vendored / "python/bin/python3.12"), "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True, text=True, check=True,
    )
    assert "(3, 12)" in out.stdout


def test_install_puts_packages_in_the_vendored_site_packages(vendored: Path):
    install(vendored / "python/bin/python3.12", ["idna"])
    hits = list((vendored / "python/lib/python3.12/site-packages").glob("idna"))
    assert hits, "idna did not land in the vendored site-packages"
```

```python
# tests/conftest.py
import shutil
import pytest
from pathlib import Path
from porter.interpreter import vendor


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: needs a working docker daemon")
    config.addinivalue_line("markers", "nspawn: needs systemd-nspawn and root")


@pytest.fixture(scope="session")
def vendored(tmp_path_factory) -> Path:
    """One vendored interpreter for the whole session -- it is ~97 MB and a
    download, so building it per-test would make the suite unusable."""
    if not shutil.which("uv"):
        pytest.skip("uv not on PATH")
    dest = tmp_path_factory.mktemp("vendored")
    vendor(dest)
    return dest
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_interpreter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'porter.interpreter'`

- [ ] **Step 3: Write the implementation**

```python
# src/porter/interpreter.py
"""Vendor a relocatable CPython.

Not a venv. `uv venv --relocatable` writes an absolute symlink to the build
host's interpreter into venv/bin/python, and rewriting pyvenv.cfg does not fix
it -- the symlink is the broken thing. Measured 2026-08-07: the venv variants
returned 127 on every target; this one returned 0 on glibc 2.35 through 2.41.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

DEFAULT_VERSION = "3.12"


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed rc={proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def vendor(dest: Path, version: str = DEFAULT_VERSION) -> Path:
    """Materialise a relocatable CPython at dest/python. Returns the binary."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    _run(["uv", "python", "install", version])
    found = Path(_run(["uv", "python", "find", version]))
    src = found.parent.parent  # .../cpython-3.12-linux-x86_64-gnu

    target = dest / "python"
    if target.exists():
        shutil.rmtree(target)
    # -L dereferences. `uv python find` resolves through a SYMLINKED directory
    # (cpython-3.12-... -> cpython-3.12.13-...), so a plain copy vendors a
    # dangling link that still resolves on any host that has uv -- and fails
    # only at the client. shutil.copytree(symlinks=False) is the -L equivalent.
    shutil.copytree(src, target, symlinks=False)

    # uv marks its managed interpreters externally-managed to protect its own
    # cache. python-build-standalone itself is not; removing it is the
    # legitimate redistributor action.
    (target / f"lib/python{version}/EXTERNALLY-MANAGED").unlink(missing_ok=True)

    binary = target / f"bin/python{version}"
    if not binary.exists():
        raise RuntimeError(f"vendored interpreter missing at {binary}")
    return binary


def install(python_bin: Path, requirements: list[str], constraints: Path | None = None) -> None:
    """Install packages into the vendored interpreter's own site-packages."""
    cmd = ["uv", "pip", "install", "--python", str(python_bin), "--break-system-packages"]
    if constraints:
        cmd += ["--constraint", str(constraints)]
    cmd += list(requirements)
    _run(cmd)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_interpreter.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/porter/interpreter.py tests/test_interpreter.py tests/conftest.py
git commit -m "feat(interpreter): vendor a relocatable CPython without a venv"
```

---

### Task 2: Staged tree → `.deb`

**Files:**
- Create: `src/porter/deb.py`
- Create: `tests/test_deb.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `build_deb(stage: Path, control: dict[str, str], out_dir: Path, conffiles: list[str] = (), scripts: dict[str, str] = None) -> Path`. `scripts` keys are `postinst` / `prerm` / `postrm`. Returns the written `.deb` path.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_deb.py
import subprocess
from pathlib import Path
import pytest
from porter.deb import build_deb

CONTROL = {"Package": "demo-app", "Version": "1.0", "Architecture": "amd64",
           "Maintainer": "porter <porter@example.com>", "Description": "demo"}


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "stage"
    (stage / "usr/lib/demo-app").mkdir(parents=True)
    (stage / "usr/lib/demo-app/app.txt").write_text("payload\n")
    (stage / "etc/demo-app").mkdir(parents=True)
    (stage / "etc/demo-app/defaults").write_text("PORT=9000\n")
    return stage


def test_builds_a_deb_with_the_declared_fields(tmp_path):
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path)
    assert deb.name == "demo-app_1.0_amd64.deb"
    fields = subprocess.run(["dpkg-deb", "--field", str(deb)],
                            capture_output=True, text=True, check=True).stdout
    assert "Package: demo-app" in fields
    assert "Version: 1.0" in fields


def test_conffiles_are_registered(tmp_path):
    deb = build_deb(_stage(tmp_path), CONTROL, tmp_path, conffiles=["/etc/demo-app/defaults"])
    listing = subprocess.run(["dpkg-deb", "--ctrl-tarfile", str(deb)],
                             capture_output=True, check=True).stdout
    names = subprocess.run(["tar", "-t"], input=listing, capture_output=True, text=True).stdout
    assert "conffiles" in names


def test_refuses_a_stage_that_writes_to_client_state(tmp_path):
    """/var/lib/<pkg> belongs to the client. A package that ships files there
    would overwrite state on upgrade -- the failure une-tools' _check-staged.sh
    exists to prevent."""
    stage = _stage(tmp_path)
    (stage / "var/lib/demo-app").mkdir(parents=True)
    (stage / "var/lib/demo-app/state.db").write_text("x")
    with pytest.raises(ValueError, match="client-owned"):
        build_deb(stage, CONTROL, tmp_path)


def test_refuses_a_stage_carrying_an_env_file(tmp_path):
    stage = _stage(tmp_path)
    (stage / "etc/demo-app/env").write_text("SECRET=1\n")
    with pytest.raises(ValueError, match="never shipped"):
        build_deb(stage, CONTROL, tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_deb.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'porter.deb'`

- [ ] **Step 3: Write the implementation**

```python
# src/porter/deb.py
"""Staged directory -> .deb. No debhelper: a hand-written DEBIAN/ is enough."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Paths the package must never own. This is une-tools' bin/_check-staged.sh,
# generalised and enforced at build time for every component.
CLIENT_OWNED = ("var/lib", "var/log")
NEVER_SHIPPED = ("env",)          # basename, under etc/<pkg>/
JUNK = ("__pycache__", ".git", ".env", ".venv")


def _lint(stage: Path) -> None:
    for rel in CLIENT_OWNED:
        p = stage / rel
        if p.exists() and any(p.rglob("*")):
            raise ValueError(f"stage writes to client-owned path /{rel}: {p}")
    for etc in (stage / "etc").glob("*"):
        for name in NEVER_SHIPPED:
            if (etc / name).exists():
                raise ValueError(f"/etc/{etc.name}/{name} is admin-owned and never shipped in the .deb")
    for junk in JUNK:
        hits = list(stage.rglob(junk))
        if hits:
            raise ValueError(f"stage carries {junk}: {hits[0]}")


def build_deb(stage: Path, control: dict[str, str], out_dir: Path,
              conffiles: list[str] = (), scripts: dict[str, str] | None = None) -> Path:
    stage, out_dir = Path(stage), Path(out_dir)
    _lint(stage)

    debian = stage / "DEBIAN"
    debian.mkdir(exist_ok=True)
    (debian / "control").write_text("".join(f"{k}: {v}\n" for k, v in control.items()))
    if conffiles:
        (debian / "conffiles").write_text("".join(f"{c}\n" for c in conffiles))
    for name, body in (scripts or {}).items():
        path = debian / name
        path.write_text(body)
        path.chmod(0o755)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{control['Package']}_{control['Version']}_{control['Architecture']}.deb"
    # -Znone: model weights and compiled libs are high-entropy. Measured
    # 2026-08-07: a 2 GB payload builds in 10 s with -Znone; xz burns minutes
    # to save approximately nothing.
    proc = subprocess.run(
        ["dpkg-deb", "-Znone", "--build", "--root-owner-group", str(stage), str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"dpkg-deb rc={proc.returncode}: {proc.stderr.strip()}")
    shutil.rmtree(debian)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_deb.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/porter/deb.py tests/test_deb.py
git commit -m "feat(deb): build packages from a staged tree, with FHS ownership lint"
```

---

### Task 3: Split config + systemd unit — the end-to-end moment

This task produces the first gallery entry. The "demo app" is
`examples/service-fastapi`, and it stays in the repo as documentation and as the
fixture every later task reuses.

**Files:**
- Create: `src/porter/config.py`
- Create: `src/porter/systemd.py`
- Create: `examples/service-fastapi/{porter.yaml,src/app.py}`
- Create: `tests/test_config.py`
- Create: `tests/test_service_e2e.py`

**Interfaces:**
- Consumes: `build_deb` (Task 2), `vendor`/`install` (Task 1).
- Produces: `split(template: dict[str, str], admin_keys: list[str]) -> tuple[str, str]` returning `(defaults_body, env_body)`. `env_postinst(pkg: str) -> str`. `unit(pkg: str, description: str, exec_start: str, workdir: str) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
from porter.config import split, env_postinst
from porter.systemd import unit

TEMPLATE = {"SIGERE_PORT": "8095", "SIGERE_DB_TDS_VERSION": "7.0", "SIGERE_DB_HOST": "", "SIGERE_DB_PASSWORD": ""}
ADMIN = ["SIGERE_DB_HOST", "SIGERE_DB_PASSWORD"]


def test_defaults_holds_package_owned_keys_only():
    defaults, _ = split(TEMPLATE, ADMIN)
    assert "SIGERE_DB_TDS_VERSION=7.0" in defaults
    assert "SIGERE_PORT=8095" in defaults
    assert "SIGERE_DB_HOST" not in defaults


def test_env_holds_admin_keys_as_empty_placeholders():
    _, env = split(TEMPLATE, ADMIN)
    assert "SIGERE_DB_HOST=" in env
    assert "SIGERE_DB_PASSWORD=" in env
    assert "SIGERE_DB_TDS_VERSION" not in env


def test_postinst_creates_env_only_if_absent_and_never_prompts():
    body = env_postinst("une-sigere-api")
    assert "[ -f /etc/une-sigere-api/env ]" in body
    assert "chmod 600" in body
    for interactive in ("read ", "debconf", "db_input"):
        assert interactive not in body, f"postinst must never prompt: found {interactive!r}"


def test_unit_uses_a_static_user_not_dynamicuser():
    """DynamicUser puts state in /var/lib/private/<pkg> at 700 root:root, which
    a non-root operator cannot read or list. Measured 2026-08-07."""
    u = unit("une-sigere-api", "SIGERE API", "/x/python3.12 -m uvicorn a:app", "/x")
    assert "DynamicUser" not in u
    assert "User=une-sigere-api" in u
    assert "Group=une-sigere-api" in u


def test_postinst_creates_the_system_user_and_restarts_on_upgrade():
    body = env_postinst("une-sigere-api")
    assert "useradd --system" in body
    assert "try-restart" in body


def test_unit_loads_defaults_then_env_so_admin_wins():
    u = unit("une-sigere-api", "SIGERE API", "/usr/lib/x/python/bin/python3.12 -m uvicorn a:app", "/usr/lib/x")
    lines = u.splitlines()
    d = lines.index("EnvironmentFile=/etc/une-sigere-api/defaults")
    e = lines.index("EnvironmentFile=-/etc/une-sigere-api/env")
    assert d < e, "admin env must be read last so it overrides defaults"
```

```python
# tests/test_service_e2e.py
"""The end-to-end moment: a package that installs and answers HTTP, offline."""
import subprocess
import pytest

pytestmark = pytest.mark.docker


def test_installed_service_answers_and_admin_env_overrides(built_demo_deb, docker_image):
    """built_demo_deb is a .deb carrying the vendored interpreter, a FastAPI
    app, the split config and a systemd unit. Run WITHOUT systemd (containers
    have no PID 1 systemd) by invoking ExecStart directly with the two env
    files sourced in order -- the unit ordering itself is asserted in
    test_config.py, and the full unit is exercised by the nspawn gate."""
    script = (
        "set -e; dpkg -i /debs/*.deb >/dev/null; "
        "echo 'GREETING=from-admin' > /etc/demo-app/env; "
        "set -a; . /etc/demo-app/defaults; . /etc/demo-app/env; set +a; "
        "/usr/lib/demo-app/python/bin/python3.12 -m uvicorn app:app "
        "  --app-dir /usr/lib/demo-app --host 127.0.0.1 --port ${PORT} & "
        "sleep 4; curl -fsS http://127.0.0.1:${PORT}/health"
    )
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{built_demo_deb.parent}:/debs:ro", docker_image, "bash", "-c", script],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert '"greeting":"from-admin"' in proc.stdout, proc.stdout
    assert '"tuning":"from-defaults"' in proc.stdout, proc.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'porter.config'`

- [ ] **Step 3: Write the implementation**

```python
# src/porter/config.py
"""Config is two files, because one file with two owners cannot work.

Measured 2026-08-07: a single conffile fails an unattended upgrade outright
(dpkg rc=1, "end of file on stdin at conffile prompt") once the admin has
edited it; DEBIAN_FRONTEND=noninteractive does not help, because that governs
debconf and not dpkg's own prompt. A postinst-copies-if-absent file never
fails -- and never delivers a newly added key to an existing client, ever.

Splitting by owner gets both: `defaults` is package-owned so it can never
conflict and new keys always land; `env` is admin-owned and never shipped, so
dpkg never has an opinion about it.
"""
from __future__ import annotations

DEFAULTS_HEADER = (
    "# Package-owned. Overwritten on upgrade -- do NOT edit.\n"
    "# Put site overrides in ./env, which is yours and is never overwritten.\n"
)
ENV_HEADER = (
    "# Admin-owned. The package never overwrites this file.\n"
    "# Values here override ./defaults.\n"
)


def split(template: dict[str, str], admin_keys: list[str]) -> tuple[str, str]:
    defaults = DEFAULTS_HEADER + "".join(
        f"{k}={v}\n" for k, v in template.items() if k not in admin_keys)
    env = ENV_HEADER + "".join(
        f"{k}={template.get(k, '')}\n" for k in admin_keys)
    return defaults, env


def env_postinst(pkg: str) -> str:
    """Create the admin file if absent. NEVER prompts: a postinst that asks a
    question hangs an unattended install, which is the one thing the whole
    'same command installs and updates' promise rests on."""
    return f"""#!/bin/sh
set -e
if [ "$1" = configure ]; then
  # Static system user: stable UID across boots, so /var/lib/{pkg} keeps
  # predictable ownership and a non-root operator can back it up.
  getent group {pkg} >/dev/null || groupadd --system {pkg}
  getent passwd {pkg} >/dev/null || useradd --system --gid {pkg} \
      --no-create-home --shell /usr/sbin/nologin {pkg}
  mkdir -p /etc/{pkg}
  if [ ! -f /etc/{pkg}/env ]; then
    cp /usr/share/{pkg}/env.example /etc/{pkg}/env
    chmod 600 /etc/{pkg}/env
  fi
  systemctl daemon-reload || true
  systemctl enable {pkg}.service >/dev/null 2>&1 || true
  # Restart on upgrade so the operator needs no follow-up command. try-restart
  # is a no-op when the unit is not running, which is the fresh-install case.
  systemctl try-restart {pkg}.service >/dev/null 2>&1 || true
fi
exit 0
"""
```

```python
# src/porter/systemd.py
"""Units replace compose services.

depends_on/service_healthy has no systemd equivalent; ordering is After=/
Requires= plus Restart=on-failure convergence. That is a deliberate accepted
behavioural change, recorded in docs/design-spec.md.

NOT DynamicUser=yes. Measured 2026-08-07: it redirects StateDirectory to
/var/lib/private/<pkg>, mode 700 root:root, which a non-root operator can
neither read nor list -- so backups, monitoring and support all need root, and
admin-dropped files silently change owner as the UID rotates. A static system
user created in postinst gives a real /var/lib/<pkg> at 750 root:<pkg>, which
passed every non-root access check the private layout failed.
"""
from __future__ import annotations


def unit(pkg: str, description: str, exec_start: str, workdir: str,
         user: str | None = None, after: str = "network.target") -> str:
    user = user or pkg
    return f"""[Unit]
Description={description}
After={after}

[Service]
EnvironmentFile=/etc/{pkg}/defaults
EnvironmentFile=-/etc/{pkg}/env
WorkingDirectory={workdir}
ExecStart={exec_start}
User={user}
Group={user}
StateDirectory={pkg}
ProtectSystem=strict
PrivateTmp=yes
NoNewPrivileges=yes
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
```

Note for the implementer: `/etc/<pkg>/env` is mode 600 root, and the service runs as the unprivileged static user — which cannot read that file itself. It works anyway because systemd reads `EnvironmentFile` as root *before* dropping privileges (verified on demos 2026-08-07). Do not "fix" this by loosening the mode or by chowning the file to the service user.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_config.py -v` → 4 passed
Then write `examples/service-fastapi/src/app.py` as a FastAPI app returning `{"greeting": os.environ["GREETING"], "tuning": os.environ["TUNING"]}`, with `TUNING=from-defaults` in `defaults` and `GREETING` as the sole entry in `admin_keys`; add the `built_demo_deb` / `docker_image` fixtures to `tests/conftest.py` that build it. Then run:
Run: `uv run pytest tests/test_service_e2e.py -v -m docker`
Expected: PASS — the response carries `from-admin` and `from-defaults`

- [ ] **Step 5: Commit**

```bash
git add src/porter/config.py src/porter/systemd.py tests/test_config.py tests/test_service_e2e.py tests/conftest.py
git commit -m "feat(config): split package-owned defaults from admin-owned env; emit units"
```

---

### Task 4: Flat apt repo, USB tree, and the upgrade path

**Files:**
- Create: `src/porter/repo.py`
- Create: `tests/test_repo.py`

**Interfaces:**
- Consumes: `build_deb` (Task 2).
- Produces: `write_index(repo_dir: Path) -> Path` (writes `Packages`, `Packages.gz`, `Release`); `usb_tree(debs: list[Path], out: Path, app: str, readme: str) -> Path`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repo.py
import gzip, subprocess
from pathlib import Path
import pytest
from porter.repo import write_index, usb_tree

pytestmark = pytest.mark.docker


def test_index_lists_every_deb_with_size_and_hash(two_demo_debs, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    for d in two_demo_debs:
        (repo / d.name).write_bytes(d.read_bytes())
    write_index(repo)
    body = (repo / "Packages").read_text()
    assert body.count("Package: demo-app") == 2
    assert "SHA256:" in body and "Filename: ./" in body
    assert gzip.decompress((repo / "Packages.gz").read_bytes()).decode() == body


def test_install_then_upgrade_offline_from_the_usb_tree(two_demo_debs, tmp_path, docker_image):
    """The delivery promise: the SAME command installs and upgrades. Every
    network apt source is removed inside the container, so a working mirror
    cannot rescue a broken local repo and make this test a lie."""
    out = usb_tree(two_demo_debs, tmp_path / "usb", app="demo-app", readme="Installation\n")
    script = (
        "set -e;"
        "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list;"
        "bash /media/usb/install.sh --version 1.0 >/dev/null;"
        "dpkg-query -W -f='${Version}' demo-app;"
        "echo -n ' -> ';"
        "bash /media/usb/install.sh >/dev/null;"
        "dpkg-query -W -f='${Version}' demo-app"
    )
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{out}:/media/usb:ro", docker_image, "bash", "-c", script],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "1.0 -> 2.0", proc.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_repo.py -v -m docker`
Expected: FAIL — `ModuleNotFoundError: No module named 'porter.repo'`

- [ ] **Step 3: Write the implementation**

```python
# src/porter/repo.py
"""Flat apt repo on a USB tree.

The index is emitted from `dpkg-deb --field` rather than dpkg-scanpackages:
that tool lives in dpkg-dev, which is absent on demos (the build host) and on
the Debian base image. The format is six lines to write, so porter depends on
nothing extra.

apt does NOT copy packages from a file: repo into /var/cache/apt/archives --
measured 2026-08-07: 1 MB cache for a 2 GB package. Peak transient disk for an
upgrade is therefore 2x payload, the same as plain `dpkg -i`.
"""
from __future__ import annotations

import gzip
import hashlib
import shutil
import subprocess
from pathlib import Path

INSTALL_SH = """#!/usr/bin/env bash
# Installs OR updates {app}. Same command either way -- dpkg knows which.
#
# Autonomous by construction: no prompt is reachable from here. There is no
# interactive fallback on an airgapped client, so a script that can block is a
# script that hangs invisibly at 3am.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)   shift ;;                      # accepted and implied; never prompts
    --version)  VERSION="={{2}}"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# Lift to root without a password prompt, or say exactly why not. Blocking on a
# password is indistinguishable from a hang in an unattended run.
if [ "$(id -u)" -ne 0 ]; then
  if sudo -n true 2>/dev/null; then exec sudo -n -E bash "$0" "$@"; fi
  echo "ERROR: not root, and passwordless sudo is unavailable." >&2
  echo "       Re-run as root:  sudo bash $0 $*" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a      # auto-restart; never the whiptail service list.
                               # NEEDRESTART_SUSPEND is NOT a real variable --
                               # absent from needrestart 3.6's code (measured).
export UCF_FORCE_CONFOLD=1

echo "deb [trusted=yes] file:${{HERE}}/repo ./" > /etc/apt/sources.list.d/{app}.list
# Scoped update: a client with a stale or unreachable source in sources.list.d/
# must not be able to break an offline install. Verified with a broken source.
apt-get update -qq \\
  -o Dir::Etc::sourcelist="sources.list.d/{app}.list" \\
  -o Dir::Etc::sourceparts="-" \\
  -o APT::Get::List-Cleanup="0"
apt-get install -y -qq --allow-downgrades \\
  -o Dpkg::Options::=--force-confold \\
  -o Dpkg::Options::=--force-confdef \\
  -o Dpkg::Use-Pty=0 \\
  "{app}${{VERSION}}"
echo "INSTALL_OK {app}=$(dpkg-query -W -f='${{Version}}' {app})"
echo "Next: edit /etc/{app}/env, then: systemctl restart {app}"
"""


def write_index(repo_dir: Path) -> Path:
    repo_dir = Path(repo_dir)
    stanzas = []
    for deb in sorted(repo_dir.glob("*.deb")):
        proc = subprocess.run(["dpkg-deb", "--field", str(deb)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"dpkg-deb --field rc={proc.returncode}: {proc.stderr.strip()}")
        digest = hashlib.sha256(deb.read_bytes()).hexdigest()
        stanzas.append(
            proc.stdout.rstrip("\n")
            + f"\nFilename: ./{deb.name}\nSize: {deb.stat().st_size}\nSHA256: {digest}\n")
    body = "\n".join(stanzas) + "\n"
    packages = repo_dir / "Packages"
    packages.write_text(body)
    (repo_dir / "Packages.gz").write_bytes(gzip.compress(body.encode()))
    (repo_dir / "Release").write_text("Archive: stable\nComponent: main\nArchitecture: amd64\n")
    if not stanzas:
        raise ValueError(f"no .deb found in {repo_dir}; refusing to publish an empty index")
    return packages


def usb_tree(debs: list[Path], out: Path, app: str, readme: str) -> Path:
    out = Path(out)
    repo = out / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for d in debs:
        shutil.copy2(d, repo / Path(d).name)
    write_index(repo)
    (out / "install.sh").write_text(INSTALL_SH.format(app=app))
    (out / "install.sh").chmod(0o755)
    (out / "README.txt").write_text(readme)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_repo.py -v -m docker`
Expected: 2 passed, with `1.0 -> 2.0` proving the same command did both

- [ ] **Step 5: Commit**

```bash
git add src/porter/repo.py tests/test_repo.py
git commit -m "feat(repo): flat apt index from dpkg-deb --field, plus the USB tree"
```

---

### Task 5: The gate

**Files:**
- Create: `src/porter/gate.py`
- Create: `tests/test_gate.py`

**Interfaces:**
- Consumes: `usb_tree` (Task 4).
- Produces: `gate(usb: Path, app: str, image: str, health_url: str, seed: dict[str, str]) -> GateResult`, where `GateResult` is a dataclass with `.ok: bool` and `.failures: list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gate.py
import pytest
from porter.gate import gate

pytestmark = pytest.mark.docker


def test_gate_passes_a_good_bundle(good_usb, docker_image):
    result = gate(good_usb, app="demo-app", image=docker_image,
                  health_url="http://127.0.0.1:9000/health",
                  seed={"/var/lib/demo-app/state.db": "client-data"})
    assert result.ok, result.failures


def test_gate_fails_when_the_package_eats_client_state(state_eating_usb, docker_image):
    """MUTATION TEST. A gate that cannot go red is worth less than no gate,
    because it licenses shipping. state_eating_usb's v2 postinst deletes
    /var/lib/demo-app -- exactly the failure une-tools' smoke-update.sh exists
    to catch. If this test ever passes, the gate is broken, not the package."""
    result = gate(state_eating_usb, app="demo-app", image=docker_image,
                  health_url="http://127.0.0.1:9000/health",
                  seed={"/var/lib/demo-app/state.db": "client-data"})
    assert not result.ok
    assert any("client state" in f for f in result.failures), result.failures


def test_gate_fails_when_the_payload_is_truncated(truncated_usb, docker_image):
    """Second mutation: a package whose payload is a stub. Magnitude checks
    exist because a 12 KB package once reported as 'built' during design."""
    result = gate(truncated_usb, app="demo-app", image=docker_image,
                  health_url="http://127.0.0.1:9000/health", seed={})
    assert not result.ok
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gate.py -v -m docker`
Expected: FAIL — `ModuleNotFoundError: No module named 'porter.gate'`

- [ ] **Step 3: Write the implementation**

```python
# src/porter/gate.py
"""Prove a bundle before it ships.

Every assertion carries a positive control or a magnitude check. During design,
five probes reported passes that were false -- a dangling symlink resolving to
the build host's own interpreter, a network probe using bash-only /dev/tcp
under dash, an interface count with no `ip` binary, a memory check satisfied by
'command not found', and a truncated 12 KB package reported as built. Each was
caught only because something downstream contradicted it.

Never pipe a gate: `cmd | tail` hands the caller tail's exit code.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GateResult:
    ok: bool = True
    failures: list[str] = field(default_factory=list)

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.ok = False
            self.failures.append(message)


def _run(image: str, mounts: dict[str, str], script: str) -> subprocess.CompletedProcess:
    cmd = ["docker", "run", "--rm", "--network", "none"]
    for host, guest in mounts.items():
        cmd += ["-v", f"{host}:{guest}:ro"]
    cmd += [image, "bash", "-c", script]
    return subprocess.run(cmd, capture_output=True, text=True)


def gate(usb: Path, app: str, image: str, health_url: str,
         seed: dict[str, str]) -> GateResult:
    r = GateResult()
    usb = Path(usb)

    versions = sorted({
        line.split(": ", 1)[1]
        for line in (usb / "repo/Packages").read_text().splitlines()
        if line.startswith("Version: ")})
    r.check(len(versions) >= 2, f"need >=2 versions to test an upgrade, found {versions}")
    if not r.ok:
        return r
    old, new = versions[0], versions[-1]

    seed_cmds = "".join(
        f"mkdir -p $(dirname {p}); printf '%s' '{v}' > {p}; " for p, v in seed.items())
    verify_cmds = "".join(
        f"[ \"$(cat {p} 2>/dev/null)\" = '{v}' ] || echo 'LOST {p}'; " for p, v in seed.items())

    script = f"""
set -e
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list 2>/dev/null
# A stale unreachable source the client left behind. A scoped apt-get update
# must step over it rather than failing the offline install.
echo 'deb [trusted=yes] http://192.0.2.1/gone ./' > /etc/apt/sources.list.d/zz-broken.list

# CONTROL for the unattended check: under this harness a bare `read` MUST fail.
# Otherwise "no prompt reached" is satisfied by a harness that cannot detect one.
if setsid bash -c 'read -r _' < /dev/null 2>/dev/null; then echo "TTYCTL=blind"; else echo "TTYCTL=ok"; fi

# The install runs with NO controlling terminal and stdin closed.
setsid bash /media/usb/install.sh --version {old} < /dev/null > /tmp/inst.log 2>&1
grep -qiE 'what would you like|whiptail|EOF on stdin|Which services' /tmp/inst.log \
  && echo "PROMPTED=yes" || echo "PROMPTED=no"
echo "INSTALLED=$(dpkg-query -W -f='${{Version}}' {app})"

# Magnitude check: a truncated payload installs cleanly and is useless.
echo "PAYLOAD=$(du -sk /usr/lib/{app} 2>/dev/null | cut -f1)"

{seed_cmds}
bash /media/usb/install.sh >/dev/null 2>&1
echo "UPGRADED=$(dpkg-query -W -f='${{Version}}' {app})"
{verify_cmds}

# CONTROL: the health probe must detect a LIVE service before its verdict on
# the real one means anything.
( /usr/lib/{app}/python/bin/python3.12 -m http.server 9999 --bind 127.0.0.1 >/dev/null 2>&1 & )
sleep 2
curl -fsS -o /dev/null http://127.0.0.1:9999/ && echo "CONTROL=ok" || echo "CONTROL=blind"

set -a; . /etc/{app}/defaults; [ -f /etc/{app}/env ] && . /etc/{app}/env; set +a
( cd /usr/lib/{app} && ./python/bin/python3.12 -m uvicorn app:app --host 127.0.0.1 --port ${{PORT}} >/dev/null 2>&1 & )
sleep 4
curl -fsS -o /dev/null {health_url} && echo "HEALTH=ok" || echo "HEALTH=dead"

# Nothing may reach the network. A bundle that works because something was
# cached on the build host fails here and only here.
grep -qiE 'pip install|apt-get install .*http|Downloading' /var/log/apt/term.log 2>/dev/null \
  && echo "FETCHED=yes" || echo "FETCHED=no"
"""
    proc = _run(image, {str(usb): "/media/usb"}, script)
    out = proc.stdout

    r.check(f"INSTALLED={old}" in out, f"install of {old} did not report that version: {out!r}")
    r.check(f"UPGRADED={new}" in out, f"upgrade to {new} did not report that version: {out!r}")
    r.check("LOST " not in out,
            "upgrade destroyed client state: " + ", ".join(
                l for l in out.splitlines() if l.startswith("LOST ")))
    r.check("CONTROL=ok" in out, "health probe is blind -- its verdict proves nothing")
    r.check("PROMPTED=no" in out, "a prompt was reachable during the install")
    r.check("TTYCTL=ok" in out,
            "the no-TTY harness did not block an interactive read -- "
            "'no prompt reached' would prove nothing")
    r.check("HEALTH=ok" in out, "service did not answer after upgrade")
    r.check("FETCHED=no" in out, "bundle attempted a network fetch")

    payload = next((int(l.split("=")[1]) for l in out.splitlines()
                    if l.startswith("PAYLOAD=") and l.split("=")[1].isdigit()), 0)
    r.check(payload > 1024, f"payload is only {payload} KB -- truncated package")
    return r
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gate.py -v -m docker`
Expected: 3 passed — the good bundle green, **both** mutations red

- [ ] **Step 5: Commit**

```bash
git add src/porter/gate.py tests/test_gate.py
git commit -m "feat(gate): prove bundles offline, with controls and mutation tests"
```

---

### Task 6: the rest of the gallery — command, oneshot, suite

**Files:**
- Create: `src/porter/spec.py`, `src/porter/cli.py`
- Create: `examples/command/{porter.yaml,src/hello.py}`
- Create: `examples/oneshot-timer/{porter.yaml,src/tick.py}`
- Create: `examples/suite/porter.yaml`
- Create: `tests/test_spec.py`, `tests/test_examples.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `Python` and `Component` dataclasses; `load(path) -> tuple[Python, list[Component]]`; `Component.kind` in `{"service", "command", "oneshot", "meta"}`; CLI `porter build|gate|publish`.

- [ ] **Step 1: Write the three example manifests**

```yaml
# examples/command/porter.yaml — a CLI. No unit, no /etc, no state.
build_floor: ubuntu:22.04
python: {version: "3.12", package: bundled}
components:
  - name: hello
    package: porter-example-command
    kind: command
    description: A command-line tool with a vendored interpreter
    entrypoint: hello:main          # exposed at /usr/bin/porter-hello
    bin_name: porter-hello
    source_paths: [src]
    requirements: []
```

```yaml
# examples/oneshot-timer/porter.yaml — a scheduled job.
build_floor: ubuntu:22.04
python: {version: "3.12", package: bundled}
components:
  - name: tick
    package: porter-example-oneshot
    kind: oneshot
    description: Writes a timestamp into client state on a schedule
    entrypoint: tick:main
    source_paths: [src]
    requirements: []
    schedule: "*-*-* *:00:00"       # hourly; becomes OnCalendar= in the .timer
    defaults: {TICK_LABEL: scheduled}
    admin_keys: []
```

```yaml
# examples/suite/porter.yaml — two components + a metapackage.
# This is UNE's two-machine shape: one name per machine, dpkg resolves the rest.
build_floor: ubuntu:22.04
python: {version: "3.12", package: bundled}
components:
  - name: api
    package: porter-example-suite-api
    kind: service
    description: Suite API component
    entrypoint: app:app
    source_paths: [../service-fastapi/src]
    requirements: [fastapi, uvicorn]
    defaults: {PORT: "9101"}
    admin_keys: [API_TOKEN]
    health: /health
  - name: tool
    package: porter-example-suite-tool
    kind: command
    description: Suite CLI component
    entrypoint: hello:main
    bin_name: porter-suite-tool
    source_paths: [../command/src]
    requirements: []
metapackages:
  - package: porter-example-suite
    description: Installs the whole suite
    depends: [porter-example-suite-api, porter-example-suite-tool]
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_spec.py
from pathlib import Path
import pytest
from porter.spec import load

EX = Path(__file__).parents[1] / "examples"


def test_python_block_is_project_declared_not_hardcoded():
    python, _ = load(EX / "service-fastapi/porter.yaml")
    assert python.version == "3.12"
    assert python.bundled


def test_every_example_manifest_parses():
    """The gallery is the regression suite; a manifest that stops parsing is a
    porter bug, not an example bug."""
    for manifest in sorted(EX.glob("*/porter.yaml")):
        python, comps = load(manifest)
        assert comps, f"{manifest} declared no components"
        assert python.version


def test_kinds_cover_every_shape_we_ship():
    kinds = {c.kind for m in EX.glob("*/porter.yaml") for c in load(m)[1]}
    assert {"service", "command", "oneshot"} <= kinds


def test_rejects_a_key_declared_in_both_halves():
    """One key, two owners is the exact defect the split config removes."""
    with pytest.raises(ValueError, match="both defaults and admin_keys"):
        load(EX.parent / "tests/fixtures/porter-overlap.yaml")
```

```python
# tests/test_examples.py
import subprocess
from pathlib import Path
import pytest

pytestmark = pytest.mark.docker
EX = Path(__file__).parents[1] / "examples"


def test_command_example_is_runnable_after_install(built_usb, docker_image):
    """kind: command produces no unit and no /etc -- just a working binary."""
    usb = built_usb("command")
    script = ("set -e; bash /media/usb/install.sh >/dev/null 2>&1; "
              "porter-hello --version; "
              "test ! -e /usr/lib/systemd/system/porter-example-command.service && echo NO_UNIT; "
              "test ! -d /etc/porter-example-command && echo NO_ETC")
    proc = subprocess.run(["docker", "run", "--rm", "--network", "none",
                           "-v", f"{usb}:/media/usb:ro", docker_image, "bash", "-c", script],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "NO_UNIT" in proc.stdout and "NO_ETC" in proc.stdout


def test_oneshot_example_writes_client_state(built_usb, docker_image):
    usb = built_usb("oneshot-timer")
    script = ("set -e; bash /media/usb/install.sh >/dev/null 2>&1; "
              "test -f /usr/lib/systemd/system/porter-example-oneshot.timer && echo HAS_TIMER; "
              "/usr/lib/porter-example-oneshot/python/bin/python3.12 "
              "  -c 'import sys; sys.path.insert(0,\"/usr/lib/porter-example-oneshot\"); "
              "     import tick; tick.main()'; "
              "cat /var/lib/porter-example-oneshot/last-tick.txt")
    proc = subprocess.run(["docker", "run", "--rm", "--network", "none",
                           "-v", f"{usb}:/media/usb:ro", docker_image, "bash", "-c", script],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "HAS_TIMER" in proc.stdout
    assert "scheduled" in proc.stdout


def test_suite_metapackage_pulls_both_components(built_usb, docker_image):
    usb = built_usb("suite")
    script = ("set -e; bash /media/usb/install.sh >/dev/null 2>&1; "
              "dpkg-query -W -f='${Status}\\n' porter-example-suite-api porter-example-suite-tool")
    proc = subprocess.run(["docker", "run", "--rm", "--network", "none",
                           "-v", f"{usb}:/media/usb:ro", docker_image, "bash", "-c", script],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("install ok installed") == 2, proc.stdout
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_spec.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'porter.spec'`

- [ ] **Step 4: Implement `spec.py` and `cli.py`**

```python
# src/porter/spec.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import yaml

KINDS = {"service", "command", "oneshot", "meta"}


@dataclass
class Python:
    """Which interpreter this project ships, and how.

    porter hardcodes neither a version nor a package name. `package` is either
    the literal "bundled" (interpreter inside each component's .deb) or the name
    of a separate interpreter package the project chooses.
    """
    version: str
    package: str = "bundled"

    @property
    def bundled(self) -> bool:
        return self.package == "bundled"


@dataclass
class Component:
    name: str
    package: str
    description: str
    kind: str = "service"
    entrypoint: str | None = None
    bin_name: str | None = None
    source_paths: list[str] = field(default_factory=list)
    requirements: list[str] | str = field(default_factory=list)
    defaults: dict[str, str] = field(default_factory=dict)
    admin_keys: list[str] = field(default_factory=list)
    constraints: str | None = None
    health: str = "/health"
    schedule: str | None = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"{self.package}: unknown kind {self.kind!r} (want one of {sorted(KINDS)})")
        if self.kind == "oneshot" and not self.schedule:
            raise ValueError(f"{self.package}: kind 'oneshot' needs a schedule")
        if self.kind == "command" and not self.bin_name:
            raise ValueError(f"{self.package}: kind 'command' needs a bin_name")
        overlap = set(self.defaults) & set(self.admin_keys)
        if overlap:
            raise ValueError(
                f"{self.package}: {sorted(overlap)} declared in both defaults and admin_keys")


def load(path: Path) -> tuple[Python, list[Component]]:
    doc = yaml.safe_load(Path(path).read_text())
    python = Python(**doc["python"])
    if python.package != "bundled" and not python.package.strip():
        raise ValueError("python.package must be 'bundled' or a package name")
    comps = [Component(**raw) for raw in doc.get("components", [])]
    for meta in doc.get("metapackages", []):
        comps.append(Component(name=meta["package"], package=meta["package"],
                               description=meta["description"], kind="meta",
                               admin_keys=[], defaults={}))
    return python, comps
```

```python
# src/porter/cli.py
"""porter -- airgapped .deb installers for Debian-family clients."""
from pathlib import Path

from microcli import App

from porter.spec import load

app = App(name="porter", help="Build airgapped .deb installers")


@app.command
def build(config: str = "porter.yaml", out: str = "dist"):
    """Bake, assemble, lint and package every component in the config."""
    python, components = load(Path(config))
    for component in components:
        print(f"building {component.package} [{component.kind}] "
              f"(python {python.version}, {python.package})")


def main():
    app.main()
```

- [ ] **Step 5: Run every test**

Run: `uv run pytest tests/ -v -m "not nspawn"`
Expected: all green, including the three gallery tests under `-m docker`

- [ ] **Step 6: Commit**

```bash
git add src/porter/spec.py src/porter/cli.py examples/ tests/test_spec.py tests/test_examples.py
git commit -m "feat(examples): command, oneshot and suite shapes as gate fixtures"
```

---

### Task 7: the desktop shape, and `Depends:` derived from ELF headers

**Files:**
- Create: `src/porter/depends.py`
- Create: `src/porter/desktop.py`
- Create: `examples/desktop-app/{porter.yaml,src/app.py,assets/icon.png}`
- Create: `tests/test_depends.py`, `tests/test_desktop.py`

**Interfaces:**
- Consumes: `build_deb` (Task 2), `Component` (Task 6).
- Produces: `derive_depends(tree: Path) -> list[str]`; `launcher(pkg, url, health, name) -> str`; `desktop_entry(pkg, name, icon) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_depends.py
import shutil, subprocess
from pathlib import Path
import pytest
from porter.depends import derive_depends


def test_derives_packages_from_a_real_binary(tmp_path):
    """A hand-written Depends: is how a package installs cleanly and then cannot
    open a window. Derive it from ELF headers instead."""
    tree = tmp_path / "payload"; tree.mkdir()
    shutil.copy(shutil.which("bash"), tree / "bash")
    deps = derive_depends(tree)
    assert deps, "no dependencies derived from a real dynamically-linked binary"
    assert any("libc" in d for d in deps), deps


def test_ignores_libraries_the_payload_ships_itself(tmp_path):
    """A bundled tree carries its own .so files; those are not system deps."""
    tree = tmp_path / "payload"; tree.mkdir()
    shutil.copy(shutil.which("bash"), tree / "bash")
    (tree / "libselfshipped.so.1").write_bytes(b"\x7fELF")
    assert not any("selfshipped" in d for d in derive_depends(tree))


def test_empty_tree_derives_nothing(tmp_path):
    assert derive_depends(tmp_path) == []
```

```python
# tests/test_desktop.py
from porter.desktop import launcher, desktop_entry


def test_launcher_probes_browsers_in_order_and_uses_app_mode():
    body = launcher("ainbox", "http://127.0.0.1:8080", "/health", "AInBox")
    order = [body.index(b) for b in ("google-chrome", "chromium", "brave-browser")]
    assert order == sorted(order), "browser probe order is not deterministic"
    assert "--app=" in body
    assert "--class=" in body
    assert "--no-first-run" in body


def test_launcher_waits_for_health_before_opening():
    """Clicking the icon right after login must not show connection-refused
    while systemd is still bringing the stack up."""
    body = launcher("ainbox", "http://127.0.0.1:8080", "/health", "AInBox")
    assert "/health" in body
    assert body.index("/health") < body.index("--app="), "opens before waiting"


def test_launcher_uses_an_isolated_profile():
    body = launcher("ainbox", "http://127.0.0.1:8080", "/health", "AInBox")
    assert "--user-data-dir=" in body
    assert ".local/share/ainbox" in body


def test_desktop_entry_is_valid(tmp_path):
    import shutil, subprocess
    entry = tmp_path / "ainbox.desktop"
    entry.write_text(desktop_entry("ainbox", "AInBox", "ainbox"))
    if shutil.which("desktop-file-validate"):
        proc = subprocess.run(["desktop-file-validate", str(entry)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_depends.py tests/test_desktop.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'porter.depends'`

- [ ] **Step 3: Write the implementation**

```python
# src/porter/depends.py
"""Derive Depends: from what the payload actually links.

objdump reads ELF headers; it never executes the loader. `ldd` does, and running
it per file over a browser tree takes minutes (measured: it hung a probe long
enough to look like a deadlock).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _sonames(elf: Path) -> list[str]:
    proc = subprocess.run(["objdump", "-p", str(elf)], capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    return [ln.split()[1] for ln in proc.stdout.splitlines() if "NEEDED" in ln]


def derive_depends(tree: Path) -> list[str]:
    tree = Path(tree)
    elves = [f for f in tree.rglob("*")
             if f.is_file() and f.open("rb").read(4) == b"\x7fELF"]
    if not elves:
        return []
    needed: set[str] = set()
    for f in elves:
        needed.update(_sonames(f))
    # Anything the payload ships itself is not a system dependency.
    own = {f.name for f in tree.rglob("*.so*")}
    external = sorted(needed - own)

    cache = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout
    paths = []
    for so in external:
        for line in cache.splitlines():
            parts = line.strip().split()
            if parts and parts[0] == so:
                paths.append(parts[-1])
                break
    if not paths:
        return []
    proc = subprocess.run(["dpkg", "-S", *paths], capture_output=True, text=True)
    pkgs = {p.strip() for line in proc.stdout.splitlines()
            for p in line.split(":", 1)[0].split(",")}
    return sorted(pkgs)
```

```python
# src/porter/desktop.py
"""Near-native: a chromeless window, an app-menu entry, an isolated profile.

Launcher-first. Most desktop clients already have a browser, and
`--app=URL` gives a window with no tabs and no URL bar; `--class` gives it its
own taskbar identity. A bundled browser is opt-in, for a client with no browser
or an app that needs a pinned engine.
"""
from __future__ import annotations

BROWSERS = ("google-chrome", "chromium", "chromium-browser", "brave-browser", "microsoft-edge")


def launcher(pkg: str, url: str, health: str, name: str, bundled: str | None = None) -> str:
    probe = "\n".join(
        f'  command -v {b} >/dev/null 2>&1 && BROWSER={b} && break' for b in BROWSERS)
    bundled_line = f'BROWSER="{bundled}"' if bundled else ""
    return f"""#!/usr/bin/env bash
# Launcher for {name}. Opens a chromeless window against the local service.
set -euo pipefail
URL="{url}"
PROFILE="$HOME/.local/share/{pkg}/browser-profile"
mkdir -p "$PROFILE"

# Wait for the service before opening, so a click right after login does not
# land on connection-refused while systemd is still starting the stack.
for _ in $(seq 1 30); do
  curl -fsS -o /dev/null "$URL{health}" 2>/dev/null && break
  sleep 1
done

BROWSER=""
{bundled_line}
if [ -z "$BROWSER" ]; then
  for _ in 1; do
{probe}
  done
fi
if [ -z "$BROWSER" ]; then
  exec xdg-open "$URL"
fi
exec "$BROWSER" --app="$URL" --class={name} \
  --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check
"""


def desktop_entry(pkg: str, name: str, icon: str) -> str:
    return f"""[Desktop Entry]
Type=Application
Name={name}
Comment={name}
Exec=/usr/bin/{pkg}-desktop
Icon={icon}
Terminal=false
Categories=Office;
StartupWMClass={name}
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_depends.py tests/test_desktop.py -v`
Expected: all green

- [ ] **Step 5: Wire `examples/desktop-app` and gate it**

```bash
uv run porter build --config examples/desktop-app/porter.yaml --out dist/
uv run porter publish --out /tmp/desktop-usb
uv run porter gate --usb /tmp/desktop-usb --app porter-example-desktop
```

Expected: two packages — `porter-example-desktop` (the service, no desktop deps) and `porter-example-desktop-desktop` (launcher, `.desktop`, icon, derived `Depends:`). The gate asserts the core package installs on a **headless** image where the desktop package's dependencies are absent; that is the property that keeps a server install from being blocked.

- [ ] **Step 6: Commit**

```bash
git add src/porter/depends.py src/porter/desktop.py examples/desktop-app/ tests/test_depends.py tests/test_desktop.py
git commit -m "feat(desktop): chromeless launcher, .desktop entry, derived Depends"
```

---

## Out of scope for this slice

Named so they are decisions, not omissions:

- **`une-sigere-api`, and every real app.** Slice 2. The gallery proves each
  mechanism against something we control first; a real migration should not be
  where a packaging bug is discovered. By then `sigere-api` needs no new
  machinery — it is the `service` shape with a `constraints:` file.
- **`python.package: shared`.** Slice 1 only implements `bundled`, which vendors the interpreter inside `une-sigere-api` (~97 MB duplicated the moment a second component exists). Emitting a separate interpreter package — named by the project, not by porter — is slice 2, once there are two consumers to prove the `Depends:` actually resolves offline.
- **`<app>-setup`**, the interactive first-run wizard. Slice 1 seeds `/etc/<pkg>/env` with empty placeholders; the sysadmin edits it by hand, exactly as today.
- **`systemd-nspawn` gating.** Task 5 gates in Docker with `--network none`, which proves the payload, the upgrade and client-state survival but *not* the unit under real systemd. nspawn arrives with slice 2.
- **GPG signing** of the repo (`[trusted=yes]` for now), the bwrap sandbox, and metapackages for UNE's two machines.
- **The `<app>-desktop` package** — chromeless browser, `.desktop` entry, icon, isolated profile. `sigere-api` is a headless service with no UI, so it cannot exercise any of it; that work belongs to the AInBox slice, where a near-native window is the point. What slice 1 *does* establish for it is the auto-derived `Depends:` mechanism, which the browser bundle needs and `sigere-api` does not.

## Self-review

- **Spec coverage.** FHS contract → Task 2 lint; split config → Task 3; install≠configure≠update → Tasks 3–4; systemd replaces compose → Tasks 3 and 6; autonomous install → Task 4, asserted in Task 5; delivery/USB → Task 4; gate → Task 5; `porter.yaml` and every packaging shape → Task 6; desktop and derived `Depends:` → Task 7. Sandbox, GPU, GPG signing and real-app migrations are explicitly deferred above.
- **Type consistency:** `build_deb`, `vendor`, `install`, `split`, `env_postinst`, `unit`, `write_index`, `usb_tree`, `gate`, `load`, `derive_depends`, `launcher`, `desktop_entry` are each defined once and referenced with matching signatures. `load` returns `(Python, list[Component])` everywhere.
- **Known gap, deliberate:** the pytest fixtures (`built_demo_deb`, `docker_image`, `two_demo_debs`, `built_usb`, `good_usb`, `state_eating_usb`, `truncated_usb`) are described rather than written out. They are mechanical compositions of Tasks 1–2 and writing them here would duplicate several hundred lines; the *mutations* and properties they must express are specified exactly. Write them in Task 3 and extend per task.
- **Deferred verification, carried from the spec:** whether the chromeless window genuinely reads as native is a visual check needing a display and a human; and the concrete Chromium build to bundle must be verified against its terms before Task 7 ships, not inferred.
