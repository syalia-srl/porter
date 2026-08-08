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


def _standalone_root(found: Path, version: str) -> Path:
    """Derive the python-build-standalone root from an interpreter path, and
    refuse anything that is not one.

    `uv python find` answers with the interpreter uv would *use*, and that is
    not necessarily a tree we may vendor. Two ways it goes wrong, both measured
    on zion 2026-08-07 with uv 0.11.29:

    - a **virtualenv** -- the active `$VIRTUAL_ENV`, or one merely discovered in
      the cwd -- whenever its version satisfies the request. From a directory
      holding a 3.12 `.venv`, `uv python find 3.12` returned `.venv/bin/python3`;
      two directories up is the venv itself, whose `bin/python3.12` is an
      absolute symlink into the build host. That is the failure rule 1 exists
      to prevent, produced silently rather than raised.
    - a **system or distro interpreter**: `uv python find --system 3.14`
      returned `/usr/bin/python3.14`, two directories up from which is `/usr`.
      Copying `/usr` "succeeds": the post-copy `binary.exists()` check passes
      because `/usr/bin/python3.14` came along. The artefact is multi-gigabyte
      and not relocatable.

    So this refuses on three independent grounds, and the third is what makes
    the guard hold even if the `--system --managed-python` flags are dropped
    from the find call:

    1. `pyvenv.cfg` present -> a virtualenv, by name.
    2. The standalone layout absent. The obvious probes are not sufficient on
       their own -- a venv *has* `bin/python<version>` and
       `lib/python<version>/`. What it lacks is the stdlib: its
       `lib/python<version>/` holds only `site-packages`, so `os.py` is the
       discriminator.
    3. The root is not under `uv python dir`. Layout alone cannot separate a
       vendorable tree from `/usr`, which has no `pyvenv.cfg`, has
       `bin/python<version>`, and has `lib/python<version>/os.py` -- verified
       on zion, where the four layout probes all pass for `/usr`. Provenance
       can: only trees uv installed itself live under its managed-python
       directory, which rejects `/usr`, `/usr/local`, conda and Homebrew roots
       with one predicate.

    Ordering is deliberate: the two cheap layout refusals name the specific
    shape they caught before the provenance check shells out.
    """
    root = found.parent.parent
    if (root / "pyvenv.cfg").exists():
        raise RuntimeError(
            f"refusing to vendor {root}: it is a virtualenv (pyvenv.cfg present), "
            f"not a python-build-standalone tree. `uv python find {version}` "
            f"resolved to {found}."
        )
    libdir = root / "lib" / f"python{version}"
    for probe in (root / "bin" / f"python{version}", libdir, libdir / "os.py"):
        if not probe.exists():
            raise RuntimeError(
                f"refusing to vendor {root}: not a python-build-standalone tree "
                f"({probe} is missing). `uv python find {version}` resolved to {found}."
            )
    answer = _run(["uv", "python", "dir"])
    if not answer:
        # Path("") is Path("."), which would silently turn this into "is the
        # root under the cwd" -- a check that passes for the wrong reason.
        raise RuntimeError("`uv python dir` answered nothing; cannot verify provenance")
    managed = Path(answer)
    if not root.resolve().is_relative_to(managed.resolve()):
        raise RuntimeError(
            f"refusing to vendor {root}: it is not under uv's managed python "
            f"directory ({managed}), so uv did not install it. A system, distro, "
            f"conda or Homebrew interpreter is not a relocatable "
            f"python-build-standalone tree. `uv python find {version}` resolved "
            f"to {found}."
        )
    return root


def vendor(dest: Path, version: str = DEFAULT_VERSION) -> Path:
    """Materialise a relocatable CPython at dest/python. Returns the binary."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    _run(["uv", "python", "install", version])
    # --system excludes virtualenvs; --managed-python excludes anything uv did
    # not install (without it, --system happily answers /usr/bin/python3.N,
    # whose root is /usr). Neither --no-project nor unsetting $VIRTUAL_ENV is
    # enough -- both were measured returning the cwd's venv. See
    # _standalone_root for the numbers.
    found = Path(_run(["uv", "python", "find", "--system", "--managed-python", version]))
    src = _standalone_root(found, version)  # .../cpython-3.12-linux-x86_64-gnu

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


def install(python_bin: Path, requirements: list[str], constraints: Path | None = None,
            target: Path | None = None) -> None:
    """Install packages into the vendored interpreter's own site-packages.

    `target` moves them somewhere else instead, and exists for the one case
    where the interpreter's own site-packages is not ours to write to: a
    **shared** interpreter (`python: {package: <name>}`) lives in a package of
    its own, installed once for every component. Two components writing
    `fastapi` into it would be a dpkg file conflict on the client, and the
    interpreter package would carry every component's wheels. `--target` puts
    them in the component's own payload root instead; see
    `assemble._install_requirements`. The wheels are still selected for
    `--python`, so the ABI is the shared interpreter's and not the build host's.

    `--break-system-packages` is NOT redundant, but it is a no-op on the tree
    `vendor()` hands over, because `vendor()` deleted EXTERNALLY-MANAGED two
    steps earlier. Measured on zion 2026-08-07, uv 0.11.29, against a vendored
    3.12 tree:

        marker present, no flag    -> rc=2, "The interpreter at ... is
                                      externally managed"
        marker present, with flag  -> rc=0, idna installed
        marker absent,  no flag    -> rc=0, idna installed

    So the flag is what decouples `install()` from `vendor()`'s deletion: it
    keeps working against any marked tree -- a uv-managed interpreter that was
    not run through `vendor()`, or a future `vendor()` that stops deleting.
    `test_install_succeeds_when_the_externally_managed_marker_is_present` is
    the check that bites on it; the plain install test cannot, because on its
    tree the marker is already gone.
    """
    cmd = ["uv", "pip", "install", "--python", str(python_bin), "--break-system-packages"]
    if constraints:
        cmd += ["--constraint", str(constraints)]
    if target:
        cmd += ["--target", str(target)]
    cmd += list(requirements)
    _run(cmd)
