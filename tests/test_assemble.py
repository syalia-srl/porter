"""assemble: a Component becomes a tree build_deb can package unmodified.

The integration nothing else covered. Tasks 1-3 each proved their own primitive
and every staged tree in this suite was hand-built in a fixture, so "the pieces
fit together" was asserted nowhere -- and `porter build` had no body at all.

Two habits carried from the rest of the suite. Assertions land on the artefact:
the staged interpreter is *run*, the wrapper is *executed*, the .deb is
*built*, and the payload is checked for magnitude rather than presence -- a
stubbed interpreter passed every path assertion in this repo once, at 30 KB.
And the cheap refusals come first, because they raise before anything vendors a
97 MB tree.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import EXAMPLE, _require_uv

from porter.assemble import assemble
from porter.deb import build_deb
from porter.types import Component, Python

PY = Python(version="3.12", package="bundled")


def _cmd(**kw) -> Component:
    """A command-kind component: no unit, no config, one wrapper in /usr/bin."""
    base = dict(name="hello", package="porter-hello", description="d",
                kind="command", module="hello", args=[],
                source_paths=["src/hello.py"], bin_name="porter-hello")
    base.update(kw)
    return Component(**base)


# --- refusals: shapes porter could only package wrongly ----------------------
#
# Each of these would otherwise produce an artefact at rc=0 -- which is the
# failure class this repo exists to refuse, not an exception somewhere.

# `oneshot` was refused here by name until Task 11, because unit() emitted no
# Type=, nothing emitted a .timer, and the postinst enabled <pkg>.service
# unconditionally. All three are emitted correctly now, so the refusal and its
# test are gone; tests/test_oneshot.py pins the behaviour that replaced them,
# including the [Install] Also= line that keeps the postinst correct for a job.


def test_refuses_a_kind_it_has_no_branch_for(tmp_path, src_tree):
    """`kind: sevice` would stage a payload with neither unit nor wrapper: a
    package that installs at rc=0 and does nothing at all."""
    with pytest.raises(ValueError, match="unknown component kind"):
        assemble(_cmd(kind="sevice"), PY, src_tree, tmp_path / "s")


def test_refuses_an_interpreter_it_does_not_bundle(tmp_path, src_tree):
    """An interpreter shipped in its own package has no known install path here,
    so ExecStart and the wrapper would both name a guess."""
    with pytest.raises(ValueError, match="not implemented"):
        assemble(_cmd(), Python(version="3.12", package="porter-python312"),
                 src_tree, tmp_path / "s")


def test_refuses_config_on_a_command(tmp_path, src_tree):
    """postinst writes /etc/<pkg>/env at 600 root:root -- unreadable by the
    operator running the command -- and the wrapper sources nothing, so the
    shipped defaults would be inert. A silently useless config file."""
    with pytest.raises(ValueError, match="may not declare config"):
        assemble(_cmd(defaults={"PORT": "1"}), PY, src_tree, tmp_path / "s")


def test_refuses_a_command_with_no_bin_name(tmp_path, src_tree):
    with pytest.raises(ValueError, match="bin_name"):
        assemble(_cmd(bin_name=None), PY, src_tree, tmp_path / "s")


def test_refuses_a_source_directory_that_stages_its_modules_below_the_import_root(
        tmp_path, src_tree):
    """`source: ["src"]` -- which this task's brief wrote -- copies the
    directory under its own name and leaves app.py at /usr/lib/<pkg>/src/app.py,
    one level below the WorkingDirectory that is the only thing on sys.path.
    The package builds, lints, installs at rc=0 and dies at its first request.

    The import probe cannot see it: a directory with no __init__.py is a
    namespace package, so find_spec("src") *succeeds* and reports a directory
    holding nothing importable (measured 2026-08-08). Refused before anything
    vendors, which is why this test costs nothing.
    """
    with pytest.raises(ValueError, match="below the import root"):
        assemble(_cmd(source_paths=["src"]), PY, src_tree, tmp_path / "s")


def test_a_source_directory_carrying_no_modules_is_data_and_is_staged(tmp_path, src_tree):
    """The positive control for the refusal above, and it is the whole reason
    that refusal reads the directory's contents rather than its shape: a guard
    that refused every directory would satisfy the test above just as well, and
    templates/ and static/ are legitimate payload with nowhere else to go."""
    _require_uv()
    root = tmp_path / "with-data"
    (root / "src/assets").mkdir(parents=True)
    shutil.copy(src_tree / "src/hello.py", root / "src/hello.py")
    (root / "src/assets/banner.txt").write_text("not a module\n")

    component = _cmd(source_paths=["src/hello.py", "src/assets"])
    st = assemble(component, PY, root, tmp_path / "s")
    assert (st.stage / f"usr/lib/{component.package}/assets/banner.txt").exists()


def test_refuses_a_stage_root_that_is_not_empty(tmp_path, src_tree):
    """A second build into a directory still holding the previous component's
    tree would ship both, at rc=0. deb.py refuses a pre-staged DEBIAN/ for the
    same reason: leftovers are exactly what nobody re-reads."""
    stale = tmp_path / "s"
    (stale / "usr/lib/some-other-package").mkdir(parents=True)
    (stale / "usr/lib/some-other-package/leftover.py").write_text("# not ours\n")
    with pytest.raises(ValueError, match="not empty"):
        assemble(_cmd(), PY, src_tree, stale)


# --- the staged service ------------------------------------------------------
#
# Session-scoped: vendoring plus `uv pip install fastapi uvicorn` is a ~97 MB
# tree, so one assemble serves every assertion about it.

@pytest.fixture(scope="session")
def assembled_service(demo_manifest, tmp_path_factory):
    """examples/service-fastapi, staged by assemble rather than by a fixture.

    Nothing here restates the manifest -- package name, version, interpreter,
    requirements, exec line, env template and source paths are all read out of
    examples/service-fastapi/porter.yaml, so an example that stops assembling
    takes the suite red. That is the same bargain conftest's `demo_stage`
    makes, and this is the code path meant to replace it.
    """
    _require_uv()
    component, python = Component.from_manifest(demo_manifest)
    return component, assemble(component, python,
                               Path(__file__).resolve().parents[1]
                               / "examples/service-fastapi",
                               tmp_path_factory.mktemp("svc") / "stage")


def test_declares_every_etc_file_as_a_conffile(assembled_service):
    """deb.py's lint refuses any file under etc/ it was not handed. If assemble
    does not return them, every build fails at the lint -- so the list is
    derived from the tree here and returned, never re-derived at the call site."""
    component, st = assembled_service
    assert st.conffiles == [f"/etc/{component.package}/defaults"]


def test_never_stages_the_admin_env_file(assembled_service):
    """env is admin-owned. Shipping it is refused by the lint, and would
    overwrite a client's secrets on upgrade if it were not."""
    component, st = assembled_service
    pkg = component.package
    assert not (st.stage / f"etc/{pkg}/env").exists()
    assert not (st.stage / f"etc/{pkg}/env").is_symlink()
    assert (st.stage / f"usr/share/{pkg}/env.example").exists()
    # The half that must ship, and must be a conffile, or dpkg overwrites an
    # edited copy on every upgrade with no .dpkg-dist and no record.
    assert (st.stage / f"etc/{pkg}/defaults").exists()


def test_the_service_carries_the_postinst_that_creates_its_user_and_env(assembled_service):
    component, st = assembled_service
    assert "postinst" in st.scripts
    assert f"/usr/share/{component.package}/env.example" in st.scripts["postinst"]


def test_entrypoint_is_invoked_via_python_m_not_a_console_script(assembled_service):
    """Console-script shebangs are absolute build paths and break on the client.
    The unit must go through the vendored interpreter."""
    component, st = assembled_service
    body = (st.stage / f"usr/lib/systemd/system/{component.package}.service").read_text()
    assert "/python/bin/python3.12 -m " in body
    assert "/bin/uvicorn" not in body


def test_the_unit_runs_from_the_directory_the_payload_was_staged_into(assembled_service):
    """WorkingDirectory is what puts the app on sys.path -- `python -m` prepends
    the working directory and, under ProtectSystem=strict, nothing else will.
    So the source entry must land directly in it, not one level below."""
    component, st = assembled_service
    pkg = component.package
    body = (st.stage / f"usr/lib/systemd/system/{pkg}.service").read_text()
    assert f"WorkingDirectory=/usr/lib/{pkg}\n" in body
    assert (st.stage / f"usr/lib/{pkg}/app.py").exists()


def test_the_staged_interpreter_is_a_real_one(assembled_service):
    """Magnitude, not presence. A 30 KB stub once passed every path assertion in
    this repo; `python/bin/python3.12 exists` is true of it too."""
    component, st = assembled_service
    tree = st.stage / f"usr/lib/{component.package}/python"
    size = sum(p.stat().st_size for p in tree.rglob("*") if p.is_file() and not p.is_symlink())
    assert size > 20_000_000, f"vendored interpreter is only {size} bytes"


def test_the_staged_tree_passes_build_deb_unmodified(assembled_service, tmp_path):
    """The integration this task exists for. assemble's output must be directly
    packageable -- a tree that needs hand-fixing first is not an assemble -- and
    build_deb's lint is a total one, so this is also the check that the returned
    conffiles agree with the tree they describe."""
    _, st = assembled_service
    deb = build_deb(st.stage, st.control, tmp_path / "out",
                    conffiles=st.conffiles, scripts=st.scripts)
    assert deb.exists()
    # -Znone and a vendored interpreter: anything small is a truncated build,
    # which is one of the five false passes recorded in docs/design-spec.md.
    assert deb.stat().st_size > 20_000_000, f"{deb} is only {deb.stat().st_size} bytes"


# --- the staged command ------------------------------------------------------

@pytest.fixture(scope="session")
def assembled_command(src_tree, tmp_path_factory):
    _require_uv()
    component = _cmd()
    return component, assemble(component, PY, src_tree,
                               tmp_path_factory.mktemp("cmd") / "stage")


def test_service_gets_a_unit_command_does_not(assembled_service, assembled_command):
    svc_component, svc = assembled_service
    assert (svc.stage / f"usr/lib/systemd/system/{svc_component.package}.service").exists()
    cmd_component, cmd = assembled_command
    assert not (cmd.stage / "usr/lib/systemd/system").exists()
    assert (cmd.stage / f"usr/bin/{cmd_component.bin_name}").exists()
    # No unit means no user to create and no config to place: a postinst here
    # would call `systemctl enable` on a unit that does not exist, and on a
    # booted client that is now fatal (config.py's guard, deliberately).
    assert cmd.scripts == {}
    assert cmd.conffiles == []
    assert not (cmd.stage / "etc").exists()


def test_the_command_wrapper_runs_the_staged_interpreter(assembled_command):
    """Executed, not read. The wrapper's whole job is to reach a relocated
    interpreter and a payload that is not on any default sys.path, and both are
    silent to a substring assertion.

    Run from `/` with PYTHONPATH stripped from the environment: that is the
    positive control for the wrapper's own PYTHONPATH doing the work, rather
    than the caller's directory happening to hold the module.
    """
    component, cmd = assembled_command
    pkg = component.package
    script = (cmd.stage / f"usr/bin/{component.bin_name}").read_text().replace(
        f"/usr/lib/{pkg}", str(cmd.stage / "usr/lib" / pkg))
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    proc = subprocess.run(["sh", "-c", script], cwd="/", env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "hello from the vendored interpreter" in proc.stdout


def test_the_command_wrapper_is_executable(assembled_command):
    """dpkg preserves the mode it is staged with, and will not run what it
    cannot exec."""
    component, cmd = assembled_command
    mode = (cmd.stage / f"usr/bin/{component.bin_name}").stat().st_mode
    assert mode & 0o111, oct(mode)


def test_the_command_tree_passes_build_deb_unmodified(assembled_command, tmp_path):
    _, cmd = assembled_command
    deb = build_deb(cmd.stage, cmd.control, tmp_path / "out",
                    conffiles=cmd.conffiles, scripts=cmd.scripts)
    assert deb.exists() and deb.stat().st_size > 20_000_000


# --- the import probe --------------------------------------------------------

def test_refuses_a_module_the_staged_interpreter_cannot_import(tmp_path, src_tree):
    """The airgap failure that looks exactly like success on the build host.

    `requirements` omitted or misspelled leaves a .deb that builds, lints and
    installs, then dies at ExecStart with ModuleNotFoundError on a client with
    no network to fix it from. Nothing else in the pipeline reads `module`:
    deb.py sees a directory of bytes and systemd.py sees a string.

    `assembled_command` is this test's positive control -- the same probe, the
    same interpreter, a module that *is* staged, and it passes.
    """
    _require_uv()
    with pytest.raises(RuntimeError, match="cannot import"):
        assemble(_cmd(module="uvicorn", requirements=[]), PY, src_tree, tmp_path / "s")


def test_refuses_an_import_the_payload_makes_that_the_interpreter_cannot_find(
        demo_manifest, tmp_path):
    """The same airgap failure one argument further out, and the likelier one.

    `module` is the runner -- `uvicorn` for the gallery -- and `app:app` is
    uvicorn's argument, which nothing in porter parses. So the runner probe is
    perfectly satisfied by a manifest that forgot the payload's own dependency:
    uvicorn imports, the .deb builds, lints and installs at rc=0, and the first
    HTTP request dies on a client with no network to fix it from. A runner is
    named in `exec:` where it is hard to forget; a payload's imports are named
    nowhere in the manifest at all.

    The gallery's app.py imports `fastapi`, so dropping it from `requirements`
    is that mistake exactly.
    """
    _require_uv()
    trimmed = [r for r in demo_manifest["requirements"] if r != "fastapi"]
    # If the example ever stops requiring fastapi this test would silently
    # assert nothing: it would be probing an unmodified manifest.
    assert trimmed != demo_manifest["requirements"]

    component, python = Component.from_manifest(
        {**demo_manifest, "requirements": trimmed})
    with pytest.raises(RuntimeError, match="app.py imports 'fastapi'"):
        assemble(component, python, EXAMPLE, tmp_path / "s")
