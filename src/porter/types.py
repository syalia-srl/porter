"""The shapes `assemble` consumes, and the manifest they are read from.

Deliberately **not** `spec.py`. That module is Task 7's: it owns validating a
`porter.yaml` -- required keys, unknown keys, multi-component manifests, useful
errors on a typo -- and it will absorb `from_manifest` below. Defining the
dataclasses here means Task 7 adds a validator rather than moving a definition
`assemble` already imports.

The schema is the example gallery's, not this file's. Every key read below
appears in `examples/service-fastapi/porter.yaml`, and a key no example carries
is not read at all: writing the schema in prose first is how the fields drift
from what the code reads (docs/design-spec.md).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Python:
    """Which interpreter, and whether this component carries it.

    porter hardcodes no version -- rule 10 -- so `version` comes from the
    manifest and reaches `interpreter.vendor()` unaltered. `package` is
    `bundled` (the interpreter lives inside this component's .deb) or the name
    of a package that provides it. Only `bundled` exists today; see
    `assemble._refuse_what_porter_cannot_emit` for why the other is a refusal
    rather than a default.
    """

    version: str = "3.12"
    package: str = "bundled"

    @property
    def bundled(self) -> bool:
        return self.package == "bundled"


@dataclass(frozen=True)
class BakeArtifact:
    """One thing a bake step must have produced, and how big it must be.

    `min_bytes` is not optional and not defaulted, and that is the whole point
    of the type existing rather than `artifacts` being a list of strings. An
    empty SQLite database is a valid file: it exists, it opens, it answers
    queries, and it has nothing in it. "the artifact is there" and "the
    artifact is real" are different questions, and only the second one is worth
    asking before a package ships to a client with no network.

    Declared as `None` here rather than required by the constructor so a
    manifest that omits it gets `bake`'s refusal, which says what is wrong,
    instead of a `TypeError` naming a field the adopter never typed.
    """

    path: str
    min_bytes: int | None = None


@dataclass(frozen=True)
class Component:
    """One installable unit: a package name, a payload, and how it is run.

    `module` and `args` and not a single `entrypoint` string, because rule 3 is
    `python -m <module>` and the two halves land in different places: the module
    is what the interpreter imports and what `assemble` proves importable, the
    args are the module's own. The gallery's ExecStart --
    `-m uvicorn app:app --host 127.0.0.1 --port ${PORT}` -- cannot be spelled at
    all with one string, and folding it into one is how `bin/uvicorn` creeps
    back in.
    """

    name: str
    package: str
    description: str
    kind: str  # service | command. See assemble.SUPPORTED_KINDS.
    module: str
    args: list[str] = field(default_factory=list)
    # Paths relative to `src_root`, each staged under its own basename in
    # /usr/lib/<pkg>/ -- which is the unit's WorkingDirectory, hence the import
    # root. See assemble's module docstring.
    source_paths: list[str] = field(default_factory=list)
    # Paths relative to `src_root`, staged under their own basename in
    # /usr/share/<pkg>/ -- the FHS contract's home for baked data, models and
    # static assets. Separate from `source_paths` because /usr/lib/<pkg> is the
    # import root: a corpus staged there is on sys.path, and a `data/` entry
    # holding a stray .py would be refused by a check meant for source.
    data_paths: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    # The `bake:` block. Commands run in src_root before anything is staged,
    # and what they must have produced. See porter.bake.
    bake_steps: list[str] = field(default_factory=list)
    bake_artifacts: list[BakeArtifact] = field(default_factory=list)
    # The whole env template. `split()` divides it by owner; membership in
    # admin_keys is the only thing that decides which half a key lands in.
    defaults: dict[str, str] = field(default_factory=dict)
    admin_keys: list[str] = field(default_factory=list)
    bin_name: str | None = None  # command kind only
    version: str = "1.0"
    architecture: str = "amd64"
    maintainer: str = "porter <porter@example.com>"

    @classmethod
    def from_manifest(cls, manifest: dict) -> tuple[Component, Python]:
        """Read a flat single-component `porter.yaml`.

        Flat because the gallery is flat: `examples/service-fastapi/porter.yaml`
        puts `package:` at the top level. A `components:` list is
        `examples/suite`'s to introduce, and it does not exist yet.

        No validation beyond KeyError. That is Task 7's, and a half-validator
        here would be the worse of the two outcomes: it would look like the
        real one.
        """
        py = manifest.get("python", {})
        bake = manifest.get("bake", {})
        return (
            cls(
                name=manifest.get("name", manifest["package"]),
                package=manifest["package"],
                description=manifest["description"],
                kind=manifest["kind"],
                module=manifest["exec"]["module"],
                args=list(manifest["exec"].get("args", [])),
                source_paths=list(manifest.get("source", [])),
                data_paths=list(manifest.get("data", [])),
                requirements=list(manifest.get("requirements", [])),
                bake_steps=list(bake.get("steps", [])),
                # `.get("min_bytes")` and not `["min_bytes"]`: an omitted
                # minimum is a manifest mistake worth a sentence, not a
                # KeyError whose whole message is the word it is missing.
                bake_artifacts=[
                    BakeArtifact(path=a["path"], min_bytes=a.get("min_bytes"))
                    for a in bake.get("artifacts", [])
                ],
                defaults=dict(manifest.get("env", {})),
                admin_keys=list(manifest.get("admin_keys", [])),
                bin_name=manifest.get("bin_name"),
                version=str(manifest["version"]),
                architecture=manifest["architecture"],
                maintainer=manifest["maintainer"],
            ),
            Python(version=str(py.get("version", "3.12")),
                   package=py.get("package", "bundled")),
        )
