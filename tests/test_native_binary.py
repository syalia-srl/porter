"""`native_binaries:` -- the payload porter did not compile, and its `Depends:`.

Rule 11 at the door: **`Depends:` is derived from the ELF headers, never
hand-written.** A hand-kept list is how a package installs cleanly on the client
and then cannot start, and it goes stale silently on the next upstream build.
The module that derives it (`porter.depends`) has existed since Task 8 and was
exercised only against the vendored interpreter -- which is to say against a
tree porter itself produced. This file is the other half: a compiled program the
*project* produced, staged beside its own private library, which is ainbox's
engine (`llama-server` plus hand-picked CUDA libraries) in miniature.

Two properties, and they pull in opposite directions:

  - a soname the CLIENT provides must be named (`libc.so.6` -> `libc6`);
  - a soname the PAYLOAD provides must NOT be, because apt on an airgapped
    client cannot fetch one no mirror has ever carried.

and one refusal that is worth more than both: a binary linking something
*neither* provides fails the build, naming the sonames. Better a red build than
a package that installs and cannot start.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import _require_uv

from porter.assemble import assemble
from porter.depends import derive_depends
from porter.types import Component, Python

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
PY = Python(version="3.12", package="bundled")

HELPER_C = """#include <stddef.h>
size_t porter_probe_answer(void) { return 42; }
"""
PROBE_C = """#include <stdio.h>
#include <stddef.h>
size_t porter_probe_answer(void);
int main(void) { printf("PROBE_OK answer=%zu\\n", porter_probe_answer()); return 0; }
"""


def _require_cc() -> None:
    """The fourth of the same bargain as PORTER_REQUIRE_UV/DOCKER/SYSTEMD.

    Every test here needs a *real* compiled object -- the whole point is that
    the ELF header is read rather than described -- so a host with no C compiler
    can only skip them, and a skipped test is green. On anything that is meant
    to be evidence, PORTER_REQUIRE_CC=1 turns the skip into a failure. It is in
    `.github/workflows/ci.yml`'s `env:` block for that reason; a variable that
    exists and is never armed is the silence AGENTS.md is about.
    """
    if shutil.which("cc"):
        return
    if os.environ.get("PORTER_REQUIRE_CC", "") not in ("", "0"):
        pytest.fail(
            "cc is not on PATH and PORTER_REQUIRE_CC is set: this run would "
            "have skipped every test that reads a real ELF header.",
            pytrace=False)
    pytest.skip("cc not on PATH")


def _compile(root: Path, *, rpath: bool = True) -> tuple[Path, Path]:
    """`probe` and the `libporterprobe.so.1` it links. -> (probe, library).

    The same two commands `examples/native-binary/porter.yaml` bakes, run
    directly so the refusal tests can vary one of them. `-Wl,-soname` is what
    puts a bare soname in probe's NEEDED entry rather than a build-host path.
    """
    _require_cc()
    root.mkdir(parents=True, exist_ok=True)
    (root / "helper.c").write_text(HELPER_C)
    (root / "probe.c").write_text(PROBE_C)
    lib = root / "libporterprobe.so.1"
    probe = root / "probe"
    subprocess.run(["cc", "-O2", "-fPIC", "-shared", "-Wl,-soname,libporterprobe.so.1",
                    "-o", str(lib), str(root / "helper.c")], check=True)
    link = ["cc", "-O2", "-o", str(probe), str(root / "probe.c"),
            f"-L{root}", "-l:libporterprobe.so.1"]
    if rpath:
        link.append("-Wl,-rpath,$ORIGIN")
    subprocess.run(link, check=True)
    return probe, lib


def _component(**kw) -> Component:
    base = dict(name="native", package="demo-native", description="d",
                kind="command", bin_name="demo-probe", module="runner",
                source_paths=["runner.py"],
                native_binaries=["probe", "libporterprobe.so.1"])
    base.update(kw)
    return Component(**base)


@pytest.fixture
def src(tmp_path) -> Path:
    """A source root holding the runner module and the two compiled objects."""
    root = tmp_path / "src"
    _compile(root)
    (root / "runner.py").write_text("def main():\n    print('ok')\n")
    return root


# --- the derivation ----------------------------------------------------------

def test_depends_names_what_the_client_provides_and_not_what_the_payload_does(src):
    """The two halves of rule 11, on a tree holding ONLY the native payload.

    Deliberately not the assembled package: a bundled interpreter contributes
    `libc6` and `libcrypt1` all by itself, so a `Depends:` read off the .deb
    cannot say whether the binary was read at all. Isolating the two objects is
    what makes the assertion about *them*.

    `libporterprobe.so.1` is in probe's NEEDED entries -- asserted below, so
    this is not a claim about a dependency that was never there -- and it is
    absent from the result because the payload ships it. Asking apt for it on
    an airgapped client is asking for a package no mirror has ever carried.
    """
    needed = subprocess.run(["objdump", "-p", str(src / "probe")],
                            capture_output=True, text=True, check=True).stdout
    assert "libporterprobe.so.1" in needed, needed
    assert "libc.so.6" in needed, needed

    stage = src.parent / "elf-only"
    (stage / "usr/lib/demo-native").mkdir(parents=True)
    for name in ("probe", "libporterprobe.so.1"):
        shutil.copy2(src / name, stage / "usr/lib/demo-native" / name)

    depends = derive_depends(stage)
    assert "libc6" in depends, depends
    assert not [d for d in depends if "porterprobe" in d], (
        f"the payload's own library reached Depends: {depends} -- apt on an "
        "airgapped client cannot fetch a soname no mirror carries")


def test_the_binary_is_staged_in_the_payload_root_with_its_mode_intact(src, tmp_path):
    """Staged under `usr/lib/<pkg>/`, beside the source, still executable.

    dpkg preserves the mode it finds, so this is the one property between the
    source tree and the client that porter can lose silently: a binary that
    arrives 644 installs at rc=0 and dies with "Permission denied" at its first
    exec, with the file visibly present and exactly the right size.
    """
    _require_uv()
    staged = assemble(_component(), PY, src, tmp_path / "stage")
    libdir = staged.stage / "usr/lib/demo-native"
    probe, lib = libdir / "probe", libdir / "libporterprobe.so.1"
    assert probe.is_file() and lib.is_file()
    assert probe.stat().st_mode & 0o111, oct(probe.stat().st_mode)
    # Magnitude, not existence: a truncated copy is still a file.
    assert probe.stat().st_size == (src / "probe").stat().st_size > 8000
    assert "libc6" in staged.control["Depends"], staged.control


# --- the refusals ------------------------------------------------------------

def test_a_native_binary_whose_libraries_do_not_resolve_is_refused_by_name(
        src, tmp_path):
    """GUARD, and the property the plan calls out.

    The library is compiled, `probe` links it, and the manifest does not declare
    it. Nothing on the build host resolves `libporterprobe.so.1` and nothing on
    the client will either: without this refusal the .deb builds at rc=0 with a
    `Depends:` that is short by exactly the entry that mattered, and the binary
    dies at its first exec on a machine with no network to fix it from.

    Two assertions and the second is the one that registers: the refusal names
    the soname -- an adopter reading "the build failed" learns nothing -- and it
    lands *before* 97 MB of interpreter is vendored, so nothing is staged.
    """
    stage = tmp_path / "stage"
    with pytest.raises(RuntimeError, match="libporterprobe.so.1"):
        assemble(_component(native_binaries=["probe"]), PY, src, stage)
    assert not stage.exists() or not any(stage.iterdir()), (
        "the refusal fired after the stage was populated: on a real payload "
        "that is a 97 MB interpreter vendored for a package that cannot start")


def test_a_native_binary_that_is_not_executable_is_refused(src, tmp_path):
    """GUARD. 644 installs at rc=0 and cannot be exec'd.

    Refused rather than chmod'ed: the source tree is the adopter's, and a build
    that produced a non-executable program produced the wrong thing. porter
    repairing it would hide that from the one person who can fix it.
    """
    (src / "probe").chmod(0o644)
    with pytest.raises(ValueError, match="not executable"):
        assemble(_component(), PY, src, tmp_path / "stage")


def test_a_shared_library_is_allowed_to_be_644(src, tmp_path):
    """CONTROL for the refusal above, and it is not symmetry for its own sake.

    Debian ships shared libraries 644 -- they are data to the loader, not
    programs -- so a refusal that fired on them would refuse exactly the CUDA
    libraries this feature exists for. Without this test the previous one could
    be satisfied by a check that refuses every non-executable file, and the
    first real payload would be the thing that discovered it.
    """
    _require_uv()
    (src / "libporterprobe.so.1").chmod(0o644)
    staged = assemble(_component(), PY, src, tmp_path / "stage")
    lib = staged.stage / "usr/lib/demo-native/libporterprobe.so.1"
    assert lib.stat().st_mode & 0o777 == 0o644, oct(lib.stat().st_mode)


def test_a_native_binary_that_is_not_an_elf_object_is_refused(src, tmp_path):
    """GUARD. A shell script named like a binary has no NEEDED entries at all.

    `derive_depends` would return an empty list for it -- truthfully -- and
    every library it really needs would go undeclared. That is rule 11's
    failure with every check apparently passing, which is why the refusal is on
    the magic bytes and not on the file's name.
    """
    (src / "probe").write_text("#!/bin/sh\necho hello\n")
    (src / "probe").chmod(0o755)
    with pytest.raises(ValueError, match="not an ELF object"):
        assemble(_component(), PY, src, tmp_path / "stage")


def test_a_native_binary_that_is_not_there_is_refused(src, tmp_path):
    """GUARD. `native_binaries: [build/probe]` before the step that compiles it."""
    (src / "probe").unlink()
    with pytest.raises(ValueError, match="is not a file"):
        assemble(_component(), PY, src, tmp_path / "stage")


def test_a_native_binary_colliding_with_a_source_entry_is_refused(src, tmp_path):
    """GUARD. Both are staged under their own basename in one directory.

    `shutil.copy2` onto an existing file overwrites it without a word, so the
    package would ship one of the two under a name the manifest claims for the
    other -- porter's characteristic bug, arriving through a directory porter
    writes four different things into.
    """
    _require_uv()
    # The library and not `probe`: it links nothing but libc, so the soname
    # refusal cannot fire first and what is measured here is the collision.
    shutil.copy2(src / "libporterprobe.so.1", src / "runner.py")
    with pytest.raises(ValueError, match="already there"):
        assemble(_component(source_paths=["runner.py"],
                            native_binaries=["runner.py"]),
                 PY, src, tmp_path / "stage")


# --- the gallery entry, installed --------------------------------------------

@pytest.mark.docker
def test_the_native_binary_example_installs_and_the_binary_runs(
        tmp_path_factory, docker_image):
    """`examples/native-binary`, built and run the way a client receives it.

    Everything above is about the build. This is the only place the two things
    that survive *packaging* are observed: the exec bit dpkg wrote to disk, and
    the `$ORIGIN` rpath resolving `libporterprobe.so.1` out of
    /usr/lib/<pkg>/ on a machine whose ld.so.conf has never heard of it.

    `--network none` against an image asserted to have no `python3` is the
    airgap: nothing here can reach an index, a wheel or a mirror, and the
    interpreter that runs `runner.py` can only be the vendored one.
    """
    _require_uv()
    porter = shutil.which("porter")
    assert porter, "the `porter` console script is not on PATH: use `uv run`"
    root = tmp_path_factory.mktemp("native")
    dist = root / "dist"
    proc = subprocess.run(
        [porter, "build", str(EXAMPLES / "native-binary/porter.yaml"),
         "--out", str(dist), "--stage", str(root / "stage")],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{dist}:/debs:ro", docker_image, "bash", "-c",
         "set -e; dpkg -i /debs/*.deb >/dev/null; "
         "stat -c 'MODE=%a' /usr/lib/porter-example-native/probe; "
         "porter-probe"],
        capture_output=True, text=True)
    out = result.stdout + result.stderr
    assert "PROBE_OK answer=42" in out, out
    assert "MODE=755" in out or "MODE=775" in out, (
        f"dpkg wrote the binary without an exec bit:\n{out}")
    assert result.returncode == 0, out
