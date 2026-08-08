"""porter's command line. Thin by design: every verb is a call into a module.

`build` is the composition the rest of the package exists for -- assemble a
manifest into a staged tree, then package that tree -- and until now it had no
body, so nothing in porter ever ran assemble and build_deb in sequence outside a
test fixture.

Task 7 replaces `Component.from_manifest` with a validating loader in
`porter.spec`; the two lines below it do not change.
"""
from __future__ import annotations

from pathlib import Path

import microcli as m
import yaml

from porter.assemble import assemble
from porter.deb import build_deb
from porter.types import Component


@m.command
def build(manifest: str, out: str = "dist", stage: str = "build") -> None:
    """Build a .deb from a porter.yaml.

    manifest: path to the porter.yaml
    out: directory the .deb is written to
    stage: directory the tree is assembled under (must not already exist)
    """
    manifest_path = Path(manifest).resolve()
    component, python = Component.from_manifest(
        yaml.safe_load(manifest_path.read_text()))
    # Source paths are relative to the manifest, so a build run from anywhere
    # stages the same tree. A cwd-relative read is the shape that works in the
    # repo root and fails in CI.
    staged = assemble(component, python, manifest_path.parent,
                      Path(stage) / component.package)
    deb = build_deb(staged.stage, staged.control, Path(out),
                    conffiles=staged.conffiles, scripts=staged.scripts)
    m.ok(f"{deb} ({deb.stat().st_size // 1024} KiB)")


def main() -> None:
    m.main()
