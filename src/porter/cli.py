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
from pathlib import Path
from typing import Annotated

import microcli as m
import yaml

from porter.assemble import assemble
from porter.deb import build_deb
from porter.types import Component


@m.command
def build(
    manifest: Annotated[str, "path to the porter.yaml"],
    out: Annotated[str, "directory the .deb is written to"] = "dist",
    stage: Annotated[str, "scratch directory the tree is assembled under; "
                          "porter removes the tree it creates there"] = "build",
) -> None:
    """Build a .deb from a porter.yaml."""
    manifest_path = Path(manifest).resolve()
    component, python = Component.from_manifest(
        yaml.safe_load(manifest_path.read_text()))
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
        staged = assemble(component, python, manifest_path.parent, stage_dir)
        deb = build_deb(staged.stage, staged.control, Path(out),
                        conffiles=staged.conffiles, scripts=staged.scripts)
    finally:
        if ours:
            shutil.rmtree(stage_dir, ignore_errors=True)
    m.ok(f"{deb} ({deb.stat().st_size // 1024} KiB)")


def main() -> None:
    m.main()
