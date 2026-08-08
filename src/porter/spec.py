"""The validated reader for a porter.yaml, and the only one `porter build` uses.

**Extends `porter.types`, never redefines it.** `Component`, `Python` and
`BakeArtifact` are defined there and re-exported below, and
`tests/test_types.py` asserts the classes are *the same objects*: a second
definition here would give the gallery two schemas to drift between, so it
takes the suite red rather than going quiet. Task 4 recorded that intent in
three docstrings and enforced it with the re-export; this module is the
validator those docstrings were waiting for, added around the definitions
rather than in place of them.

What validation is *for* here is narrower than "check the input". porter's
characteristic bug is the silently dropped input, and every refusal below
stands where the alternative is a package that builds at rc=0, lints at rc=0,
installs at rc=0 and is wrong at a client with no network:

  - `admin_key:` for `admin_keys:` leaves the key in `env:`, so `split()` files
    it under /etc/<pkg>/defaults -- package-owned, a conffile, shipped inside
    the .deb and replaced on every upgrade. The client's secret is in the
    package and the admin's file never receives it.
  - `verson:` for `version:` falls back to the default interpreter, so the .deb
    carries a Python one release below what the payload needs.
  - `metapackage:` for `metapackages:` builds every component and no role at
    all, and the sysadmin's one-name runbook names a package not on the USB.
  - a role naming a component the manifest does not build produces a .deb whose
    `Depends:` cannot be satisfied. dpkg-deb never resolves anything, so the
    build is happy; `apt install` reports unmet dependencies at the client.

The allowlists are written by hand and that is the safe direction. A key added
to `Component.from_manifest` and forgotten here refuses a *correct* manifest --
loud, at build time, on the developer's machine. The reverse (reading a key
nothing validates) is what this module exists to end.

`load` merges the shared top-level keys into each entry and then calls
`Component.from_manifest`, which is what `Component.all_from_manifest` does in
one step. Inlined rather than delegated so validation can sit *between* the
merge and the construction: a required key may be inherited from the top level,
so it can only be checked once merged, and it must be checked before
`from_manifest` turns its absence into a bare `KeyError`.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from porter.types import BakeArtifact, Component, Migration, Python

__all__ = ["BakeArtifact", "Component", "Manifest", "Metapackage", "Migration",
           "Python", "load"]

# Every key a component may carry, shared or per-entry. Mirrors what
# `Component.from_manifest` reads, plus `python:`, which it reads separately.
COMPONENT_KEYS = frozenset({
    "name", "package", "version", "description", "maintainer", "architecture",
    "kind", "python", "requirements", "exec", "source", "data", "bake", "env",
    "admin_keys", "bin_name", "after", "schedule", "migrations", "build",
    "native_binaries",
})
# The keys ONLY the built-in assembler reads. A `build:` component's script
# writes the whole stage -- interpreter, payload, unit, wrapper, config, the lot
# -- so `assemble` reads not one of these, and reading none of them is exactly
# how they would be dropped in silence. `kind: service` beside a `build:` is the
# worst of them: the adopter waits for a unit porter never writes, and the
# package installs at rc=0 with nothing to start.
#
# `bake` is deliberately NOT here. It runs before assembly, in `porter.bake`,
# and it is orthogonal to who stages the tree -- a hook component with an ETL
# still wants its artefacts checked for magnitude and for a stranded WAL.
# `version`, `description`, `maintainer`, `architecture` and `name` are not here
# either: they are control fields, and the hook receives every one of them in
# its environment.
ASSEMBLER_ONLY_KEYS = frozenset({
    "kind", "exec", "python", "requirements", "source", "data", "env",
    "admin_keys", "bin_name", "after", "schedule", "migrations",
    "native_binaries",
})
# The top level is the component keys (a flat manifest is one component written
# there) plus the blocks that may only appear at the top. `desktop:` is one of
# those: a project has at most one window, whatever number of services it runs,
# and the block names the component that window opens. Its own keys are
# validated by `porter.desktop.Desktop.from_manifest` -- which is where the
# refusals about them belong, next to the launcher those keys are baked into.
TOP_LEVEL_KEYS = COMPONENT_KEYS | {"components", "metapackages", "desktop"}
METAPACKAGE_KEYS = frozenset({"package", "description", "depends", "version",
                              "architecture", "maintainer"})
PYTHON_KEYS = frozenset({"version", "package"})
EXEC_KEYS = frozenset({"module", "args"})

# Required after the shared keys are merged in, not before: `version` is
# normally written once at the top of a multi-component manifest.
#
# `exec` and not `exec.module`, and `bake`'s and `migrations`' own sub-keys are
# unvalidated here on purpose. Both already refuse legibly one layer down --
# `bake` names the artefact it could not find, `migrate` names the version --
# and a validator that half-covers a block is worse than one that does not
# touch it, because it reads like the real thing.
REQUIRED_COMPONENT_KEYS = ("package", "description", "kind", "exec")
# A hook component needs neither. `kind` selects between the branches of
# `assemble` and a hook takes all of them over; `exec` names the module porter
# runs and porter runs nothing here. Requiring them anyway would mean every
# escape-hatch manifest carrying two fields whose values nothing reads -- which
# is the drift the hatch exists to avoid, written into the schema.
REQUIRED_HOOK_COMPONENT_KEYS = ("package", "description", "build")
REQUIRED_METAPACKAGE_KEYS = ("package", "description", "depends")


@dataclass(frozen=True)
class Metapackage:
    """One machine role: a name a sysadmin installs, and what it pulls in.

    Deliberately **not** a `Component`, and deliberately not in `types.py`.
    `types.py` holds the shapes `assemble` consumes, and a metapackage never
    reaches `assemble`: it has no payload, no interpreter, no unit, no module
    to import and nothing to stage. Forcing it into `Component` would mean
    inventing a `module` for something that runs nothing -- and `module` is the
    field three separate refusals in `assemble` are built on. It is packaged
    straight from an empty stage, by `cli`, out of these six fields.

    `architecture` defaults to `all` rather than inheriting the manifest's.
    A package with no binaries in it is not architecture-specific, and pinning
    it to `amd64` would make the one package on the USB with nothing
    machine-dependent inside it the one an arm64 client cannot install.
    """

    package: str
    description: str
    depends: list[str] = field(default_factory=list)
    version: str = "1.0"
    architecture: str = "all"
    maintainer: str = "porter <porter@example.com>"

    @property
    def control(self) -> dict[str, str]:
        """The whole of the .deb. `Depends:` is comma-separated, as dpkg reads it."""
        return {
            "Package": self.package,
            "Version": self.version,
            "Architecture": self.architecture,
            "Maintainer": self.maintainer,
            "Description": self.description,
            "Depends": ", ".join(self.depends),
        }


@dataclass(frozen=True)
class Manifest:
    """Everything one porter.yaml declares.

    Components come back as **pairs**, keeping `all_from_manifest`'s contract:
    a component may override `python:`, so a single shared interpreter would be
    a claim this reader is not entitled to make.
    """

    components: list[tuple[Component, Python]]
    metapackages: list[Metapackage]
    path: Path
    # The distinct shared interpreters this manifest asks porter to emit a
    # package for -- one per `python.package` that is not `bundled`. Computed
    # here rather than by `porter build` because the refusals it needs are
    # cross-component ones, and a caller re-deriving the list is a caller that
    # can derive a different one.
    interpreters: list[Python] = field(default_factory=list)


def _refuse_unknown_keys(where: str, mapping: Mapping, allowed: frozenset) -> None:
    """A key porter does not read is a key porter silently drops."""
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(
            f"{where}: unknown key(s) {', '.join(repr(k) for k in unknown)}. "
            f"porter reads {', '.join(sorted(allowed))} and nothing else, so a "
            "key it does not know is read by nothing and reported by nothing -- "
            "the package builds and installs without whatever that key was "
            "meant to configure. Fix the spelling, or delete the key"
        )


def _refuse_missing_keys(where: str, mapping: Mapping,
                         required: Iterable[str]) -> None:
    """Named here, so the error is not a bare `KeyError` naming one word."""
    missing = [key for key in required if key not in mapping]
    if missing:
        raise ValueError(
            f"{where}: missing required key(s) {', '.join(repr(k) for k in missing)}. "
            f"Every porter.yaml entry needs {', '.join(required)}; a shared value "
            "may be written once at the top level and inherited"
        )


def _refuse_a_role_naming_a_package_the_manifest_does_not_build(
        where: str, metapackages: list[Metapackage],
        components: list[Component]) -> None:
    """A `Depends:` porter cannot satisfy from its own USB.

    dpkg-deb resolves nothing and never has, so the metapackage builds at rc=0
    with any string in it at all. The failure lands on the airgapped client, as
    `apt install <role>` reporting unmet dependencies, at the one moment there
    is no network to fetch the missing name from.

    A porter package `Depends:` only on packages porter itself produced
    (docs/design-spec.md), so "not in this manifest" is the whole predicate --
    there is no external package a role is entitled to name.
    """
    built = {component.package for component in components}
    for meta in metapackages:
        if not meta.depends:
            raise ValueError(
                f"{where}: metapackage {meta.package!r} depends on nothing. It "
                "would install successfully and deliver not one component, "
                "which is the one outcome a machine role must not have"
            )
        for name in meta.depends:
            if name not in built:
                raise ValueError(
                    f"{where}: metapackage {meta.package!r} depends on {name!r}, "
                    f"which no component in this manifest builds "
                    f"({', '.join(sorted(built))}). dpkg-deb resolves nothing, so "
                    "this .deb would build at rc=0 and `apt install` would fail "
                    "with unmet dependencies on the client"
                )


def _refuse_two_packages_with_one_name(where: str, names: list[str]) -> None:
    """One `<package>_<version>_<arch>.deb` filename, two packages.

    `build_deb` names the artefact after the control fields, so the second
    build overwrites the first: a manifest declaring four packages puts three
    on the USB, and both builds exit 0. Components and metapackages are checked
    together because the collision is worst across the two -- a role sharing a
    name with a component would depend on itself and replace a 91 MB payload
    with a 20 KB stub.
    """
    duplicates = sorted(name for name, n in Counter(names).items() if n > 1)
    if duplicates:
        raise ValueError(
            f"{where}: package name(s) {', '.join(repr(d) for d in duplicates)} "
            "declared twice. Both would be written to one .deb filename and the "
            "second would overwrite the first, silently, at rc=0"
        )


def _shared_interpreters(where: str, pairs: list[tuple[Component, Python]],
                         package_names: list[str]) -> list[Python]:
    """The interpreter packages to build, and the two ways the ask is incoherent.

    - **One name, two versions.** Two components declaring
      `python: {package: proj-python}` with `version: 3.12` and `version: 3.13`
      describe one package holding two trees. porter builds one, whichever it
      staged last, and the other component's ExecStart names a `python3.13`
      that is not in it -- an install apt resolves happily and a service that
      never starts. There is no merge available: the package has one payload.
    - **A name something else already claims.** An interpreter package sharing
      a name with a component or a metapackage is two .debs written to one
      filename, the second overwriting the first -- the same failure
      `_refuse_two_packages_with_one_name` exists for, arriving through a key
      that check cannot see because `python.package` is not a package *entry*.
      Whichever lands second wins, at rc=0, and the USB carries a 97 MB
      interpreter under a component's name or a component under the
      interpreter's.
    """
    by_name: dict[str, Python] = {}
    for component, python in pairs:
        if python.bundled:
            continue
        if python.package in package_names:
            raise ValueError(
                f"{where}: component {component.name!r} declares python.package="
                f"{python.package!r}, which is also the name of a package this "
                "manifest builds. Both .debs would be written to one filename and "
                "the second would overwrite the first, silently, at rc=0"
            )
        seen = by_name.get(python.package)
        if seen is not None and seen.version != python.version:
            raise ValueError(
                f"{where}: python.package={python.package!r} is declared with "
                f"version {seen.version!r} and version {python.version!r}. One "
                "package cannot hold two interpreters -- porter would build "
                "whichever it staged last, and the other component's ExecStart "
                "would name a python that is not in it"
            )
        by_name[python.package] = python
    return list(by_name.values())


def _refuse_assembler_keys_beside_a_build_hook(where: str, written: Mapping) -> None:
    """A key the hook makes porter stop reading is a key porter would drop.

    The escape hatch's whole claim is that it bypasses *assembly* and nothing
    else, so the fields assembly alone consumes have no reader once it is on.
    `source:` beside a `build:` is the plain case -- the payload the adopter
    listed is never copied and the script's tree ships instead -- and
    `admin_keys:` is the expensive one: no `<pkg>-setup`, no `/etc/<pkg>/env`,
    and an operator with a wizard the manifest promised and the package does
    not carry. Every one of them builds, lints and installs at rc=0.

    Read off the entry AS WRITTEN, never the merged one. `python:`, `version:`
    and friends are shared top-level keys in a `components:` manifest, so a
    project with one hook component beside three ordinary ones would otherwise
    be refused for a key it never wrote next to the hook.
    """
    declared = sorted(set(written) & ASSEMBLER_ONLY_KEYS)
    if declared:
        raise ValueError(
            f"{where}: declares build: and also "
            f"{', '.join(repr(k) for k in declared)}. A build hook writes the whole "
            "stage, so porter reads none of those -- they would be accepted here "
            "and dropped, and the package would build, lint and install at rc=0 "
            "without whatever they asked for. Delete them, or delete build: and "
            "let porter assemble the component"
        )


def _component_pairs(where: str, entries: list[Mapping],
                     written: list[Mapping]) -> list[tuple[Component, Python]]:
    """Validate each merged entry, then hand it to `types.Component`.

    `written` is the same list before the shared top-level keys were merged in,
    and only the hook refusal uses it -- see
    `_refuse_assembler_keys_beside_a_build_hook`.
    """
    pairs = []
    for index, entry in enumerate(entries):
        label = f"{where}: component {entry.get('name', entry.get('package', index))!r}"
        if entry.get("build"):
            _refuse_missing_keys(label, entry, REQUIRED_HOOK_COMPONENT_KEYS)
            _refuse_assembler_keys_beside_a_build_hook(label, written[index])
        else:
            _refuse_missing_keys(label, entry, REQUIRED_COMPONENT_KEYS)
        _refuse_unknown_keys(label, entry, COMPONENT_KEYS)
        _refuse_unknown_keys(f"{label} python:", entry.get("python", {}), PYTHON_KEYS)
        _refuse_unknown_keys(f"{label} exec:", entry.get("exec", {}), EXEC_KEYS)
        pairs.append(Component.from_manifest(dict(entry)))
    return pairs


def _metapackages(where: str, entries: list[Mapping],
                  shared: Mapping) -> list[Metapackage]:
    """Read the roles, inheriting `version` and `maintainer` but not `architecture`."""
    roles = []
    for index, entry in enumerate(entries):
        label = f"{where}: metapackage {entry.get('package', index)!r}"
        _refuse_unknown_keys(label, entry, METAPACKAGE_KEYS)
        _refuse_missing_keys(label, entry, REQUIRED_METAPACKAGE_KEYS)
        roles.append(Metapackage(
            package=entry["package"],
            description=entry["description"],
            depends=list(entry["depends"]),
            version=str(entry.get("version", shared.get("version", "1.0"))),
            # Not `shared.get("architecture")`: see the class docstring.
            architecture=entry.get("architecture", "all"),
            maintainer=entry.get("maintainer",
                                 shared.get("maintainer",
                                            "porter <porter@example.com>")),
        ))
    return roles


def load(path: Path | str) -> Manifest:
    """Read and validate a porter.yaml. Raises `ValueError` on anything wrong."""
    path = Path(path)
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, Mapping):
        raise ValueError(
            f"{path}: a porter.yaml is a mapping, and this is {type(doc).__name__}. "
            "An empty file parses as None and would otherwise build nothing at rc=0"
        )
    _refuse_unknown_keys(str(path), doc, TOP_LEVEL_KEYS)

    # Shared keys are everything that is not one of the top-level-only blocks;
    # an entry overrides them. This is `all_from_manifest`'s merge, inlined so
    # the validation above and below can sit either side of it -- see the module
    # docstring. `desktop:` is excluded for the same reason `components:` is:
    # merged in, it would reach `_component_pairs` and be refused there as an
    # unknown *component* key, which is a correct manifest rejected with an
    # error naming a key that is spelled right.
    shared = {k: v for k, v in doc.items()
              if k not in ("components", "metapackages", "desktop")}
    entries = doc["components"] if "components" in doc else [shared]
    merged = [{**shared, **entry} for entry in entries] if "components" in doc \
        else [shared]

    components = _component_pairs(str(path), merged, entries)
    metapackages = _metapackages(str(path), doc.get("metapackages", []), shared)

    # Duplicates first, and the order is not arbitrary: a collision makes the
    # role check's own message wrong. Rename one component onto another's
    # package and the set of built names loses an entry, so the role that
    # depended on the shadowed one is reported as naming a component "no
    # component in this manifest builds" -- which is false, and sends the
    # adopter to the wrong line of the manifest.
    names = [c.package for c, _ in components] + [m.package for m in metapackages]
    _refuse_two_packages_with_one_name(str(path), names)
    _refuse_a_role_naming_a_package_the_manifest_does_not_build(
        str(path), metapackages, [c for c, _ in components])
    return Manifest(components=components, metapackages=metapackages, path=path,
                    interpreters=_shared_interpreters(str(path), components, names))
