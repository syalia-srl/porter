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
    of a package porter emits for it -- one 97 MB tree that every component of
    the project `Depends:` on by exact version, rather than one apiece.
    `assemble.assemble_interpreter` builds that package and
    `assemble.Interpreter` is what a component is handed to reach it.
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
    # Paths relative to `src_root`, staged under their own basename in
    # /usr/lib/<pkg>/ beside the source -- compiled programs the project built
    # or vendored, `llama-server` and its CUDA libraries being the case this
    # exists for. Separate from `source_paths` because they are not Python and
    # nothing about them is importable: they are copied with their mode intact
    # and read by `depends.derive_depends`, which is where rule 11's `Depends:`
    # comes from.
    native_binaries: list[str] = field(default_factory=list)
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
    # Component NAMES -- `alpha`, not `demo-alpha.service`. The manifest is
    # written in the author's vocabulary and systemd is spoken to in its own;
    # `systemd.resolve_ordering` is the single place that translates, so a
    # dependency can be misspelled in exactly one way and it is caught there.
    after: list[str] = field(default_factory=list)
    schedule: str | None = None  # oneshot kind only: the timer's OnCalendar=
    version: str = "1.0"
    architecture: str = "amd64"
    maintainer: str = "porter <porter@example.com>"
    # Declared last because a component acquires them last: a migration exists
    # only once there is a previous version to migrate *from*. See porter.migrate.
    migrations: list[Migration] = field(default_factory=list)
    # The escape hatch (docs/design-spec.md): a script, relative to `src_root`,
    # that takes over the ASSEMBLE stage entirely. When it is set, `assemble`
    # runs it instead of staging anything itself and every field above is unread
    # -- which is why `spec.py` refuses them beside it rather than letting them
    # be dropped in silence. See `assemble._run_the_build_hook`.
    build: str | None = None

    @classmethod
    def from_manifest(cls, manifest: dict) -> tuple[Component, Python]:
        """Read a flat single-component `porter.yaml`.

        Flat because the first gallery entry is flat:
        `examples/service-fastapi/porter.yaml` puts `package:` at the top
        level. `all_from_manifest` below reads the `components:` shape and
        comes back through here for each entry, so there is one reader and not
        two.

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
                # `.get` and not `[...]` for these two, because a `build:`
                # component declares neither: its script writes the whole stage,
                # so there is no kind for porter to branch on and no module for
                # porter to run. `spec.py` requires both of an ordinary
                # component and refuses both beside a hook, so the defaults here
                # are reachable only through the hatch -- and `custom` is not a
                # member of `assemble.SUPPORTED_KINDS`, so a component that
                # reaches the ordinary path carrying it is refused by name
                # rather than staged as something porter guessed.
                kind=manifest.get("kind", "custom"),
                module=manifest.get("exec", {}).get("module", ""),
                args=list(manifest.get("exec", {}).get("args", [])),
                source_paths=list(manifest.get("source", [])),
                data_paths=list(manifest.get("data", [])),
                native_binaries=list(manifest.get("native_binaries", [])),
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
                after=list(manifest.get("after", [])),
                schedule=manifest.get("schedule"),
                version=str(manifest["version"]),
                architecture=manifest["architecture"],
                maintainer=manifest["maintainer"],
                migrations=[
                    Migration(before_version=str(m["before_version"]),
                              script=m["script"])
                    for m in manifest.get("migrations", [])
                ],
                build=manifest.get("build"),
            ),
            Python(version=str(py.get("version", "3.12")),
                   package=py.get("package", "bundled")),
        )

    @classmethod
    def all_from_manifest(cls, manifest: dict) -> list[tuple[Component, Python]]:
        """Every component a manifest declares, flat shape or `components:`.

        A multi-service project is **one manifest and several packages**, not
        several manifests: `after:` names a sibling, and a sibling in another
        file is a reference porter cannot check. Both refusals in
        `systemd.resolve_ordering` need the whole list at once, so the list is
        what the reader produces.

        Top-level keys are the shared ones and an entry overrides them --
        `version`, `maintainer`, `architecture` and `python` are written once
        for the project, because three components of one suite that drift to
        three versions is the drift porter exists to stop.

        Returned as **pairs**, not `(components, one Python)`: a component may
        override `python:` and a single shared interpreter would be a claim
        this reader is not entitled to make.
        """
        if "components" not in manifest:
            return [cls.from_manifest(manifest)]
        shared = {k: v for k, v in manifest.items() if k != "components"}
        return [cls.from_manifest({**shared, **entry})
                for entry in manifest["components"]]


@dataclass(frozen=True)
class Migration:
    """One state migration, and the upgrade it fires on.

    Defined at the end of this file rather than beside `Component` for a
    mechanical reason worth writing down: three implementers shared this
    checkout on 2026-08-08, and a new dataclass wedged between the existing
    ones is a diff hunk that overlaps a peer's. The class is small and its
    reader arrives here from `Component.migrations` either way.

    `before_version` is the version the migration must run *before* -- normally
    the version of the package that carries it. `script` is `sh` text, spliced
    into the postinst rather than shipped as a file: a migration that is a path
    is a migration that can fail to be staged, and that failure looks exactly
    like an install with nothing to migrate.

    There is no `id` and no stamp file. dpkg's `$2` already says which version
    the client is coming from, and a stamp would live in `/var/lib/<pkg>/`,
    which rule 6 says the package never writes to.
    """

    before_version: str
    script: str
