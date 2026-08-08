# porter Slice 1 — `une-sigere-api` as a `.deb`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** porter builds, gates and publishes une-tools' `sigere-api` as an installable, upgradable `.deb` that runs on an airgapped Debian-family box with no Docker and no system Python.

**Architecture:** Six tasks, vertically sliced. Task 1 vendors a relocatable CPython; Task 2 turns a staged tree into a `.deb`; **Task 3 is the end-to-end moment** — a real systemd service answering HTTP from an installed package; Task 4 adds the USB apt repo and the upgrade path; Task 5 adds the gate; Task 6 replaces the toy app with the real `sigere-api`. Every stage shells out to `dpkg-deb`, `tar` and `systemd-nspawn` rather than reimplementing them.

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

**Files:**
- Create: `src/porter/config.py`
- Create: `src/porter/systemd.py`
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
Then add the `built_demo_deb` / `docker_image` fixtures to `tests/conftest.py` (stage a FastAPI app returning `{"greeting": os.environ["GREETING"], "tuning": os.environ["TUNING"]}`, with `TUNING=from-defaults` in the template and `GREETING` as the sole admin key), and run:
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

### Task 6: `une-sigere-api` — the real component

**Files:**
- Create: `src/porter/spec.py`
- Create: `src/porter/cli.py` (currently empty)
- Create: `repos/une-tools/porter.yaml` (in the une-tools repo)
- Create: `tests/test_spec.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `Component` dataclass (`name`, `package`, `version`, `entrypoint`, `requirements`, `defaults`, `admin_keys`, `description`); `load(path: Path) -> list[Component]`; CLI `porter build|gate|publish`.

- [ ] **Step 1: Write `porter.yaml` against the real component**

```yaml
# repos/une-tools/porter.yaml
# Derived from deploy/release/sigere-api/{requirements.txt,env.example,Makefile}.
build_floor: ubuntu:22.04

# The interpreter this project needs. porter hardcodes neither the version nor
# a package name -- `bundled` puts it inside each component's own .deb, which is
# what slice 1 implements. A project with several components declares a shared
# package name here instead, and porter emits it as its own .deb.
python:
  version: "3.12"
  package: bundled

components:
  - name: sigere-api
    package: une-sigere-api
    description: Read-only HTTP adapter over the SIGERE SQL Server
    entrypoint: apps.sigere.backend.main:app
    source_paths: [apps/sigere, apps/__init__.py]
    requirements: deploy/release/sigere-api/requirements.txt
    constraints: uv.lock
    health: /health

    # Package-owned. These are measured constants, not preferences -- see the
    # comments in env.example. Under the old .env scheme a client who had
    # edited their file could NEVER receive a corrected TDS version; as a
    # conffile they arrive on every upgrade.
    defaults:
      SIGERE_PORT: "8095"
      SIGERE_DB_PORT: "1433"
      SIGERE_DB_LOGIN_TIMEOUT: "5"
      SIGERE_DB_TIMEOUT: "30"
      SIGERE_DB_TDS_VERSION: "7.0"
      SIGERE_DB_CHARSET: LATIN1

    # Admin-owned, per site. postinst seeds them empty; <app>-setup fills them.
    admin_keys: [SIGERE_DB_HOST, SIGERE_DB_NAME, SIGERE_DB_USER, SIGERE_DB_PASSWORD]
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_spec.py
from pathlib import Path
from porter.spec import load

FIXTURE = Path(__file__).parent / "fixtures/porter.yaml"


def test_python_block_is_project_declared_not_hardcoded():
    python, _ = load(FIXTURE)
    assert python.version == "3.12"
    assert python.bundled, "slice 1 ships a bundled interpreter"


def test_loads_the_sigere_component():
    _, comps = load(FIXTURE)
    assert len(comps) == 1
    c = comps[0]
    assert c.package == "une-sigere-api"
    assert c.entrypoint == "apps.sigere.backend.main:app"
    assert c.defaults["SIGERE_DB_TDS_VERSION"] == "7.0"
    assert "SIGERE_DB_PASSWORD" in c.admin_keys
    assert "SIGERE_DB_PASSWORD" not in c.defaults, "a secret must never be package-owned"


def test_rejects_a_key_declared_in_both_halves():
    """One key, two owners is the exact defect the split exists to remove."""
    import pytest
    bad = FIXTURE.parent / "porter-overlap.yaml"
    with pytest.raises(ValueError, match="both defaults and admin_keys"):
        load(bad)
```

Copy `repos/une-tools/porter.yaml` to `tests/fixtures/porter.yaml`, and make `porter-overlap.yaml` a copy with `SIGERE_PORT` added to `admin_keys`.

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_spec.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'porter.spec'`

- [ ] **Step 4: Implement `spec.py` and `cli.py`**

```python
# src/porter/spec.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import yaml


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
    entrypoint: str
    source_paths: list[str]
    requirements: str
    defaults: dict[str, str] = field(default_factory=dict)
    admin_keys: list[str] = field(default_factory=list)
    constraints: str | None = None
    health: str = "/health"


def load(path: Path) -> tuple[Python, list[Component]]:
    doc = yaml.safe_load(Path(path).read_text())
    python = Python(**doc["python"])
    if python.package != "bundled" and not python.package.strip():
        raise ValueError("python.package must be 'bundled' or a package name")
    out = []
    for raw in doc["components"]:
        overlap = set(raw.get("defaults", {})) & set(raw.get("admin_keys", []))
        if overlap:
            raise ValueError(
                f"{raw['package']}: {sorted(overlap)} declared in both defaults and admin_keys")
        out.append(Component(**raw))
    return python, out
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
        print(f"building {component.package} (python {python.version}, {python.package})")
    # Wired to interpreter.vendor -> interpreter.install -> deb.build_deb
    # in this task; see docs/plans/2026-08-07-slice-1-sigere-api.md.


def main():
    app.main()
```

- [ ] **Step 5: Wire `build` end-to-end and gate the real package**

Stage into `usr/lib/une-sigere-api/` (vendored interpreter + `apps/sigere`), `usr/share/une-sigere-api/env.example`, `etc/une-sigere-api/defaults`, `usr/lib/systemd/system/une-sigere-api.service` with
`ExecStart=/usr/lib/une-sigere-api/python/bin/python3.12 -m uvicorn apps.sigere.backend.main:app --host 0.0.0.0 --port ${SIGERE_PORT}`.

```bash
cd /home/apiad/Workspace/repos/une-tools
uv run --project ../porter porter build --config porter.yaml --out dist/
uv run --project ../porter porter publish --out /tmp/une-usb
uv run --project ../porter porter gate --usb /tmp/une-usb --app une-sigere-api
```

Expected: the gate reports green, and `du -sh dist/une-sigere-api_*.deb` is around 140 MB — the component's three real dependencies measured 42 MB, plus the 97 MB bundled interpreter. Slice 1 uses `python.package: bundled`; a project that later declares a shared interpreter package drops this to ~42 MB.

- [ ] **Step 6: Commit**

```bash
git add src/porter/spec.py src/porter/cli.py tests/test_spec.py tests/fixtures/
git commit -m "feat(spec): porter.yaml, and une-sigere-api as the first real package"
cd /home/apiad/Workspace/repos/une-tools
git add porter.yaml
git commit -m "feat(release): declare sigere-api as a porter component"
```

---

## Out of scope for this slice

Named so they are decisions, not omissions:

- **`python.package: shared`.** Slice 1 only implements `bundled`, which vendors the interpreter inside `une-sigere-api` (~97 MB duplicated the moment a second component exists). Emitting a separate interpreter package — named by the project, not by porter — is slice 2, once there are two consumers to prove the `Depends:` actually resolves offline.
- **`<app>-setup`**, the interactive first-run wizard. Slice 1 seeds `/etc/<pkg>/env` with empty placeholders; the sysadmin edits it by hand, exactly as today.
- **`systemd-nspawn` gating.** Task 5 gates in Docker with `--network none`, which proves the payload, the upgrade and client-state survival but *not* the unit under real systemd. nspawn arrives with slice 2.
- **GPG signing** of the repo (`[trusted=yes]` for now), the bwrap sandbox, and metapackages for UNE's two machines.

## Self-review

- **Spec coverage.** FHS contract → Task 2 lint; split config → Task 3; install≠configure≠update → Tasks 3–4; systemd replaces compose → Task 3; delivery/USB → Task 4; gate → Task 5; `porter.yaml` → Task 6. Sandbox, GPU, metapackages and migrations are explicitly deferred above.
- **Symbols checked against real `main`:** `apps/sigere/backend/main.py` exists and defines `app = FastAPI(...)` at line 27 with `@app.get("/health")` at line 80; `deploy/release/sigere-api/requirements.txt` lists exactly `fastapi`, `uvicorn[standard]`, `pymssql`; every `SIGERE_*` key above is copied from `deploy/release/sigere-api/env.example`.
- **Type consistency:** `build_deb`, `vendor`, `install`, `split`, `env_postinst`, `unit`, `write_index`, `usb_tree`, `gate`, `load` are each defined once and referenced with matching signatures.
- **Known gap, deliberate:** Task 3's `built_demo_deb` / `docker_image` and Tasks 4–5's `two_demo_debs` / `good_usb` / `state_eating_usb` / `truncated_usb` fixtures are described rather than written out. They are mechanical compositions of Tasks 1–2 and writing them here would duplicate ~150 lines; the *mutations* they must express are specified exactly.
