"""A Component becomes a staged tree that `build_deb` can package unmodified.

Tasks 1-3 built the primitives -- a relocatable interpreter, a linted .deb, the
config split, the unit -- and nothing composed them. `porter build` had no body,
and every staged tree in the suite was hand-built in a fixture. This module is
that composition, and it is the only place that knows porter's client layout:

    /usr/lib/<pkg>/python/          the vendored interpreter (rule 1)
    /usr/lib/<pkg>/<basename>       each source_paths entry
    /usr/lib/systemd/system/<pkg>.service
    /usr/bin/<bin_name>             command kinds only
    /etc/<pkg>/defaults             package-owned half of rule 4, a conffile
    /usr/share/<pkg>/env.example    admin-owned half -- COPIED into /etc by
                                    postinst, never shipped there

`/usr/lib/<pkg>` is both the payload root and the unit's `WorkingDirectory`, and
that is what puts the app on `sys.path`: `python -m` prepends the working
directory, and on a `ProtectSystem=strict` unit nothing else will. So a source
entry is staged under its own basename and `module` names it --
`source_paths=["src/app.py"]` with `-m uvicorn app:app` is the gallery entry,
and `app` is importable for exactly that reason. Staging `src/` whole would put
the payload one directory below the import root and the service would fail at
its first request, on the client, with the package having installed at rc=0.
Three checks hold that, and it takes all three, because each is blind to the
others' failure: `module` must be importable (the runner -- `uvicorn`), the
payload's own top-level imports must be importable (`fastapi`, which is what
the gallery's app actually needs and which `module` never names), and a source
entry may not be a directory that leaves its modules one level below the import
root (`source: ["src"]`). Only the third can catch that last one: a directory
with no `__init__.py` is a *namespace package*, so `find_spec("src")` succeeds
while nothing inside it is importable (measured 2026-08-08).

Refusals here follow deb.py's precedent: **refuse, never repair.** A stage
porter quietly fixes is a stage whose next surprise ships.

**And a component may decline all of it.** `build: <script>` is the escape
hatch of docs/design-spec.md: the script is handed an empty stage and writes
the whole tree, and everything above -- the interpreter, the layout, the unit,
the wrapper, the config split, the import probes -- is skipped. What is *not*
skipped is the rest of the pipeline: the FHS lint, the `sh -n` pass, the
`Depends:` derivation, the conffile derivation, packaging, the gate and the
repo all run on the script's output exactly as they run on porter's own. The
hatch bypasses assembly, not the guarantees -- a custom build cannot ship
anything the built-in assembler would be refused for.

That is the point of it existing. porter serves four repos it was not designed
around, so the first component whose shape the schema does not anticipate would
otherwise fork the tool; the spec's phrase is that porter must not become "a
second thing to fight". The cost of the hatch is that porter reads none of the
manifest's assembly keys any more, which is why `spec.py` refuses them beside
it rather than accepting and dropping them.
"""
from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# `_bash` is private to bake and imported anyway, rather than copied: it is the
# one place that says bash is REFUSED when absent and never substituted with sh,
# and two copies of that decision is one copy that can be relaxed alone.
from porter.bake import _bash
from porter.config import env_postinst, setup_script, split
from porter.depends import (
    ELF_MAGIC,
    derive_depends,
    refuse_a_native_object_the_target_cannot_run,
)
from porter.interpreter import install, vendor
from porter.systemd import resolve_ordering, timer, unit
from porter.types import Component, Python

# The shapes porter can emit a *correct* package for today.
#
# `oneshot` was absent until Task 11, and the refusal that stood here is worth
# remembering: it was not squeamishness, it was that `unit()` emitted no `Type=`
# and no `.timer` and the postinst enabled `<pkg>.service` unconditionally, so a
# job pushed through them installed at rc=0 and ran as a permanently-restarting
# service. All three are now emitted correctly (`systemd.unit(kind="oneshot")`,
# `systemd.timer`, and `[Install] Also=` so that the *existing* postinst enables
# the timer), and `examples/oneshot-timer` is the entry that defines the shape.
SUPPORTED_KINDS = ("service", "command", "oneshot")

# The kinds that get a systemd unit rather than a /usr/bin wrapper.
UNIT_KINDS = ("service", "oneshot")

# Basenames `assemble` itself writes into /usr/share/<pkg>/. A `data:` entry
# with one of these names would be staged first and then overwritten, at rc=0,
# with the manifest still claiming it shipped.
RESERVED_SHARE_NAMES = ("VERSION", "env.example")

# What `admin_keys` may be spelled as. `setup_script` interpolates each key into
# a shell IDENTIFIER (`{key}_value=$new`) and into an ERE alternation
# (`grep -vE '^({managed})='`), and `split()` writes it as `KEY=value` into a
# file systemd parses as an environment file. All three want the same shape.
SHELL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass
class Interpreter:
    """A vendored interpreter that lives in a package of its own.

    The measurement this shape exists for: the tree is **97 MB before a single
    dependency**, and ainbox has ten services. Bundled, that is ~780 MB of the
    same bytes ten times over on one USB stick and in one client's /usr/lib.
    Shared, it is 97 MB once and a `Depends:` line per component.

    `version` is the **full CPython version the staged tree reports** --
    `3.12.13`, not the project's `1.0`. Two reasons, and the first is a
    correctness one: components may carry different `version:` values, and an
    exact-version dependency keyed on the project's would then be unsatisfiable
    for whichever component disagreed. The second is that the interpreter is not
    a thing the project versions -- rebuilding a project must not churn a 30 MB
    package whose contents did not change, and a patch bump of CPython *must*
    churn it, because a component's compiled wheels were selected against that
    ABI.

    `python_bin` is the staged binary on the **build host** -- what the import
    probes exec and what `uv pip install --python` selects wheels for.
    `installed_python` is the path on the **client**, which is what reaches
    ExecStart and the wrapper. Keeping the two apart is rule 1's whole subject:
    an ExecStart naming a build directory works perfectly on the machine that
    produced it.
    """

    package: str
    version: str
    python_version: str
    stage: Path
    python_bin: Path
    control: dict[str, str]

    @property
    def installed_python(self) -> str:
        return f"/usr/lib/{self.package}/python/bin/python{self.python_version}"

    @property
    def exact_dependency(self) -> str:
        """What a component puts in `Depends:`. Exact, and that is the point.

        `>=` would let a client keep an older interpreter that satisfies the
        constraint and run wheels compiled against a newer ABI -- an install at
        rc=0 and an `ImportError` in a unit's status. The whole USB moves
        together or not at all.
        """
        return f"{self.package} (= {self.version})"


@dataclass
class Staged:
    """Everything `build_deb` needs, so the caller re-derives none of it.

    `conffiles` above all. deb.py's lint refuses any path under /etc it was not
    handed, so a list rebuilt at the call site is a list that can disagree with
    the tree -- and it would disagree at build time, on a real client's package,
    about the one file dpkg is not allowed to overwrite.
    """

    stage: Path
    conffiles: list[str]
    control: dict[str, str]
    scripts: dict[str, str]


def _refuse_what_porter_cannot_emit(component: Component, python: Python,
                                    interpreter: Interpreter | None) -> None:
    """Refuse the shapes porter could only package *wrongly*.

    Each of these has a plausible-looking wrong answer, which is why it is a
    refusal and not a default:

    - **A misspelled kind.** `kind: sevice` would otherwise stage a payload with
      no unit and no wrapper: a .deb that builds, lints, installs cleanly and
      does nothing at all.
    - **A shared interpreter nobody built.** `python.package: <name>` means the
      interpreter arrives in a package of its own, and `assemble_interpreter`
      is what builds it. A caller that declares one and hands over no
      `interpreter=` has no path to put in ExecStart, no binary to probe
      imports with and no version to depend on -- and the plausible wrong answer
      is to fall back to bundling, which would emit a package that works
      perfectly and is 97 MB larger than the manifest asked for, with the
      shared package never built and every other component depending on it.
    - **A command with config.** postinst creates `/etc/<pkg>/env` at
      `600 root:root`, which a command run by a non-root operator cannot read,
      and the wrapper does not source `/etc/<pkg>/defaults`, so the shipped half
      would be inert. Both are decisions the gallery has not made.
    - **A command with no `bin_name`.** There is no name to install under.
    - **A command declaring `migrations:`.** The command branch emits no
      postinst, so dpkg would have nothing to call them from: the upgrade would
      succeed at rc=0 having migrated nothing.
    - **A command declaring `after:`.** A wrapper in /usr/bin is not a unit;
      systemd has nothing to order, so the declaration would be read, accepted
      and dropped. porter's characteristic bug is the silently ignored input,
      not the loud one.
    - **A `service` or a `command` declaring `schedule:`.** The schedule reaches
      a `.timer` that only the oneshot branch writes, so a service that declares
      one gets a package that runs continuously and never on the calendar its
      author asked for -- at rc=0, with the manifest saying otherwise. A
      `command` is worse still: it emits no unit at all, so the schedule is read
      and dropped with nothing anywhere to notice.
    - **A `oneshot` declaring no `schedule:`.** Refused *here*, and not only in
      `timer()`, so that the refusal really does land before anything is staged
      -- `assemble` reaches `timer()` after `vendor()` has materialised ~97 MB
      and after `<pkg>.service` has been written. The sibling refusal above has
      always been here; this one was not, and a test claiming it was is the
      thing that gets believed.
    - **An `admin_keys` entry that is not a shell identifier.** `MY-KEY` is
      spliced into `<pkg>-setup` as `MY-KEY_value=$new`, which PARSES -- it is a
      command named `MY-KEY_value` -- and fails at run time on the client, and
      into `grep -vE '^(MY-KEY)='`, where a key like `A.B` silently matches
      lines it was never meant to drop. `sh -n` in deb.py cannot see either.
    - **A `data:` entry named `VERSION` or `env.example`.** Both are written by
      `assemble` into the same `/usr/share/<pkg>/`, after the data is staged, so
      the adopter's file is overwritten at rc=0 with the manifest still listing
      it.
    """
    for entry in component.data_paths:
        if Path(entry).name in RESERVED_SHARE_NAMES:
            raise ValueError(
                f"data entry {entry!r} would be staged as /usr/share/{component.package}"
                f"/{Path(entry).name}, which porter writes itself "
                f"({', '.join(RESERVED_SHARE_NAMES)} are the provenance stamp and "
                "the admin env template). It would be overwritten and the package "
                "would ship porter's file under the manifest's name -- rename it"
            )
    if component.kind not in SUPPORTED_KINDS:
        raise ValueError(
            f"unknown component kind {component.kind!r}: porter emits "
            f"{', '.join(SUPPORTED_KINDS)}. A kind with no branch here would stage a "
            "payload with neither a unit nor a wrapper -- a package that installs "
            "and does nothing"
        )
    if python.bundled and interpreter is not None:
        raise ValueError(
            f"component {component.name!r} declares python.package: 'bundled' and "
            f"was handed the shared interpreter {interpreter.package!r}. One of "
            "the two is a mistake, and the silent reading -- bundle anyway -- "
            "ships a second 97 MB interpreter the manifest did not ask for"
        )
    if not python.bundled:
        if interpreter is None:
            raise ValueError(
                f"component {component.name!r} declares python.package="
                f"{python.package!r} and no interpreter package was built for it. "
                "`porter build` builds one per distinct (package, version) in the "
                "manifest and hands it to `assemble`; a caller that does not is "
                "asking for an ExecStart porter would have to guess"
            )
        if interpreter.package != python.package:
            raise ValueError(
                f"component {component.name!r} declares python.package="
                f"{python.package!r} and was handed {interpreter.package!r}. The "
                "component would Depends: on one package and ExecStart the path "
                "of the other -- an install that resolves and a service that "
                "cannot start"
            )
        if interpreter.python_version != python.version:
            raise ValueError(
                f"component {component.name!r} declares python.version="
                f"{python.version!r} and was handed an interpreter package built "
                f"for {interpreter.python_version!r}. ExecStart names "
                f"python{python.version}, which that package does not contain"
            )
    if component.kind != "oneshot" and component.schedule:
        raise ValueError(
            f"component {component.name!r} is kind {component.kind!r} and declares "
            f"schedule: {component.schedule!r}. Only 'oneshot' emits a .timer, so "
            "the schedule would be silently dropped -- a service would run "
            "continuously and a command would not run at all. Use kind: oneshot"
        )
    if component.kind == "oneshot" and not component.schedule:
        raise ValueError(
            f"component {component.name!r} is kind 'oneshot' and declares no "
            "schedule: a timer with no OnCalendar= is a unit that loads, enables "
            "and never fires. Add `schedule:` to the manifest"
        )
    for key in component.admin_keys:
        if not SHELL_IDENTIFIER.match(key):
            raise ValueError(
                f"admin_keys entry {key!r} is not a shell identifier "
                f"([A-Za-z_][A-Za-z0-9_]*). {component.package}-setup writes it as "
                f"`{key}_value=$new`, which sh PARSES as a command and fails only "
                "on the client, and greps it as `^(...)=`, where a '.' or a '|' "
                "matches keys nobody named. Rename the key"
            )
    if component.kind == "command":
        if not component.bin_name:
            raise ValueError("a 'command' component needs bin_name: there is no name "
                             "to install under /usr/bin")
        if component.after:
            raise ValueError(
                f"a 'command' component may not declare after: "
                f"{component.after!r}. It installs a wrapper in /usr/bin and no "
                "unit, so there is nothing for systemd to order and the "
                "declaration would be read and dropped"
            )
        if component.defaults or component.admin_keys:
            raise ValueError(
                "a 'command' component may not declare config: postinst would create "
                "/etc/<pkg>/env at 600 root:root, unreadable by the operator running "
                "the command, and the wrapper sources nothing -- the shipped defaults "
                "would be inert. Add examples/command first"
            )
        if component.migrations:
            raise ValueError(
                f"a 'command' component may not declare migrations: "
                f"{len(component.migrations)} declared. The command branch below "
                "emits no postinst at all, so dpkg would never call them -- the "
                "package would upgrade at rc=0 having migrated nothing, which is "
                "exactly the silence porter.migrate exists to end"
            )


def _probe_import(python_bin: Path, workdir: Path,
                  name: str) -> subprocess.CompletedProcess:
    """Can the **staged** interpreter find `name` from the **payload directory**?

    Those two are what decide the answer on the client, so both are what the
    probe uses -- with `PYTHONPATH` and `PYTHONHOME` stripped, so the build
    host's environment cannot answer on the client's behalf. `find_spec`
    locates without importing, so a module with import-time side effects is not
    executed here.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    return subprocess.run(
        [str(python_bin), "-c",
         ("import importlib.util, sys; "
          "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)"),
         name],
        cwd=workdir, env=env, capture_output=True, text=True)


def _refuse_a_module_the_interpreter_cannot_import(
        python_bin: Path, workdir: Path, module: str) -> None:
    """The staged interpreter must be able to find the module the package runs.

    This is the airgap failure that looks exactly like success on the build
    host. `requirements` omitted, misspelled, or installed into some *other*
    interpreter leaves a .deb that builds, lints, installs, and then dies at
    ExecStart with `ModuleNotFoundError` -- on a client with no network to fix
    it from and, for a service, with the failure visible only in the unit's
    status. Nothing else in the pipeline reads `module` at all: deb.py sees a
    directory of bytes, systemd.py sees a string.

    This covers the **runner** and only the runner. `module` is `uvicorn` for
    the gallery and the payload is uvicorn's *argument*, so
    `_refuse_an_import_the_payload_makes_that_the_interpreter_cannot_find` is
    the other half and neither substitutes for the other.
    """
    probe = _probe_import(python_bin, workdir, module)
    if probe.returncode != 0:
        raise RuntimeError(
            f"the staged interpreter cannot import {module!r} from the payload "
            f"directory ({workdir}), so ExecStart would fail on the client. "
            f"Is it missing from requirements? rc={probe.returncode} "
            f"{probe.stderr.strip()}"
        )


def _payload_front_doors(libdir: Path, source_paths: list[str]) -> list[Path]:
    """The staged files the client imports first, and only those.

    A `.py` entry is its own front door; a package directory's is its
    `__init__.py`. Everything the package imports beyond those is reached
    *through* them, and walking a payload in full is how a `tests/` directory
    inside it makes `import pytest` a build-time requirement.
    """
    doors = []
    for entry in source_paths:
        staged = libdir / Path(entry).name
        if staged.is_dir():
            if (staged / "__init__.py").exists():
                doors.append(staged / "__init__.py")
        elif staged.suffix == ".py":
            doors.append(staged)
    return doors


def _unconditional_imports(path: Path) -> list[str]:
    """The module names `path` imports at import time, no matter what.

    Direct children of the module body only, deliberately. An import inside a
    `try: ... except ImportError:` or under an `if sys.platform ...` is an
    optional dependency, and refusing a package for one it declares optional is
    a refusal that fires on a *correct* manifest. A relative import names
    nothing the interpreter could be asked about. `import a.b` is asked as `a`,
    because `find_spec` on a dotted name imports the parent package to find the
    child -- and nothing here may execute payload code.
    """
    names = []
    for node in ast.parse(path.read_text(), filename=str(path)).body:
        if isinstance(node, ast.Import):
            names += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split(".")[0])
    return sorted(set(names))


def _refuse_an_import_the_payload_makes_that_the_interpreter_cannot_find(
        python_bin: Path, libdir: Path, source_paths: list[str]) -> None:
    """The payload's own dependencies, which `module` does not name.

    The check above proves the *runner* importable. For the gallery that is
    `uvicorn` -- and `app:app` is uvicorn's argument, which nothing in porter
    parses, so what the app itself needs is invisible to it. Drop `fastapi`
    from `requirements` and the runner probe is perfectly satisfied: uvicorn
    imports, the .deb builds, lints and installs at rc=0, and the first HTTP
    request dies on a client with no network to fix it from. That is the same
    airgap failure one argument further out, and it is the likelier of the two
    -- a runner is named in `exec:` where it is hard to forget, and a payload's
    imports are named nowhere in the manifest at all.

    Read statically and probed with `find_spec`: neither this nor the runner
    check may *execute* payload code, because a module that opens a socket or
    reads its config at import time is a legitimate payload and would otherwise
    fail the build.
    """
    for door in _payload_front_doors(libdir, source_paths):
        for name in _unconditional_imports(door):
            found = _probe_import(python_bin, libdir, name)
            if found.returncode != 0:
                raise RuntimeError(
                    f"the payload's {door.name} imports {name!r} and the staged "
                    f"interpreter cannot find it from the payload directory "
                    f"({libdir}), so the package installs at rc=0 and dies the "
                    f"first time that import runs on the client. Is it missing "
                    f"from requirements? rc={found.returncode} {found.stderr.strip()}"
                )


def _refuse_a_source_directory_below_the_import_root(
        src_root: Path, source_paths: list[str]) -> None:
    """A staged directory whose modules nothing on the client can import.

    `source: ["src"]` is the obvious thing to write -- this task's own brief
    wrote it -- and it copies `src/` in whole, leaving `app.py` one directory
    below `/usr/lib/<pkg>`. `python -m` prepends only the working directory and
    under `ProtectSystem=strict` nothing else is on `sys.path`, so `app` is not
    importable: the package builds, lints, installs at rc=0 and dies at its
    first request, on the client.

    The interpreter probe cannot see this, which is why the check is here and
    not there: a directory with no `__init__.py` is a *namespace package*, so
    `find_spec("src")` succeeds and reports a directory that holds nothing
    importable. The predicate is therefore on the tree -- a directory carrying
    top-level `.py` files and no `__init__.py` is code staged one level too
    deep. A directory with no `.py` files in it is data (templates, static
    assets), which is a legitimate payload and is left alone.

    Checked against the source rather than the stage so it raises before 97 MB
    of interpreter is vendored for a tree that cannot work.
    """
    for entry in source_paths:
        src = src_root / entry
        if not src.is_dir() or (src / "__init__.py").exists():
            continue
        modules = sorted(p.name for p in src.iterdir() if p.suffix == ".py")
        if modules:
            raise ValueError(
                f"source entry {entry!r} stages {', '.join(modules)} one directory "
                f"below the import root: they land in /usr/lib/<pkg>/{Path(entry).name}/ "
                "and the unit's WorkingDirectory is /usr/lib/<pkg>, which is the only "
                "thing on sys.path. Name the module directly "
                f"(source: [{entry}/{modules[0]}]) or give {Path(entry).name!r} an "
                "__init__.py and import it as a package"
            )


def _refuse_a_native_binary_that_cannot_be_staged(
        src_root: Path, native_binaries: list[str]) -> list[Path]:
    """Everything about a declared native binary that is checkable off disk.

    Three refusals, and the second is the one that costs a client a callout:

    - **Absent, or not a file.** `native_binaries: [build/probe]` before the
      bake step that compiles it is a manifest whose payload does not exist.
    - **Not executable, unless it is a shared library.** dpkg preserves the
      mode it finds in the tree, so a program staged 644 installs at rc=0 and
      cannot be exec'd -- "Permission denied" from a wrapper or a unit, with
      the file visibly present and the right size. `chmod`ing it here would be
      repair, and the source tree is the adopter's: a build that produced a
      non-executable program produced the wrong thing. A `.so` is exempt
      because it is data to the loader and Debian ships shared libraries 644;
      refusing one would refuse the CUDA libraries this feature exists for. The
      predicate is the name, which is the same one `depends._self_shipped`
      already uses to decide what a payload provides for itself -- one spelling
      of "this is a library", not two.
    - **Not an ELF object.** A shell script, a `.tar.gz` or a wrapper named like
      a binary has no `NEEDED` entries at all, so `derive_depends` would return
      an empty list for it and every dependency it really has would go
      undeclared. That is exactly rule 11's failure with the check apparently
      passing. `source:` or `data:` is where a non-ELF file belongs.

    Returns the resolved paths so the soname check below reads them once.
    """
    resolved = []
    for entry in native_binaries:
        path = src_root / entry
        if not path.is_file():
            raise ValueError(
                f"native binary {entry!r} is not a file at {path}. The path is "
                "relative to the manifest's own directory; if a bake step "
                "compiles it, declare that step and its artifact so the build "
                "fails on the missing binary rather than on this line"
            )
        if ".so" not in path.name and not path.stat().st_mode & 0o111:
            raise ValueError(
                f"native binary {entry!r} is not executable ({path.stat().st_mode & 0o777:o}). "
                "dpkg preserves the mode it finds, so it would install at rc=0 "
                "and fail with 'Permission denied' at its first exec on the "
                "client. chmod it in the source tree or in the bake step"
            )
        with path.open("rb") as fh:
            if fh.read(4) != ELF_MAGIC:
                raise ValueError(
                    f"native binary {entry!r} is not an ELF object. porter derives "
                    "Depends: from ELF headers (rule 11), so this one would "
                    "contribute nothing and whatever it really needs would go "
                    "undeclared -- the package installs cleanly and cannot run. "
                    "Stage a script under source: or a blob under data:"
                )
        resolved.append(path)
    return resolved


def _copy_into_the_payload_root(libdir: Path, src: Path, entry: str,
                                what: str) -> Path:
    """Stage one entry under its own basename in /usr/lib/<pkg>/, or refuse.

    Four things write into that one directory -- the vendored interpreter
    (`python/`), the component's requirements when the interpreter is shared,
    `source:` and `native_binaries:` -- and a basename collision between any two
    of them is silent in every direction that matters. `shutil.copy2` onto an
    existing file overwrites it; onto an existing *directory* it writes the file
    inside, so a `source: [python]` would land in the interpreter tree and the
    module would not be importable at all. `copytree` raises, but with
    `FileExistsError` naming a staging path an adopter has never seen.

    So: refuse, name both the entry and the directory, and say what else writes
    there. This is the same argument as `RESERVED_SHARE_NAMES` one directory
    over, generalised -- porter's characteristic bug is the input that is
    accepted and then overwritten by porter itself.
    """
    dest = libdir / Path(entry).name
    if dest.exists():
        raise ValueError(
            f"{what} {entry!r} would be staged as {dest.name} in the payload "
            f"root, and something is already there. That directory holds the "
            "vendored interpreter ('python'), the component's requirements when "
            "the interpreter is shared, and every source: and native_binaries: "
            "entry -- one of them has this basename. Rename it"
        )
    if src.is_dir():
        # symlinks=True: preserved, not dereferenced, so deb.py's lint gets to
        # see an absolute one rather than a silently inlined copy of whatever it
        # pointed at on the build host.
        shutil.copytree(src, dest, symlinks=True)
    else:
        # copy2 and not copy: it preserves the mode, which for a native binary
        # is the difference between a program and a file.
        shutil.copy2(src, dest)
    return dest


def _install_requirements(component: Component, python: Python,
                          python_bin: Path, libdir: Path) -> None:
    """Where a component's wheels go, and it depends on who owns the interpreter.

    **Bundled:** into the interpreter's own `site-packages`, exactly as before.
    The tree is this package's, so nothing else can collide with it.

    **Shared:** into the payload root, with `--target`. The interpreter's
    `site-packages` belongs to *another package* -- one every component of the
    project installs -- so writing there would put `fastapi` in the interpreter
    .deb, make two components that both want it a dpkg file conflict on the
    client, and rebuild 97 MB whenever any component's requirements changed.

    `/usr/lib/<pkg>` is already the unit's `WorkingDirectory` and already what
    the wrapper puts on `PYTHONPATH`, so nothing about sys.path has to be
    invented: `python -m` prepends the working directory and the wheels are in
    it. Precedence is the same as bundled's, too -- the payload's own modules
    are found before anything installed, because they are in the same directory
    and the collision above is refused rather than resolved.

    The `bin/` directory and the `.lock` file `--target` writes are removed.
    Those scripts' shebangs are absolute build-host paths -- rule 3 is that
    porter never runs one, so shipping them is a directory of files that look
    runnable on the client and are not; the lock is uv's own bookkeeping. Both
    are porter's byproduct and not the adopter's input, which is why removing
    them is not the "repair" the refusals above forbid.
    """
    if not component.requirements:
        return
    if python.bundled:
        install(python_bin, component.requirements)
        return
    install(python_bin, component.requirements, target=libdir)
    shutil.rmtree(libdir / "bin", ignore_errors=True)
    (libdir / ".lock").unlink(missing_ok=True)


def _conffiles(stage: Path) -> list[str]:
    """Every file and symlink staged under `etc/`, as absolute client paths.

    Derived from the tree, never listed by hand: deb.py's lint refuses any /etc
    path it was not handed, and the two predicates have to agree. A real
    directory ships as a directory and is not a conffile; everything else is
    one, symlinks included -- dpkg only warns that a symlinked conffile "is not
    a plain file" and builds anyway.

    `os.walk` and not `rglob`, for deb.py's reason: this must look *at* symlinks
    rather than through them, and whether `**` descends into a symlinked
    directory changed in 3.13.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(stage / "etc"):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_dir() and not p.is_symlink():
                continue
            found.append("/" + str(p.relative_to(stage)))
    return sorted(found)


def _wrapper(python_path: str, workdir: str, module: str, args: list[str]) -> str:
    """`/usr/bin/<bin_name>`: exec the vendored interpreter, never a console script.

    `PYTHONPATH` rather than a `cd`: `python -m` puts the *working directory* on
    `sys.path`, so a wrapper that cd'd into /usr/lib/<pkg> would resolve the
    caller's own relative arguments against the payload directory instead --
    `mytool ./report.csv` reading a file the operator never named.
    """
    argv = " ".join([f'"{python_path}"', "-m", module,
                     *(shlex.quote(a) for a in args), '"$@"'])
    return f"""#!/bin/sh
set -e
PYTHONPATH="{workdir}${{PYTHONPATH:+:$PYTHONPATH}}"
export PYTHONPATH
exec {argv}
"""


def _open_an_empty_stage(stage: Path) -> None:
    """The stage porter builds into is porter's alone, and it starts empty.

    Refused rather than emptied, which is deb.py's argument about DEBIAN/ one
    step earlier: a second `porter build` into a directory that still holds the
    previous component's tree would ship both, at rc=0, and the extra files are
    precisely the ones nobody re-reads. Clearing it silently would instead throw
    away a caller's deliberate pre-staging with nothing to show for it.

    Shared by both paths on purpose. A build hook is handed the same empty
    directory the assembler would have got, so a hook that appends to whatever
    it finds cannot inherit a neighbour's payload either.
    """
    if stage.exists() and any(stage.iterdir()):
        raise ValueError(
            f"stage root is not empty: {stage}. porter will not build on top of an "
            "existing tree -- the leftovers would ship. Remove it or pick another"
        )
    stage.mkdir(parents=True, exist_ok=True)


def _refuse_a_hook_beside_keys_it_makes_porter_stop_reading(
        component: Component, python: Python) -> None:
    """The same refusal as `spec._refuse_assembler_keys_beside_a_build_hook`,
    one layer in.

    Written twice deliberately, and the two are not redundant: `spec.py` sees
    the manifest keys as the adopter typed them and can name `python:`, which
    reaches here only as a defaulted `Python` this function cannot tell from an
    omission; this one sees every caller, and most of porter's own suite builds
    a `Component` in Python and never goes near a YAML file. A refusal that
    lives only in the loader is a refusal a test can walk straight past, and
    then the behaviour the test pins is not the behaviour the tool has.
    """
    declared = [name for name, present in (
        ("kind", component.kind != "custom"),
        ("exec", bool(component.module or component.args)),
        ("source", bool(component.source_paths)),
        ("data", bool(component.data_paths)),
        ("native_binaries", bool(component.native_binaries)),
        ("requirements", bool(component.requirements)),
        ("env", bool(component.defaults)),
        ("admin_keys", bool(component.admin_keys)),
        ("bin_name", bool(component.bin_name)),
        ("after", bool(component.after)),
        ("schedule", bool(component.schedule)),
        ("migrations", bool(component.migrations)),
        ("python.package", not python.bundled),
    ) if present]
    if declared:
        raise ValueError(
            f"component {component.name!r} declares build: {component.build!r} and "
            f"also {', '.join(declared)}. The hook writes the whole stage, so "
            "porter reads none of those -- they would be dropped and the package "
            "would build, lint and install at rc=0 without them"
        )


def _hook_environment(component: Component, python: Python, src_root: Path,
                      stage: Path, stamp: str | None) -> dict[str, str]:
    """What the script is told, and it is told it through the environment.

    Not argv: a hook is a script an adopter already had, or one they write in
    ten lines, and `$PORTER_STAGE` reads better in both than `$1` -- which is
    also positional, so inserting a variable later would silently renumber every
    existing hook's arguments.

    The control fields are all here because the hook cannot get at them any
    other way and porter will stamp them onto the .deb regardless: a script that
    writes `/usr/share/$PORTER_PACKAGE/` from `$PORTER_PACKAGE` cannot drift
    from the `Package:` field, and one that hardcodes the name can. `$PORTER_STAMP`
    is `bake`'s provenance block, and porter does *not* write it into the stage
    itself -- `/usr/share/<pkg>/VERSION` is inside the hook's tree, and porter
    writing there after the hook ran is the silent-overwrite failure that
    `RESERVED_SHARE_NAMES` exists to refuse one path over.

    Inherited and extended, not replaced: a hook needs PATH, HOME and whatever
    toolchain the project's build depends on.
    """
    env = dict(os.environ)
    env.update({
        "PORTER_STAGE": str(stage),
        "PORTER_SRC_ROOT": str(Path(src_root).resolve()),
        "PORTER_NAME": component.name,
        "PORTER_PACKAGE": component.package,
        "PORTER_VERSION": component.version,
        "PORTER_ARCHITECTURE": component.architecture,
        "PORTER_MAINTAINER": component.maintainer,
        "PORTER_DESCRIPTION": component.description,
        "PORTER_PYTHON_VERSION": python.version,
        "PORTER_STAMP": stamp or "",
    })
    return env


def _run_the_build_hook(component: Component, python: Python, src_root: Path,
                        stage: Path, stamp: str | None) -> None:
    """Run the script, and let nothing about its failure be quiet.

    `bash -e -o pipefail`, which is `porter.bake`'s contract and for the same
    reason: without `pipefail`, `render | tee build.log` reports `tee`'s success
    and a hook that produced nothing exits 0. Run from `src_root`, so a hook
    written as `./build.sh` in a repo behaves the same whatever directory
    `porter build` was invoked from.

    **stderr is captured and stdout is not.** They are different things here: a
    build hook's stdout is its log, minutes of it, and swallowing that to
    re-print a tail on failure trades the useful half for the tidy half
    (`bake._run_steps` made the same call). Its stderr is the diagnostic that
    has to survive into porter's own error, because the traceback is the last
    place anyone looks before deciding porter is broken. Captured stderr is
    written back out on success, so a hook that warns is still heard.
    """
    script = Path(src_root) / component.build
    # Before the hook runs rather than as bash's rc=127, so the message names
    # the manifest key and the path porter resolved it to. `build: build.sh` is
    # relative to the manifest's directory, which an adopter reading
    # `No such file or directory` from a shell has no way to know.
    if not script.is_file():
        raise ValueError(
            f"component {component.name!r} declares build: {component.build!r} and "
            f"there is no such file at {script}. The path is relative to the "
            "manifest's own directory"
        )
    proc = subprocess.run(
        [_bash(), "-e", "-o", "pipefail", str(script)],
        cwd=Path(src_root), stderr=subprocess.PIPE, text=True,
        env=_hook_environment(component, python, src_root, stage, stamp))
    if proc.returncode != 0:
        raise RuntimeError(
            f"build hook {component.build!r} failed with rc={proc.returncode} "
            f"(run in {src_root}). Its stderr follows.\n{proc.stderr.rstrip()}"
        )
    if proc.stderr:
        sys.stderr.write(proc.stderr)


def _refuse_a_hook_that_produced_nothing(component: Component,
                                         stage: Path) -> None:
    """Magnitude, not existence -- the hatch's own version of the standing rule.

    A hook's entire output is an rc, and an rc is the least reliable thing about
    a build script: `mkdir -p "$PORTER_STAGE/usr/lib/$PORTER_PACKAGE"` and a
    render step that silently wrote nothing exits 0, and porter would package
    the directories into a .deb that installs perfectly and delivers no payload.
    That is the shape this whole repo exists to refuse, arriving through the one
    door where porter did not write the tree.

    So both halves are checked, and the second is the one that matters: a stage
    can hold twenty files and zero bytes. `touch` is what a half-written render
    step leaves behind, and "the file is there" and "the file is real" are
    different questions (AGENTS.md; a 30,912-byte package once passed every path
    assertion in this suite).

    Zero is the floor because zero is the only floor that is not a guess about
    somebody else's payload. A hook that wants a real minimum declares `bake:`
    artifacts with `min_bytes`, which runs before this and says what the numbers
    mean.

    Symlinks are not counted. A tree that is nothing but links to paths outside
    itself has no payload of its own, and deb.py refuses the absolute ones a
    moment later anyway.
    """
    files, total = [], 0
    for dirpath, _dirnames, filenames in os.walk(stage):
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            files.append(p)
            total += p.stat().st_size
    if not files:
        raise RuntimeError(
            f"build hook {component.build!r} exited 0 and left no files in the "
            f"stage ({stage}): porter would package an empty tree into a .deb "
            "that installs cleanly and delivers nothing"
        )
    if total == 0:
        raise RuntimeError(
            f"build hook {component.build!r} exited 0 and left {len(files)} file(s) "
            f"totalling 0 bytes in the stage ({stage}). Every one of them exists "
            "and none of them has anything in it -- which is what a render step "
            "that failed halfway leaves behind, and it passes every check that "
            "asks whether a path is there"
        )


def _assemble_by_hook(component: Component, python: Python, src_root: Path,
                      stage: Path, stamp: str | None) -> Staged:
    """The escape hatch: the script stages the tree, porter keeps the rest.

    What porter still does after the hook returns is the whole argument for the
    hatch being safe, and none of it is optional:

      - the stage must actually hold a payload (above);
      - `conffiles` are derived from the tree, so deb.py's lint and this list
        are two readings of the same directory and cannot disagree;
      - `Depends:` is derived from the ELF headers of whatever was staged --
        rule 11 holds for a hook exactly as it holds for the assembler, and a
        hook is *likelier* to stage a native binary porter never compiled;
      - and `build_deb` then applies the FHS lint and the `sh -n` pass to the
        result, unchanged. A hook cannot ship `/home/...`, `/etc/<pkg>/env`, an
        absolute symlink, a pre-staged `DEBIAN/` or a `/usr/bin` script that
        does not parse, because none of those refusals ever looked at who wrote
        the tree.

    `scripts` is empty and that is a real boundary rather than an oversight: a
    hook has no way to contribute a postinst, because `DEBIAN/` is porter's and
    deb.py refuses a stage carrying one. A hook component that needs maintainer
    scripts is the next example the gallery owes, not a key to invent here.
    """
    _refuse_a_hook_beside_keys_it_makes_porter_stop_reading(component, python)
    _open_an_empty_stage(stage)
    _run_the_build_hook(component, python, src_root, stage, stamp)
    _refuse_a_hook_that_produced_nothing(component, stage)

    control = {
        "Package": component.package,
        "Version": component.version,
        "Architecture": component.architecture,
        "Maintainer": component.maintainer,
        "Description": component.description,
    }
    depends = derive_depends(stage)
    if depends:
        control["Depends"] = ", ".join(depends)
    # Derived from the tree the hook wrote, exactly as it is for a tree porter
    # wrote: deb.py's lint refuses any /etc path it was not handed, so this list
    # and that lint are two readings of one directory and cannot disagree. A
    # hook has no key to declare conffiles with and needs none.
    conffiles = _conffiles(stage)
    return Staged(stage=stage, conffiles=conffiles, control=control, scripts={})


def _cpython_version(python_bin: Path, declared: str) -> str:
    """The full version the STAGED tree reports, asked of the tree itself.

    Not parsed out of the directory name and not taken from the manifest: the
    manifest says `3.12`, and what the interpreter package must be versioned by
    is the patch release whose ABI the components' wheels were compiled
    against. The one authority on that is the binary.

    The prefix check is a positive control on `vendor()`: uv is asked for
    `3.12` and answers with a tree, and nothing between the request and the
    staged directory has so far confirmed the two agree. A `3.13` tree under a
    `python3.12` ExecStart is a package that installs and cannot start.
    """
    proc = subprocess.run(
        [str(python_bin), "-c",
         "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"the staged interpreter at {python_bin} does not run on this build "
            f"host: rc={proc.returncode} {proc.stderr.strip()}. porter cannot "
            "version a package around a tree it cannot ask"
        )
    full = proc.stdout.strip()
    if not full.startswith(f"{declared}."):
        raise RuntimeError(
            f"the vendored tree reports Python {full} and the manifest declares "
            f"{declared!r}. ExecStart would name python{declared}, which this "
            "tree does not contain"
        )
    return full


def assemble_interpreter(python: Python, stage_root: Path, *,
                         architecture: str = "amd64",
                         maintainer: str = "porter <porter@example.com>"
                         ) -> Interpreter:
    """Build the shared interpreter's own package. One tree, `Depends:`-ed on.

    Everything a component's bundled interpreter would be, in a .deb of its
    own and at the same path shape: `/usr/lib/<interp-pkg>/python/`. Nothing
    else is in it -- no unit, no config, no conffile, no maintainer script and
    no payload -- so `scripts` is empty and there is nothing for dpkg to run.

    `Depends:` is derived here exactly as it is for a component, and it is not
    decoration: the tree's single dynamically-linked extension pulls in
    `libcrypt1`, which is named nowhere in any manifest and which the components
    no longer carry once the interpreter is theirs no more. If it were dropped,
    every component would install and every one would fail to import `crypt`.

    The caller keeps the returned `stage` alive while its components build --
    `python_bin` points into it, and that binary is what selects the components'
    wheels and answers their import probes.
    """
    stage = Path(stage_root).resolve()
    _open_an_empty_stage(stage)
    libdir = stage / "usr/lib" / python.package
    libdir.mkdir(parents=True)
    python_bin = vendor(libdir, python.version)
    full = _cpython_version(python_bin, python.version)

    control = {
        "Package": python.package,
        "Version": full,
        "Architecture": architecture,
        "Maintainer": maintainer,
        "Description": (
            f"CPython {full}, vendored by porter and shared by this project's "
            f"components"),
    }
    depends = derive_depends(stage)
    if depends:
        control["Depends"] = ", ".join(depends)
    return Interpreter(package=python.package, version=full,
                       python_version=python.version, stage=stage,
                       python_bin=python_bin, control=control)


def assemble(component: Component, python: Python,
             src_root: Path, stage_root: Path, stamp: str | None = None,
             siblings: Sequence[Component] = (),
             interpreter: Interpreter | None = None) -> Staged:
    """Stage `component` under `stage_root`. Returns what `build_deb` needs.

    `stamp` is `porter.bake`'s provenance block, written to
    `usr/share/<pkg>/VERSION` -- package-owned, replaced on upgrade, and inside
    the top-level allowlist. bake cannot write it itself: the stage does not
    exist when bake runs, and `assemble` refuses to build into one that is
    already populated.

    `siblings` is **every component the manifest declares, this one included**,
    and it is what turns `after: [alpha]` into `After=demo-alpha.service`. It
    is passed rather than resolved by the caller so that a caller which forgets
    it cannot silently ship a package with its ordering dropped: with no
    siblings, a component that declares `after:` names something the (one-entry)
    manifest does not provide and `resolve_ordering` refuses it. A component
    that declares no ordering is unaffected, which is the single-component
    project and the common case.
    """
    # Resolved, and not merely wrapped in Path: `porter build`'s own --stage
    # default is the relative "build", and the import probe execs the staged
    # interpreter with `cwd=libdir`. The child chdir's before exec, so a
    # relative program path is resolved against the *new* directory and cannot
    # be found -- `porter build` failed on porter's own example for exactly
    # this, while all 18 tests here passed absolute tmp_paths and saw nothing.
    src_root, stage = Path(src_root), Path(stage_root).resolve()
    pkg = component.package

    # The escape hatch, taken before anything below is consulted. Everything
    # from here to the end of this function is what `build:` declines; the
    # pipeline either side of it is what it keeps. See `_assemble_by_hook`.
    if component.build:
        return _assemble_by_hook(component, python, src_root, stage, stamp)

    _refuse_what_porter_cannot_emit(component, python, interpreter)
    _refuse_a_source_directory_below_the_import_root(src_root, component.source_paths)
    # Before 97 MB is vendored, all three of them: a cycle, a misspelled
    # `after:`, a native binary that is missing, unreadable or links a library
    # nothing on the target provides are all properties of the source tree and
    # the manifest, and none of them needs anything staged to detect.
    depends_on = resolve_ordering(list(siblings) or [component])[component.name]
    natives = _refuse_a_native_binary_that_cannot_be_staged(
        src_root, component.native_binaries)
    refuse_a_native_object_the_target_cannot_run(natives)

    _open_an_empty_stage(stage)

    libdir = stage / "usr/lib" / pkg
    libdir.mkdir(parents=True)
    # The bundled interpreter goes in the payload root; a shared one is already
    # a package of its own and only its staged binary is borrowed, to select
    # wheels and answer the import probes.
    python_bin = vendor(libdir, python.version) if python.bundled \
        else interpreter.python_bin
    _install_requirements(component, python, python_bin, libdir)

    for entry in component.source_paths:
        _copy_into_the_payload_root(libdir, src_root / entry, entry, "source entry")

    # Beside the source and not under /usr/share: these are programs the unit or
    # the wrapper execs, and /usr/lib/<pkg> is the directory the package already
    # owns and already resolves paths against.
    for entry, src in zip(component.native_binaries, natives):
        _copy_into_the_payload_root(libdir, src, entry, "native binary")

    # Baked data goes to /usr/share/<pkg>, which is the FHS contract's home for
    # it, and NOT to /usr/lib/<pkg> with the source. That directory is the unit's
    # WorkingDirectory and therefore the import root: a corpus staged there is on
    # sys.path, where a `data/model.py` shadows the stdlib for the whole service.
    sharedir = stage / "usr/share" / pkg
    for entry in component.data_paths:
        sharedir.mkdir(parents=True, exist_ok=True)
        src, dest = src_root / entry, sharedir / Path(entry).name
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest)

    # Which build produced this package, readable on the client with `cat`. The
    # package version does not answer that question: two builds of 1.0 from
    # different corpora are indistinguishable, and the client is where the only
    # copy of the evidence is.
    if stamp is not None:
        sharedir.mkdir(parents=True, exist_ok=True)
        (sharedir / "VERSION").write_text(stamp)

    # The paths as they will be ON THE CLIENT, never the staging paths: an
    # ExecStart naming a build directory is the exact class of bug rule 1 is
    # about, and it works perfectly on the machine that produced it.
    installed_python = f"/usr/lib/{pkg}/python/bin/python{python.version}" \
        if python.bundled else interpreter.installed_python
    workdir = f"/usr/lib/{pkg}"
    scripts: dict[str, str] = {}

    if component.kind in UNIT_KINDS:
        defaults, env_example = split(component.defaults, component.admin_keys)
        etc = stage / "etc" / pkg
        etc.mkdir(parents=True)
        # Written even when the component declares no keys at all: the unit's
        # `EnvironmentFile=/etc/<pkg>/defaults` carries no `-`, so an absent
        # file is a start failure whose only trace is the unit's status.
        (etc / "defaults").write_text(defaults)
        # exist_ok: the data staging and the VERSION stamp above may already
        # have created it, and both are legitimate for a service.
        sharedir.mkdir(parents=True, exist_ok=True)
        # Under /usr/share and never /etc. env in the stage is refused by
        # deb.py's lint, and rightly: shipping it lets dpkg replace the admin's
        # secrets on the next upgrade.
        (sharedir / "env.example").write_text(env_example)

        exec_start = " ".join([installed_python, "-m", component.module, *component.args])
        units = stage / "usr/lib/systemd/system"
        units.mkdir(parents=True)
        (units / f"{pkg}.service").write_text(
            unit(pkg, component.description, exec_start, workdir,
                 depends_on=depends_on, kind=component.kind))
        if component.kind == "oneshot":
            # The schedule is the whole point of the kind, so a oneshot with no
            # `schedule:` is refused by `timer()` rather than defaulted: there
            # is no interval porter could pick that is not a guess about the
            # author's job, and a job that never fires reports nothing.
            (units / f"{pkg}.timer").write_text(
                timer(pkg, component.description, component.schedule))
        # The interactive half of rule 5, and only when there is something to
        # ask: postinst leaves /etc/<pkg>/env holding the right keys and no
        # values, and this is where a human fills them in. `setup_script`
        # refuses a component with no admin_keys, so the condition here and the
        # refusal there are the same predicate written twice on purpose -- the
        # one that would otherwise go wrong is silent, a package that ships a
        # wizard prompting for nothing.
        if component.admin_keys:
            setup = stage / "usr/bin" / f"{pkg}-setup"
            setup.parent.mkdir(parents=True, exist_ok=True)
            setup.write_text(setup_script(component))
            setup.chmod(0o755)  # dpkg preserves the mode; without it, not runnable
        # Creates the static system user the unit's User= names, and the admin's
        # env file. Both are why a service always ships one. `migrations` reaches
        # the client through here and nowhere else: a manifest that declares one
        # and an emitter that drops it is an upgrade that silently does not
        # migrate, which is the whole failure this task exists to close.
        # `kind=` decides which unit the postinst starts on a fresh install, and
        # for a oneshot that is the .timer -- starting its .service would run the
        # job at install time, which is not what a schedule means.
        scripts["postinst"] = env_postinst(
            pkg, migrations=component.migrations,
            has_setup=bool(component.admin_keys), kind=component.kind)
    else:  # command -- no unit, no user, no config; see the refusal above
        bindir = stage / "usr/bin"
        bindir.mkdir(parents=True)
        wrapper = bindir / component.bin_name
        wrapper.write_text(_wrapper(installed_python, workdir,
                                    component.module, component.args))
        wrapper.chmod(0o755)  # dpkg preserves the mode; without it, not runnable

    _refuse_a_module_the_interpreter_cannot_import(python_bin, libdir, component.module)
    _refuse_an_import_the_payload_makes_that_the_interpreter_cannot_find(
        python_bin, libdir, component.source_paths)

    control = {
        "Package": pkg,
        "Version": component.version,
        "Architecture": component.architecture,
        "Maintainer": component.maintainer,
        "Description": component.description,
    }
    # Rule 11: derived from the ELF headers of what is actually staged, never
    # hand-written. The vendored interpreter is a bundled native tree, and its
    # `_crypt` extension links libcrypt.so.1 -- a dependency nothing in the
    # manifest names and nobody writing the list by hand would include. Set only
    # when there is something to declare, so a payload with no native code
    # ships no empty field. `derive_depends` refuses rather than shortens: an
    # object it cannot read is not an object with no dependencies.
    depends = derive_depends(stage)
    # First, and by exact version. A shared interpreter is the one dependency
    # nothing in the stage can reveal -- it is in another package, so there is
    # no ELF header here that names it and `derive_depends` is structurally
    # blind to it. Without this line the component installs perfectly on a
    # client that has never seen the interpreter package, and ExecStart names a
    # path that does not exist.
    if interpreter is not None:
        depends.insert(0, interpreter.exact_dependency)
    if depends:
        control["Depends"] = ", ".join(depends)
    return Staged(stage=stage, conffiles=_conffiles(stage),
                  control=control, scripts=scripts)
