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

# The only top-level directories a porter package may own. Everything else is
# refused by name. The failure this catches is a stage rooted one directory too
# high, which is how it goes wrong in practice: measured on zion 2026-08-08, a
# stage carrying `home/apiad/secrets/id_rsa` built at rc=0 and dpkg-deb
# --contents listed `./home/apiad/secrets/id_rsa` -- an ssh key packaged for an
# airgapped client, with nothing anywhere reporting it.
#
# Why each is in:
#   usr -- the payload. usr/lib/<pkg>/, usr/share/<pkg>/, usr/lib/systemd/system/.
#   etc -- rule 4's shipped half, /etc/<pkg>/defaults, as a conffile.
#   opt -- FHS's location for add-on software distributed outside the distro,
#          which is exactly what a vendored interpreter plus app tree is.
#   lib -- Debian's pre-usrmerge unit and udev paths (/lib/systemd/system,
#          /lib/udev/rules.d). Ubuntu 22.04 is the build floor and is usrmerged,
#          but the client's dpkg resolves the compat symlink, so refusing these
#          would refuse a correct package for a cosmetic reason.
#   var -- for the parts of /var that are not the client's. var/lib and var/log
#          stay refused beneath it, by CLIENT_OWNED.
#
# Why /srv is NOT here, against the set the review suggested: FHS defines /srv
# as "data for services provided by this system" -- the site administrator's,
# which is the same argument that refuses /var/lib. A package shipping into /srv
# claims the admin's data directory, and no porter example needs it. bin/ and
# sbin/ are out for the reason usr/bin is in: rule 3 bans console scripts, and
# on a usrmerged client the top-level pair are symlinks to usr/ anyway.
ALLOWED_TOP_LEVEL = ("usr", "etc", "opt", "lib", "var")


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
    # DEBIAN/ is porter's to construct, out of `control`, `conffiles=` and
    # `scripts=`. Refused rather than removed: deleting it gives the same
    # protection against a stale member leaking into the package, but a caller
    # who staged `triggers` or `shlibs` -- real control members with no
    # build_deb parameter -- then gets a build that succeeds *without* them, at
    # rc=0, saying nothing. A silent drop is the failure class this module
    # exists to stop. Checked first so it beats the top-level allowlist below,
    # which would otherwise refuse DEBIAN/ with the less useful message.
    debian = stage / "DEBIAN"
    if debian.exists() or debian.is_symlink():
        raise ValueError(
            f"stage carries a DEBIAN/ directory: {debian}. DEBIAN/ is porter's to "
            "build -- pass its contents as control fields, conffiles= and scripts="
        )
    # A porter package owns a declared set of paths; an unbounded top level
    # contradicts that outright. This is also the check that catches a stage
    # rooted at the wrong directory, and it subsumes a family of one-off
    # refusals that would each have to be written by hand.
    for entry in sorted(stage.iterdir()):
        if entry.name not in ALLOWED_TOP_LEVEL:
            raise ValueError(
                f"stage carries a top-level path porter does not own: /{entry.name} "
                f"({entry}). A package may own only "
                f"{', '.join('/' + d for d in ALLOWED_TOP_LEVEL)}"
            )
    for rel in CLIENT_OWNED:
        p = stage / rel
        if p.exists() and any(p.rglob("*")):
            raise ValueError(f"stage writes to client-owned path /{rel}: {p}")
    for etc in (stage / "etc").glob("*"):
        for name in NEVER_SHIPPED:
            # `or is_symlink()`: exists() follows the link, so a *dangling*
            # env symlink reported False and shipped. Verified 2026-08-08 --
            # `etc/<pkg>/env -> nowhere-at-all` built at rc=0 and dpkg-deb
            # --contents listed it, declared as a conffile or not.
            p = etc / name
            if p.exists() or p.is_symlink():
                raise ValueError(
                    f"/etc/{etc.name}/{name} is admin-owned and never shipped in the .deb"
                )
    # The other half of rule 4. Refusing /etc/<pkg>/env stops the admin's file
    # being replaced; nothing so far stopped the *shipped* half going in as
    # ordinary payload, which is the same harm one directory over: dpkg
    # overwrites an edited /etc/<pkg>/defaults on every upgrade, with no
    # .dpkg-dist, no prompt and no record, because it was never registered.
    #
    # A symlink counts as a file here. dpkg only warns that a symlinked
    # conffile "is not a plain file" and builds anyway (rc=0, verified
    # 2026-08-08), so `etc/<pkg>/extra.conf -> ../../usr/lib/<pkg>/defaults`
    # -- relative and inside the stage, so the symlink guard below passes it --
    # shipped an undeclared config path under /etc with nothing to show for it.
    declared = {"/" + str(c).lstrip("/") for c in conffiles}
    for p in _entries(stage / "etc"):
        if p.is_dir() and not p.is_symlink():
            continue  # a real directory ships as a directory; its contents are the config
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


def _refuse_generated_shell_that_does_not_parse(
        stage: Path, scripts: dict[str, str]) -> None:
    """Every script porter generated must parse, and `sh` is what decides.

    Nothing here ran `sh -n` on anything until 2026-08-08, and until Task 10 it
    did not need to: porter's maintainer scripts were built wholly from fixed
    strings and always parsed. They no longer are. A manifest's
    `migrations: script:` is spliced into the postinst verbatim, and
    `<pkg>-setup` interpolates `admin_keys` into shell text. So an unbalanced
    quote in `porter.yaml` produced a .deb that BUILT, linted and installed at
    rc=0 and died on the client with `subprocess installed post-installation
    script returned error exit status 2` -- porter's characteristic bug, newly
    reachable from the manifest and reported by the Task 10 review.

    `usr/bin/` is read for the same reason and no other: porter writes every
    file it puts there (the `command` wrapper, `<pkg>-setup`) and nothing else
    does -- `source:` lands in `/usr/lib/<pkg>`, `data:` in `/usr/share/<pkg>`.
    A payload's own shell scripts are the adopter's and are not read here.

    A syntax error is all this catches. `MY-KEY_value=$new` parses perfectly and
    is a *command* named `MY-KEY_value`, which fails at run time and nowhere
    else -- see `assemble._refuse_what_porter_cannot_emit` for the refusal that
    covers that half.
    """
    candidates = [(f"maintainer script {name}", body)
                  for name, body in sorted(scripts.items())]
    for path in sorted((stage / "usr/bin").glob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        body = path.read_bytes().decode("utf-8", errors="replace")
        if body.startswith("#!/bin/sh"):
            candidates.append((f"/usr/bin/{path.name}", body))
    for label, body in candidates:
        # Read from stdin rather than written to a file: this runs before
        # DEBIAN/ exists and must leave nothing behind on a refusal.
        probe = subprocess.run(["sh", "-n"], input=body,
                               capture_output=True, text=True)
        if probe.returncode != 0:
            raise ValueError(
                f"{label} is not valid sh: {probe.stderr.strip()}. dpkg-deb "
                "packages it, dpkg installs it, and it fails on the client -- "
                "the build is the last place this is visible"
            )


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
    _refuse_generated_shell_that_does_not_parse(stage, scripts or {})

    # Built fresh, and removed whatever the call's outcome. Both halves matter:
    # reusing an existing DEBIAN/ means a call that passes no scripts= and no
    # conffiles= still ships the previous call's, at rc=0 -- and the previous
    # call does not even have to have succeeded, because cleanup used to sit
    # after the raise. That is the silent success this module exists to stop.
    # Freshness is guaranteed by _lint refusing a pre-existing DEBIAN/ outright,
    # so this mkdir is bare: nothing here may quietly overwrite a caller's tree.
    debian = stage / "DEBIAN"
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
