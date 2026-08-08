"""Task 7's module, existing today only to make the seam a tripwire.

`Component` and `Python` are defined in `porter.types`, which `assemble`
imports. Task 7 owns *validating* a `porter.yaml` -- required keys, unknown
keys, multi-component manifests, useful errors on a typo -- and will absorb
`Component.from_manifest`. Defining the dataclasses in `types.py` means Task 7
adds a validator rather than moving a definition `assemble` already imports.

That intent was recorded in three docstrings and enforced by nothing, and the
divergence had already started: Task 4's brief writes
`from porter.spec import Component, Python` while the tests import
`porter.types`, so two module names were in play for one schema before Task 7
had been written. This file is the two lines that convert the note into a
tripwire -- `tests/test_types.py` asserts the classes are *the same objects*,
so a Task 7 that defines its own `Component` here takes the suite red instead
of quietly giving the gallery a second schema to drift from.
"""

from porter.types import Component, Python

__all__ = ["Component", "Python"]
