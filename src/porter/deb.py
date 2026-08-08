"""Staged directory -> .deb. No debhelper: a hand-written DEBIAN/ is enough.

This module knows nothing about interpreters, components or manifests. It takes
a directory that already looks like the installed filesystem and turns it into
a package -- which is why the ownership lint lives here and not in whatever
built the stage: it is the last place that sees the whole tree before it
becomes an artefact nobody re-reads.
"""
from __future__ import annotations

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


def _lint(stage: Path) -> None:
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
    for junk in JUNK:
        hits = list(stage.rglob(junk))
        if hits:
            raise ValueError(f"stage carries {junk}: {hits[0]}")


def build_deb(stage: Path, control: dict[str, str], out_dir: Path,
              conffiles: Sequence[str] = (),
              scripts: dict[str, str] | None = None) -> Path:
    """Package `stage` as a .deb in `out_dir`. Returns the written path.

    `scripts` keys are maintainer script names -- postinst / prerm / postrm.
    """
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
    shutil.rmtree(debian)
    return out
