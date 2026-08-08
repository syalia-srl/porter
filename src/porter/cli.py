"""porter's command line. Thin by design: every verb is a call into a module.

`build` is the composition the rest of the package exists for -- assemble a
manifest into a staged tree, then package that tree -- and until now it had no
body, so nothing in porter ever ran assemble and build_deb in sequence outside a
test fixture.

Deliberately **without** `from __future__ import annotations`. microcli reads a
flag's help out of `Annotated[str, "..."]` at decoration time, and under PEP 563
the annotation arrives as the *string* `'Annotated[str, "..."]'`, which has no
`__metadata__` -- so the help would be empty and microcli registers an empty
help as `argparse.SUPPRESS`. That is how `--out` and `--stage` came to be absent
from `porter build --help` while the prose beneath the usage line described
them: functional, undiscoverable.

Task 7 replaces `Component.from_manifest` with a validating loader in
`porter.spec`; the two lines below it do not change.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import microcli as m
import yaml

from porter.assemble import assemble, assemble_interpreter
from porter.bake import bake
from porter.deb import build_deb
from porter.desktop import (
    Desktop,
    assemble_desktop,
    refuse_desktop_dependencies_in_the_core_package,
)
from porter.spec import load


@m.command
def build(
    manifest: Annotated[str, "path to the porter.yaml"],
    out: Annotated[str, "directory the .deb is written to"] = "dist",
    stage: Annotated[str, "scratch directory the tree is assembled under; "
                          "porter removes the tree it creates there"] = "build",
) -> None:
    """Build a .deb from a porter.yaml. One per component, one per metapackage."""
    manifest_path = Path(manifest).resolve()
    # Read through `porter.spec`, which validates before anything is staged: an
    # unknown key, a missing one, or a role naming a component the manifest does
    # not build are all refused here rather than 97 MB later -- or, worse, not
    # at all.
    #
    # Every component, because a multi-service project is one manifest and
    # several packages: `after:` names a sibling, and the ordering refusals need
    # the whole list. A flat manifest comes back as a list of one, so the common
    # case is unchanged.
    parsed = load(manifest_path)
    siblings = [c for c, _ in parsed.components]

    # The shared interpreters first, because their stages have to outlive the
    # components: `assemble` execs the STAGED binary to select each component's
    # wheels and to answer its import probes, and reads the package's exact
    # version off it for the `Depends:` line. A component built after the stage
    # was cleaned would have nothing to probe with.
    #
    # A manifest with no `python.package` builds none of these and the loop
    # below is exactly what it was -- the bundled path is untouched.
    interpreters: dict[str, object] = {}
    stages: list[tuple[Path, bool]] = []
    debs: list[Path] = []
    try:
        for python in parsed.interpreters:
            # Architecture and maintainer come from a component that asked for
            # this interpreter: they are shared top-level keys in every manifest
            # that declares one, and inventing porter's own defaults here would
            # put a package on the USB whose Maintainer disagrees with every
            # other package beside it.
            owner = next(c for c, py in parsed.components
                         if py.package == python.package)
            stage_dir = Path(stage) / python.package
            # Same ownership rule as `_build_one`: porter removes only what
            # porter made.
            stages.append((stage_dir, not stage_dir.exists()))
            interp = assemble_interpreter(python, stage_dir,
                                          architecture=owner.architecture,
                                          maintainer=owner.maintainer)
            interpreters[python.package] = interp
            debs.append(build_deb(interp.stage, interp.control, Path(out)))
        built = [(c, _build_one(c, py, manifest_path, out, stage, siblings,
                                interpreters.get(py.package)))
                 for c, py in parsed.components]
    finally:
        for stage_dir, ours in stages:
            if ours:
                shutil.rmtree(stage_dir, ignore_errors=True)
    debs += [deb for _, deb in built]
    # After the components, so that a role's dependencies are on disk beside it
    # in `out` when the last line is printed. Nothing enforces the order --
    # dpkg-deb resolves nothing at build time -- but a USB assembled from a
    # half-failed run should not carry a role whose components were never built.
    debs += [_build_metapackage(meta, out, stage) for meta in parsed.metapackages]

    # The optional near-native shape, and it is a SECOND package. Rule 12: a
    # GUI needs GTK, X11 and NSS from the client and apt cannot fetch them on an
    # airgapped box, so a desktop dependency in the core package does not
    # degrade a headless install -- it makes it impossible. The launcher, the
    # app-menu entry and the icon are therefore packaged apart from the service
    # they open.
    #
    # Re-read rather than taken off `parsed`: `porter.spec` is Task 7's and owns
    # validating the manifest, and the desktop block's own keys are refused in
    # `porter.desktop` next to the launcher they are baked into. When the two
    # merge, this line goes and `parsed.desktop` takes its place.
    spec = Desktop.from_manifest(yaml.safe_load(manifest_path.read_text()))
    if spec is not None:
        debs.append(_build_desktop(spec, built, manifest_path, out, stage))
    m.ok(" ".join(f"{d} ({d.stat().st_size // 1024} KiB)" for d in debs))


def _build_desktop(spec, built, manifest_path: Path, out: str, stage: str) -> Path:
    """`<pkg>-desktop`, and the check that the split actually held."""
    component = spec.pick([c for c, _ in built])
    core_deb = next(deb for comp, deb in built if comp is component)
    stage_dir = Path(stage) / f"{component.package}-desktop"
    # Same ownership rule as `_build_one`: porter removes only what porter made.
    ours = not stage_dir.exists()
    try:
        staged = assemble_desktop(component, spec, manifest_path.parent, stage_dir)
        # Checked here and in neither assembler, because it is a statement about
        # the PAIR and neither half can see the other's control fields. Read off
        # the BUILT core package rather than out of the `Staged` that produced
        # it: the field that decides whether a headless client can install is the
        # one dpkg reads on that client, and the two are the same only for as
        # long as nothing between them edits control.
        refuse_desktop_dependencies_in_the_core_package(
            core_package=component.package,
            core_depends=_field(core_deb, "Depends"),
            desktop_depends=[d.strip()
                             for d in staged.control["Depends"].split(",")],
        )
        return build_deb(staged.stage, staged.control, Path(out),
                         conffiles=staged.conffiles, scripts=staged.scripts)
    finally:
        if ours:
            shutil.rmtree(stage_dir, ignore_errors=True)


def _field(deb: Path, name: str) -> list[str]:
    """One comma-separated control field, read back off the built artefact."""
    out = subprocess.run(["dpkg-deb", "--field", str(deb), name],
                         capture_output=True, text=True).stdout
    return [entry.strip() for entry in out.split(",") if entry.strip()]


def _build_one(component, python, manifest_path: Path, out: str, stage: str,
               siblings, interpreter=None) -> Path:
    # Source paths are relative to the manifest, so a build run from anywhere
    # stages the same tree. A cwd-relative read is the shape that works in the
    # repo root and fails in CI.
    #
    # Left relative if that is what the caller passed: `assemble` resolves it,
    # and doing it twice would leave that fix with nothing to prove.
    stage_dir = Path(stage) / component.package

    # porter invented this directory, so porter removes it -- on success and on
    # failure alike. `assemble`'s "refuse, never repair" is right for a stage a
    # caller pre-staged deliberately, and wrong for one the CLI owns: the tree
    # is 97 MB, and leaving it behind means the *next* run of the same command
    # meets the non-empty-stage refusal instead of whatever it was going to do.
    # The command then succeeds exactly once, and a failure reports a different
    # error the second time than the first.
    #
    # `ours` is the whole of the distinction, and it is also what keeps the
    # rmtree honest: a directory that was already there is a caller's, whatever
    # put it there.
    ours = not stage_dir.exists()
    try:
        # Before the stage exists, and before 97 MB is vendored on top of data
        # that may not have been rebuilt: bake runs the project's own ETL and
        # refuses an artefact that is absent, too small, or hiding half its rows
        # in an uncheckpointed SQLite WAL. Its stamp is what tells a client
        # which build it is running.
        baked = bake(component, manifest_path.parent)
        staged = assemble(component, python, manifest_path.parent, stage_dir,
                          stamp=baked.stamp, siblings=siblings,
                          interpreter=interpreter)
        deb = build_deb(staged.stage, staged.control, Path(out),
                        conffiles=staged.conffiles, scripts=staged.scripts)
    finally:
        if ours:
            shutil.rmtree(stage_dir, ignore_errors=True)
    return deb


def _build_metapackage(meta, out: str, stage: str) -> Path:
    """A machine role: a control file with `Depends:`, and no payload at all.

    It does not go through `assemble`. There is nothing to assemble -- no
    interpreter, no module, no unit, no config -- and routing it through a
    stager whose every refusal is about payload would mean inventing a `module`
    for a package that runs nothing. `build_deb` packages an empty directory
    quite happily: 20 KB, `dpkg-deb --contents` listing `./` and nothing else
    (measured 2026-08-08).

    **The stage must be empty, and that is a real guard rather than hygiene.**
    `build_deb`'s lint refuses top-level paths porter does not own, and `usr/`
    is one porter *does* own -- so a leftover tree from the component built
    immediately before would be packaged into the role, at rc=0, turning a
    20 KB metapackage into a 91 MB one carrying another package's interpreter.
    The two would then both own `/usr/lib/<other-pkg>` and dpkg would report a
    file conflict on the client, at install time.
    """
    stage_dir = Path(stage) / meta.package
    if stage_dir.exists() and any(stage_dir.iterdir()):
        raise ValueError(
            f"metapackage stage is not empty: {stage_dir}. A metapackage has no "
            "payload, so anything found here belongs to something else and would "
            "be packaged into the role -- remove it or pick another --stage"
        )
    # Same ownership rule as `_build_one`: porter removes only what porter made.
    ours = not stage_dir.exists()
    stage_dir.mkdir(parents=True, exist_ok=True)
    try:
        return build_deb(stage_dir, meta.control, Path(out))
    finally:
        if ours:
            shutil.rmtree(stage_dir, ignore_errors=True)


def main() -> None:
    m.main()
