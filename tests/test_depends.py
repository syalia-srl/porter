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

from porter.depends import _elf_objects, _sonames, derive_depends, packages_owning


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

    The vendored interpreter carries
    `NEEDED $ORIGIN/../lib/libpython3.12.so.1.0` -- the NEEDED entry is a
    *path*, not a bare soname, so subtracting the set of staged `.so`
    basenames from it leaves it in. It then resolves to nothing, and an
    implementation that drops what it cannot resolve reports success having
    silently discarded it, while one that refuses the unresolvable (as porter
    does, below) refuses every package it builds.

    **Which object carries it is upstream's business, not porter's.** This
    test used to objdump `python/bin/python3.12` by name, and that spelling of
    the control went stale between two python-build-standalone builds of the
    same 3.12 series (measured 2026-08-10): in 3.12.8 the interpreter binary
    links libpython dynamically and carries the entry; in 3.12.13 libpython is
    linked into the binary and the `$ORIGIN` entry lives in
    `lib/libpython3.so` instead. Nothing about porter changed and the
    regression it guards is exactly as live -- only the file moved. So the
    control asks the tree, not one path in it, and it is still a control: if
    no object in the vendored tree carries a path-shaped NEEDED, this test
    proves nothing and says so rather than passing.
    """
    origin_relative = {
        obj: sorted(so for so in _sonames(obj) if "/" in so)
        for obj in _elf_objects(vendored)
    }
    carriers = {obj: sos for obj, sos in origin_relative.items() if sos}
    assert carriers, (
        "no object in this vendored tree carries a NEEDED entry that is a path "
        "rather than a bare soname, so the basename comparison this test is the "
        "regression test for is not exercised here at all; re-measure against "
        "the current interpreter build before deleting it"
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


def test_refuses_an_unowned_library_under_an_aliased_root_too():
    """The usr-merge alias must not become a way to be owned by nothing.

    `/lib/x86_64-linux-gnu` is exactly where a vendor tarball drops a library,
    and it is also the root that now gets a second spelling asked of dpkg. The
    refusal above uses a tmp_path, which has only one name, so on its own it
    would stay green if the alias lookup ever started answering for a path
    neither spelling owns. Both spellings must miss.
    """
    with pytest.raises(RuntimeError, match="libhandmade"):
        packages_owning([Path("/lib/x86_64-linux-gnu/libhandmade.so.1")])
    with pytest.raises(RuntimeError, match="libhandmade"):
        packages_owning([Path("/usr/lib/x86_64-linux-gnu/libhandmade.so.1")])


def test_packages_owning_maps_a_real_library(tmp_path):
    """The positive control for the refusal above.

    Without it, `packages_owning` raising on everything would satisfy the
    refusal test and nothing would notice.
    """
    libc = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout
    path = next(line.strip().split()[-1] for line in libc.splitlines()
                if line.strip().startswith("libc.so.6 "))
    assert packages_owning([Path(path)]) == ["libc6"]


def test_either_spelling_of_a_usr_merged_path_maps_to_the_same_package():
    """`ldconfig -p` and dpkg's database disagree about /lib versus /usr/lib.

    Both are true names for the same file -- `/lib` is a symlink to `/usr/lib`
    on every release porter supports -- but `dpkg -S` matches its *recorded*
    string and nothing else, so asking it the question in the wrong spelling
    gets "no path found matching pattern" for a library the host obviously has.

    Measured 2026-08-10, one container per release, `ldconfig -p` against
    `dpkg -S` on the path it printed:

        ubuntu 22.04   ldconfig /lib/...      dpkg /lib/...        agree
        debian 12      ldconfig /lib/...      dpkg /lib/...        agree
        ubuntu 24.04   ldconfig /lib/...      dpkg /usr/lib/...    DISAGREE
        debian 13      ldconfig /lib/...      dpkg /usr/lib/...    DISAGREE
        ubuntu 26.04   ldconfig /usr/lib/...  dpkg /usr/lib/...    agree

    So porter derived `Depends:` correctly on its build floor and on the
    newest release, and refused *every* package in between -- including on
    `ubuntu-latest`, which is why CI was red while zion was green. The refusal
    is the honest failure of the two, but it is still a build porter cannot
    do on two of the five releases it claims.

    This asserts both spellings, whichever one the host records, so it is red
    on every one of those releases before the fix: exactly one of the two
    always misses.
    """
    cache = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout
    recorded = Path(next(line.strip().split()[-1] for line in cache.splitlines()
                         if line.strip().startswith("libc.so.6 ")))
    parts = recorded.parts
    other = (Path("/", *parts[2:]) if parts[1] == "usr"
             else Path("/usr", *parts[1:]))
    assert other.exists() and other.samefile(recorded), (
        f"control: {other} is meant to be the other spelling of {recorded} and "
        "this host does not resolve it to the same file, so the test below "
        "would be asserting something other than the usr-merge alias"
    )
    assert packages_owning([recorded]) == packages_owning([other]) != []
