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
    # Dereference the OUTER link only.
    #
    # `uv python find` returns a path through a SYMLINKED directory --
    # measured on zion 2026-08-07, after `uv python install 3.12`:
    #   cpython-3.12-linux-x86_64-gnu -> cpython-3.12.8-linux-x86_64-gnu
    # A shell `cp -a` of that copies the LINK, vendoring nothing: it still
    # resolves on any host that has uv, and fails only at the client. That is
    # the P1b bug. `.resolve()` pins the versioned directory so the intent
    # survives a future rewrite to cp/rsync/tar.
    #
    # (copytree specifically already follows a symlinked source root whatever
    # `symlinks=` says -- verified both ways -- so `.resolve()` is belt and
    # braces here, not the thing doing the work. Do not read the passing
    # not-a-symlink regression test as proof that this line is exercised.)
    #
    # symlinks=True is deliberate and NOT a bug to be "simplified" away: the
    # `cp -aL` equivalent (symlinks=False) would also dereference the tree's
    # INTERNAL links -- bin/python -> python3.12, lib/libpython3.12.so.1.0 and
    # friends -- duplicating megabytes of binary. Those links are relative and
    # survive relocation intact, so preserving them is both correct and smaller.
    shutil.copytree(src.resolve(), target, symlinks=True)

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
