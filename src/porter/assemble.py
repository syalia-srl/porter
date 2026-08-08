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

Refusals here follow deb.py's precedent: **refuse, never repair.** A stage
porter quietly fixes is a stage whose next surprise ships.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from porter.config import env_postinst, split
from porter.interpreter import install, vendor
from porter.systemd import unit
from porter.types import Component, Python

# The shapes porter can emit a *correct* package for today. `oneshot` is absent
# on purpose; see _refuse_what_porter_cannot_emit.
SUPPORTED_KINDS = ("service", "command")


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

    - **`oneshot`.** `systemd.unit()` takes no `Type=` and always emits
      `Restart=on-failure` plus `WantedBy=multi-user.target`, and
      `config.env_postinst` enables `<pkg>.service` unconditionally. Pushed
      through those, a "scheduled job" installs at rc=0 and runs as a
      permanently-restarting service with no timer anywhere. The gallery entry
      (`examples/oneshot-timer`, listed in docs/design-spec.md and not yet
      written) is what unblocks it, together with a `Type=`/`timer()` pair in
      systemd.py and a postinst that knows which unit to enable.
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
    """
    if component.kind == "oneshot":
        raise ValueError(
            "kind 'oneshot' is not implemented: systemd.unit() emits no Type= and "
            "no .timer, and config.env_postinst enables <pkg>.service "
            "unconditionally -- a oneshot pushed through them installs at rc=0 as "
            "a restarting service. Add examples/oneshot-timer first"
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
    if component.kind == "command":
        if not component.bin_name:
            raise ValueError("a 'command' component needs bin_name: there is no name "
                             "to install under /usr/bin")
        if component.defaults or component.admin_keys:
            raise ValueError(
                "a 'command' component may not declare config: postinst would create "
                "/etc/<pkg>/env at 600 root:root, unreadable by the operator running "
                "the command, and the wrapper sources nothing -- the shipped defaults "
                "would be inert. Add examples/command first"
            )


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

    Run against the **staged** interpreter from the **payload directory** --
    the two things that decide the answer on the client -- with `PYTHONPATH`
    and `PYTHONHOME` stripped, so the build host's environment cannot answer on
    the client's behalf. `find_spec` locates without importing, so a module with
    import-time side effects is not executed here.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    probe = subprocess.run(
        [str(python_bin), "-c",
         ("import importlib.util, sys; "
          "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)"),
         module],
        cwd=workdir, env=env, capture_output=True, text=True)
    if probe.returncode != 0:
        raise RuntimeError(
            f"the staged interpreter cannot import {module!r} from the payload "
            f"directory ({workdir}), so ExecStart would fail on the client. "
            f"Is it missing from requirements? rc={probe.returncode} "
            f"{probe.stderr.strip()}"
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
             src_root: Path, stage_root: Path) -> Staged:
    """Stage `component` under `stage_root`. Returns what `build_deb` needs."""
    src_root, stage = Path(src_root), Path(stage_root)
    pkg = component.package

    _refuse_what_porter_cannot_emit(component, python)

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

    # The paths as they will be ON THE CLIENT, never the staging paths: an
    # ExecStart naming a build directory is the exact class of bug rule 1 is
    # about, and it works perfectly on the machine that produced it.
    installed_python = f"/usr/lib/{pkg}/python/bin/python{python.version}"
    workdir = f"/usr/lib/{pkg}"
    scripts: dict[str, str] = {}

    if component.kind == "service":
        defaults, env_example = split(component.defaults, component.admin_keys)
        etc = stage / "etc" / pkg
        etc.mkdir(parents=True)
        # Written even when the component declares no keys at all: the unit's
        # `EnvironmentFile=/etc/<pkg>/defaults` carries no `-`, so an absent
        # file is a start failure whose only trace is the unit's status.
        (etc / "defaults").write_text(defaults)
        share = stage / "usr/share" / pkg
        share.mkdir(parents=True)
        # Under /usr/share and never /etc. env in the stage is refused by
        # deb.py's lint, and rightly: shipping it lets dpkg replace the admin's
        # secrets on the next upgrade.
        (share / "env.example").write_text(env_example)

        exec_start = " ".join([installed_python, "-m", component.module, *component.args])
        units = stage / "usr/lib/systemd/system"
        units.mkdir(parents=True)
        (units / f"{pkg}.service").write_text(
            unit(pkg, component.description, exec_start, workdir))
        # Creates the static system user the unit's User= names, and the admin's
        # env file. Both are why a service always ships one.
        scripts["postinst"] = env_postinst(pkg)
    else:  # command -- no unit, no user, no config; see the refusal above
        bindir = stage / "usr/bin"
        bindir.mkdir(parents=True)
        wrapper = bindir / component.bin_name
        wrapper.write_text(_wrapper(installed_python, workdir,
                                    component.module, component.args))
        wrapper.chmod(0o755)  # dpkg preserves the mode; without it, not runnable

    _refuse_a_module_the_interpreter_cannot_import(python_bin, libdir, component.module)

    control = {
        "Package": pkg,
        "Version": component.version,
        "Architecture": component.architecture,
        "Maintainer": component.maintainer,
        "Description": component.description,
    }
    return Staged(stage=stage, conffiles=_conffiles(stage),
                  control=control, scripts=scripts)
