"""Staged directory -> .deb. No debhelper: a hand-written DEBIAN/ is enough.

This module knows nothing about interpreters, components or manifests. It takes
a directory that already looks like the installed filesystem and turns it into
a package -- which is why the ownership lint lives here and not in whatever
built the stage: it is the last place that sees the whole tree before it
becomes an artefact nobody re-reads.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

# Paths the package must never own. This is une-tools' bin/_check-staged.sh,
# generalised and enforced at build time for every component. dpkg would ship
# these happily; the damage lands on the client, on the *second* install, when
# the upgrade overwrites state or logs the operator was keeping.
CLIENT_OWNED = ("var/lib", "var/log")

# Basenames under etc/<pkg>/. /etc/<pkg>/env is admin-owned: the admin writes
# the client's secrets there and no package version may ever replace it.
# /etc/<pkg>/defaults is the shipped half, and it goes in as a conffile.
NEVER_SHIPPED = ("env",)

# Build-host residue that must not become payload. Each is a specific failure:
# .venv is rule 1 (absolute symlinks into the build host) shipped inside a
# .deb; .git ships the repo's history to the client; .env ships whatever the
# developer had locally.
#
# __pycache__ is deliberately NOT here, though the obvious version of this list
# includes it. The tree `interpreter.vendor()` materialises carries 35
# __pycache__ directories in its stdlib (counted on zion 2026-08-07 against
# uv's managed cpython-3.12) -- precompiled bytecode is part of a
# python-build-standalone distribution, not residue. Listing it would refuse
# every stage that contains a vendored interpreter, i.e. every real porter
# package, and it would do so only once Task 3 wired the two together.
JUNK = (".venv", ".git", ".env")


def _entries(root: Path):
    """Every entry under `root`, without following symlinks.

    Not `rglob`: whether `**` descends into a symlinked directory changed in
    3.13, and this lint's job is to look *at* symlinks rather than through
    them -- an absolute link to `/` would otherwise walk the build host.
    Yields nothing when `root` does not exist.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            yield Path(dirpath) / name


def _lint(stage: Path, conffiles: Sequence[str] = ()) -> None:
    """Refuse a stage that would make the package own what it must not.

    Runs before anything is written, so a refusal leaves no half-built
    artefact behind.
    """
    for rel in CLIENT_OWNED:
        p = stage / rel
        if p.exists() and any(p.rglob("*")):
            raise ValueError(f"stage writes to client-owned path /{rel}: {p}")
    for etc in (stage / "etc").glob("*"):
        for name in NEVER_SHIPPED:
            if (etc / name).exists():
                raise ValueError(
                    f"/etc/{etc.name}/{name} is admin-owned and never shipped in the .deb"
                )
    # The other half of rule 4. Refusing /etc/<pkg>/env stops the admin's file
    # being replaced; nothing so far stopped the *shipped* half going in as
    # ordinary payload, which is the same harm one directory over: dpkg
    # overwrites an edited /etc/<pkg>/defaults on every upgrade, with no
    # .dpkg-dist, no prompt and no record, because it was never registered.
    declared = {"/" + str(c).lstrip("/") for c in conffiles}
    for p in _entries(stage / "etc"):
        if p.is_dir() or p.is_symlink():
            continue
        shipped = "/" + str(p.relative_to(stage))
        if shipped not in declared:
            raise ValueError(
                f"{shipped} ships under /etc but is not declared a conffile: "
                f"pass conffiles=[..., {shipped!r}]"
            )
    for junk in JUNK:
        hits = list(stage.rglob(junk))
        if hits:
            raise ValueError(f"stage carries {junk}: {hits[0]}")
    # Rule 1's own failure mode, caught by hazard rather than by directory
    # name. `.venv` in JUNK catches the shape uv leaves behind; any other
    # absolute symlink -- `usr/lib/<pkg>/python -> /home/<user>/.local/share/uv/
    # python/...` -- builds and ships just as happily, works on the build host
    # and dies at the client. Task 3's stages carry symlinks by design
    # (`vendor()` copies with symlinks=True), so this is the check that lets
    # the legitimate ones through: relative, and landing inside the stage.
    root = stage.resolve()
    for p in _entries(stage):
        if not p.is_symlink():
            continue
        target = Path(os.readlink(p))
        if target.is_absolute():
            raise ValueError(
                f"stage carries an absolute symlink into the build host: {p} -> {target}"
            )
        if not (p.parent / target).resolve().is_relative_to(root):
            raise ValueError(f"stage carries a symlink escaping the stage: {p} -> {target}")


def _control_field(key: str, value: str) -> str:
    """One control field, with a multi-line value folded Debian-style.

    A bare newline in a value starts a *new field*: `Description` carrying
    "demo\\nDepends: sudo" produced a package with a real `Depends:` the caller
    never wrote, at rc=0 (verified on dpkg 1.23.7). Debian continues a field
    with a leading space and spells an empty continuation line ` .`, so folding
    keeps the whole value inside its own field.

    Folded rather than refused: at Task 6 these values come from `porter.yaml`,
    where a multi-line description is ordinary input, not an attack.
    """
    head, *rest = str(value).rstrip("\n").split("\n")
    return "".join([f"{key}: {head}\n", *(f" {ln}\n" if ln.strip() else " .\n" for ln in rest)])


def build_deb(stage: Path, control: dict[str, str], out_dir: Path,
              conffiles: Sequence[str] = (),
              scripts: dict[str, str] | None = None) -> Path:
    """Package `stage` as a .deb in `out_dir`. Returns the written path.

    `scripts` keys are maintainer script names -- postinst / prerm / postrm.
    """
    stage, out_dir = Path(stage), Path(out_dir)
    _lint(stage, conffiles)

    # Built fresh, and removed whatever the call's outcome. Both halves matter:
    # reusing an existing DEBIAN/ means a call that passes no scripts= and no
    # conffiles= still ships the previous call's, at rc=0 -- and the previous
    # call does not even have to have succeeded, because cleanup used to sit
    # after the raise. That is the silent success this module exists to stop.
    debian = stage / "DEBIAN"
    if debian.exists():
        shutil.rmtree(debian)
    debian.mkdir()
    try:
        (debian / "control").write_text(
            "".join(_control_field(k, v) for k, v in control.items()))
        if conffiles:
            (debian / "conffiles").write_text("".join(f"{c}\n" for c in conffiles))
        for name, body in (scripts or {}).items():
            path = debian / name
            path.write_text(body)
            path.chmod(0o755)  # dpkg will not run a maintainer script it cannot exec

        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{control['Package']}_{control['Version']}_{control['Architecture']}.deb"
        # -Znone: model weights and compiled libs are high-entropy. Measured
        # 2026-08-07: a 2 GB payload builds in 10 s with -Znone; xz burns minutes
        # to save approximately nothing.
        #
        # --root-owner-group: porter builds unprivileged, and without it dpkg-deb
        # stamps the builder's uid on every payload file (`apiad/apiad` in
        # --contents, verified on zion 2026-08-07) while still exiting 0.
        proc = subprocess.run(
            ["dpkg-deb", "-Znone", "--build", "--root-owner-group", str(stage), str(out)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            # Read the rc directly. An unchecked returncode here returns a Path
            # that reads exactly like a built package and points at nothing.
            raise RuntimeError(f"dpkg-deb rc={proc.returncode}: {proc.stderr.strip()}")
        return out
    finally:
        shutil.rmtree(debian, ignore_errors=True)
