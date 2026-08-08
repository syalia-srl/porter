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
"""
from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from porter.config import env_postinst, setup_script, split
from porter.depends import derive_depends
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


def _refuse_what_porter_cannot_emit(component: Component, python: Python) -> None:
    """Refuse the shapes porter could only package *wrongly*.

    Each of these has a plausible-looking wrong answer, which is why it is a
    refusal and not a default:

    - **A misspelled kind.** `kind: sevice` would otherwise stage a payload with
      no unit and no wrapper: a .deb that builds, lints, installs cleanly and
      does nothing at all.
    - **An interpreter porter does not bundle.** `python.package: <name>` means
      the interpreter arrives in a package of its own; nothing yet knows where
      that package puts it, so ExecStart, the wrapper and the import probe would
      all be guesses at a path.
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
    if not python.bundled:
        raise ValueError(
            f"python.package={python.package!r} is not implemented: only 'bundled' "
            "is. An interpreter shipped in a package of its own has no known "
            "install path here, so ExecStart would be a guess"
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


def assemble(component: Component, python: Python,
             src_root: Path, stage_root: Path, stamp: str | None = None,
             siblings: Sequence[Component] = ()) -> Staged:
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

    _refuse_what_porter_cannot_emit(component, python)
    _refuse_a_source_directory_below_the_import_root(src_root, component.source_paths)
    # Before 97 MB is vendored: a cycle or a misspelled `after:` is a property
    # of the manifest and needs nothing staged to detect.
    depends_on = resolve_ordering(list(siblings) or [component])[component.name]

    # Refused rather than emptied, which is deb.py's argument about DEBIAN/ one
    # step earlier: a second `porter build` into a directory that still holds
    # the previous component's tree would ship both, at rc=0, and the extra
    # files are precisely the ones nobody re-reads. Clearing it silently would
    # instead throw away a caller's deliberate pre-staging (a `build:` hook's
    # output, docs/design-spec.md's escape hatch) with nothing to show for it.
    if stage.exists() and any(stage.iterdir()):
        raise ValueError(
            f"stage root is not empty: {stage}. porter will not build on top of an "
            "existing tree -- the leftovers would ship. Remove it or pick another"
        )
    stage.mkdir(parents=True, exist_ok=True)

    libdir = stage / "usr/lib" / pkg
    libdir.mkdir(parents=True)
    python_bin = vendor(libdir, python.version)
    if component.requirements:
        install(python_bin, component.requirements)

    for entry in component.source_paths:
        src = src_root / entry
        dest = libdir / Path(entry).name
        if src.is_dir():
            # symlinks=True: preserved, not dereferenced, so deb.py's lint gets
            # to see an absolute one rather than a silently inlined copy of
            # whatever it pointed at on the build host.
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest)

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
    installed_python = f"/usr/lib/{pkg}/python/bin/python{python.version}"
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
    if depends:
        control["Depends"] = ", ".join(depends)
    return Staged(stage=stage, conffiles=_conffiles(stage),
                  control=control, scripts=scripts)
