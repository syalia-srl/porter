"""`Depends:` derived from ELF headers, and the four ways deriving it can lie.

A hand-written `Depends:` is how a package installs cleanly on the client and
then cannot open a window, and it goes stale silently on the next upstream
build. So porter reads the headers -- but every failure mode of *reading* them
is a silently short list, which is a package that installs and then dies. Each
refusal below exists because the obvious implementation returns `[]` instead.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from porter.depends import derive_depends, packages_owning


def _elf_needing(dest: Path, soname: str) -> Path:
    """A real ELF whose NEEDED names `soname`, built without a compiler.

    A copy of /bin/bash with the `libtinfo.so.6` string in its `.dynstr`
    overwritten in place. The replacement must be the SAME LENGTH -- .dynstr is
    a packed table of NUL-terminated strings and every offset into it is
    absolute, so a longer name shifts every later entry and objdump reports
    "file format not recognized" rather than the soname the test meant to
    install (measured 2026-08-08, first attempt).
    """
    old = b"libtinfo.so.6\x00"
    new = soname.encode() + b"\x00"
    assert len(new) == len(old), f"{soname!r} must be {len(old) - 1} characters"
    src = Path(shutil.which("bash"))
    body = src.read_bytes()
    assert body.count(old) >= 1, "this bash does not link libtinfo; pick another donor"
    dest.write_bytes(body.replace(old, new))
    dest.chmod(0o755)
    return dest


# --- what it derives -------------------------------------------------------


def test_derives_packages_from_a_real_binary(tmp_path):
    """A hand-written Depends: is how a package installs cleanly and then cannot
    open a window. Derive it from ELF headers instead."""
    tree = tmp_path / "payload"
    tree.mkdir()
    shutil.copy(shutil.which("bash"), tree / "bash")
    deps = derive_depends(tree)
    assert deps, "no dependencies derived from a real dynamically-linked binary"
    assert any("libc" in d for d in deps), deps


def test_derives_what_the_vendored_interpreter_actually_links(vendored):
    """The one bundled native binary every porter package carries.

    Measured on zion 2026-08-08 against a python-build-standalone 3.12 tree:
    4 ELF objects, 9 sonames, **two** packages -- `libc6` and `libcrypt1`.
    `libcrypt1` is the whole argument for this module: it arrives through the
    single dynamically-linked extension in the tree (`_crypt`), nothing in the
    manifest names it, and a hand-written list would not have it. Its absence
    on a client is an interpreter that imports `crypt` and dies.
    """
    deps = derive_depends(vendored)
    assert "libc6" in deps, deps
    assert "libcrypt1" in deps, (
        f"the vendored interpreter's _crypt extension links libcrypt.so.1 and "
        f"nothing else in porter names it; derived: {deps}"
    )


def test_ignores_libraries_the_payload_ships_itself(tmp_path):
    """A bundled tree carries its own .so files; those are not system deps.

    Written with a **real** library, and with the positive control first. A
    four-byte stub named `libselfshipped.so.1` would exercise nothing: no
    object in the tree links it, so an implementation with the exclusion
    deleted passes too. Here the same package is derived before the library is
    staged and absent after it, so the exclusion is the only difference.
    """
    tree = tmp_path / "payload"
    tree.mkdir()
    shutil.copy(shutil.which("bash"), tree / "bash")

    cache = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout
    libtinfo = Path(next(line.strip().split()[-1] for line in cache.splitlines()
                         if line.strip().startswith("libtinfo.so.6 ")))
    owner = packages_owning([libtinfo])[0]
    assert owner in derive_depends(tree), (
        f"control: bash links libtinfo.so.6 and {owner} should be derived when "
        "the payload does not ship it"
    )

    shutil.copy(libtinfo, tree / "libtinfo.so.6")
    assert owner not in derive_depends(tree), (
        f"the payload ships libtinfo.so.6 itself; {owner} is not a system "
        "dependency of this tree"
    )


def test_ignores_an_origin_relative_soname_the_payload_ships(vendored):
    """The self-shipped exclusion has to compare basenames, not strings.

    Measured: the vendored interpreter's `python3.12` carries
    `NEEDED $ORIGIN/../lib/libpython3.12.so.1.0` -- the NEEDED entry is a
    *path*, not a bare soname, so subtracting the set of staged `.so`
    basenames from it leaves it in. It then resolves to nothing, and an
    implementation that drops what it cannot resolve reports success having
    silently discarded it, while one that refuses the unresolvable (as porter
    does, below) refuses every package it builds.
    """
    needed = subprocess.run(
        ["objdump", "-p", str(vendored / "python/bin/python3.12")],
        capture_output=True, text=True).stdout
    assert "$ORIGIN/../lib/libpython3.12.so.1.0" in needed, (
        "this interpreter build no longer carries the $ORIGIN NEEDED entry this "
        "test is the regression test for; re-measure before deleting it"
    )
    assert not any("python" in d for d in derive_depends(vendored)), derive_depends(vendored)


def test_empty_tree_derives_nothing(tmp_path):
    assert derive_depends(tmp_path) == []


def test_a_tree_of_shell_scripts_derives_nothing(tmp_path):
    """The desktop package's own shape when `browser: system`.

    It ships a launcher, a .desktop entry and a PNG -- no ELF at all -- so its
    dependencies are the tools the launcher invokes, declared in desktop.py.
    An empty list here is correct and must not be confused with the empty list
    a broken derivation returns; the refusals below are what separate them.
    """
    tree = tmp_path / "payload"
    (tree / "usr/bin").mkdir(parents=True)
    (tree / "usr/bin/app-desktop").write_text("#!/bin/sh\nexec xdg-open http://x\n")
    (tree / "usr/bin/icon.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    assert derive_depends(tree) == []


def test_a_file_shorter_than_the_elf_magic_is_not_a_crash(tmp_path):
    """`open(f).read(4) == b"\\x7fELF"` on a two-byte file is a real tree.

    A payload holding an empty marker file, a one-byte flag or a fifo must not
    take the build down, and must not be probed as an object either.
    """
    tree = tmp_path / "payload"
    tree.mkdir()
    (tree / "empty").write_bytes(b"")
    (tree / "short").write_bytes(b"\x7fE")
    assert derive_depends(tree) == []


def test_derived_depends_are_sorted_and_unique(tmp_path):
    """Two objects linking the same library must not produce it twice.

    A `Depends:` with a repeated package is accepted by dpkg and diffs
    differently on every build, which is how a control field stops being
    reviewable.
    """
    tree = tmp_path / "payload"
    tree.mkdir()
    shutil.copy(shutil.which("bash"), tree / "bash")
    shutil.copy(shutil.which("bash"), tree / "bash-again")
    deps = derive_depends(tree)
    assert deps == sorted(set(deps)), deps


# --- the refusals ----------------------------------------------------------


def test_refuses_a_soname_the_build_host_cannot_resolve(tmp_path):
    """An unresolvable NEEDED is a missing dependency, not an absent one.

    If the build host has no `libzzzzz.so.6`, the airgapped client certainly
    has not, and dropping it silently is the exact failure this module exists
    to end: the package builds, lints, installs at rc=0 and the binary dies at
    its first exec with "cannot open shared object file".
    """
    tree = tmp_path / "payload"
    tree.mkdir()
    _elf_needing(tree / "app", "libzzzzz.so.6")
    with pytest.raises(RuntimeError, match="libzzzzz.so.6"):
        derive_depends(tree)


def test_refuses_an_object_objdump_cannot_read(tmp_path):
    """A truncated or corrupt object is not a dependency-free one.

    `objdump -p` exits non-zero and prints nothing to stdout, so an
    implementation that returns `[]` on a bad rc reports a package with no
    dependencies for a payload it could not read at all. Reproduced with a
    real ELF whose .dynstr offsets have been shifted -- objdump says "file
    format not recognized" (measured 2026-08-08).
    """
    tree = tmp_path / "payload"
    tree.mkdir()
    src = Path(shutil.which("bash")).read_bytes()
    # A LONGER replacement shifts every later .dynstr offset -- the corruption
    # objdump actually refuses, as opposed to a random byte it would not notice.
    (tree / "app").write_bytes(src.replace(b"libtinfo.so.6\x00", b"libtinfooo.so.6\x00"))
    with pytest.raises(RuntimeError, match="objdump"):
        derive_depends(tree)


def test_refuses_a_library_no_package_owns(tmp_path):
    """A library apt cannot deliver is not a dependency porter may declare.

    Anything hand-installed under /usr/local, or dropped in by a vendor
    tarball, resolves perfectly on the build host and maps to no package at
    all. `dpkg -S` exits 1 saying so; taking its stdout and moving on emits a
    `Depends:` with the interesting entry missing.
    """
    unowned = tmp_path / "libhandmade.so.1"
    unowned.write_bytes(b"\x7fELF")
    with pytest.raises(RuntimeError, match="libhandmade"):
        packages_owning([unowned])


def test_packages_owning_maps_a_real_library(tmp_path):
    """The positive control for the refusal above.

    Without it, `packages_owning` raising on everything would satisfy the
    refusal test and nothing would notice.
    """
    libc = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout
    path = next(line.strip().split()[-1] for line in libc.splitlines()
                if line.strip().startswith("libc.so.6 "))
    assert packages_owning([Path(path)]) == ["libc6"]
