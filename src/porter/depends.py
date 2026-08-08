"""`Depends:` derived from what the payload actually links.

Rule 11 at the door: a hand-written dependency list is how a package installs
cleanly on the client and then cannot open a window, and it goes stale silently
on the next upstream build. So porter reads the ELF headers of everything it
stages and maps the result to target-distro packages.

**`objdump -p`, never `ldd`.** `ldd` executes the dynamic loader once per file.
Over a 380 MB browser tree that took long enough to look like a deadlock and had
to be killed; `objdump` parses the header and runs nothing. The measured
pipeline on that tree was 6 ELF objects -> 32 sonames -> **24 packages**
(`libnss3`, `libgbm1`, `libcups2t64`, `libatk-bridge2.0-0t64`, `libxkbcommon0`,
...) -- a list nobody would get right by hand. On a porter package's own
payload, the vendored interpreter, it is 4 objects -> 9 sonames -> `libc6` and
`libcrypt1`, the second arriving through the single dynamically-linked extension
in the tree and named nowhere in the manifest.

Every step here **refuses** what it cannot answer rather than returning a
shorter list. That is the whole discipline of the module: a derivation that
drops what it could not resolve produces a `Depends:` that is not wrong in any
visible way -- the build is green, the lint passes, dpkg is satisfied -- and the
binary dies at its first exec on a client with no network to fix it from.

The mapping is to **the build host's** packages. Ubuntu 22.04 is porter's build
floor precisely so that mapping is meaningful downstream (`libcrypt1` and
`libc6` are spelled the same on 22.04 through 26.04); building on a host newer
than the client's distro can still name a package the client's apt has never
heard of, and there is nothing in the headers that would reveal it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

ELF_MAGIC = b"\x7fELF"


def _elf_objects(tree: Path) -> list[Path]:
    """Every real file under `tree` whose first four bytes are the ELF magic.

    Symlinks are skipped rather than followed: a tree that ships
    `libfoo.so -> libfoo.so.1` would otherwise be read twice, and one carrying
    an absolute link would be read from the *build host* -- which deb.py's lint
    refuses at package time, but this runs first.

    The read is guarded because a payload legitimately holds files shorter than
    the magic (an empty marker, a one-byte flag) and things that are not files
    at all in the OSError sense.
    """
    found = []
    for path in sorted(tree.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("rb") as fh:
                head = fh.read(4)
        except OSError:
            continue
        if head == ELF_MAGIC:
            found.append(path)
    return found


def _sonames(elf: Path) -> set[str]:
    """The `NEEDED` entries of one object, verbatim.

    Verbatim matters: a NEEDED entry is not always a bare soname. The vendored
    interpreter's `python3.12` carries
    `NEEDED $ORIGIN/../lib/libpython3.12.so.1.0` -- a payload-relative *path* --
    and the caller compares basenames for exactly that reason.
    """
    try:
        proc = subprocess.run(["objdump", "-p", str(elf)],
                              capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "objdump is not on PATH, so porter cannot derive Depends: from the "
            "payload. Install binutils. Deriving nothing would emit a package "
            "with an empty Depends: for a payload full of native code"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"objdump could not read {elf}: rc={proc.returncode} "
            f"{proc.stderr.strip()}. An object porter cannot read is not an "
            "object with no dependencies -- returning an empty list here is a "
            "package that installs at rc=0 and dies at its first exec"
        )
    return {line.split()[1] for line in proc.stdout.splitlines()
            if "NEEDED" in line and len(line.split()) >= 2}


def _self_shipped(tree: Path, objects: list[Path]) -> set[str]:
    """The basenames the payload provides for itself.

    A bundled tree carries its own `.so` files and those are not system
    dependencies -- an interpreter that ships `libpython3.12.so.1.0` must not
    make the client apt-install a Python. Symlinks count here (they are how a
    tree spells `libfoo.so -> libfoo.so.1`) even though they are not read as
    objects above.
    """
    own = {path.name for path in tree.rglob("*") if ".so" in path.name}
    return own | {obj.name for obj in objects}


def _library_cache() -> dict[str, str]:
    """soname -> path, from `ldconfig -p`.

    First entry wins: the cache lists a soname once per ABI (`libc.so.6
    (libc6,x86-64)` and `(libc6, OS ABI: ...)`), and both are the same package.
    """
    proc = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True)
    index: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 4 and parts[0] not in index:
            index[parts[0]] = parts[-1]
    return index


def resolve_sonames(sonames: list[str]) -> list[Path]:
    """Where the build host keeps each library. Refuses one it does not have.

    An unresolvable soname is a **missing** dependency, not an absent one: if
    the build host has no `libfoo.so.6`, the airgapped client certainly has
    not, and every alternative to raising here is silent. Dropping it emits a
    `Depends:` short by exactly the entry that mattered.
    """
    cache = _library_cache()
    missing = [so for so in sonames if so not in cache]
    if missing:
        raise RuntimeError(
            f"the payload links {', '.join(missing)} and `ldconfig -p` on this "
            "build host resolves none of them, so porter cannot say which "
            "package provides them. Either the library is missing here -- in "
            "which case it is missing on the client too -- or the payload ships "
            "it under a name this tree does not carry"
        )
    return [Path(cache[so]) for so in sonames]


def packages_owning(paths: list[Path]) -> list[str]:
    """The packages that own each library, in one `dpkg -S`.

    One call and not one per file: `dpkg -S` reads the whole file list of every
    installed package, and 32 invocations of it is what turns a browser tree's
    derivation from seconds into minutes.

    A path no package owns is refused. Anything hand-installed under
    /usr/local, or dropped in by a vendor tarball, resolves perfectly on the
    build host and maps to nothing -- and apt on the client cannot deliver a
    file that came from a tarball here.
    """
    if not paths:
        return []
    proc = subprocess.run(["dpkg", "-S", *(str(p) for p in paths)],
                          capture_output=True, text=True)
    owners: dict[str, set[str]] = {}
    for line in proc.stdout.splitlines():
        if ": " not in line:
            continue
        names, path = line.rsplit(": ", 1)
        # `libc6:amd64` -- the architecture qualifier is dpkg's, not a package
        # name, and a Depends: carrying it pins the package to one architecture.
        owners.setdefault(path.strip(), set()).update(
            n.strip().split(":")[0] for n in names.split(","))
    unowned = [str(p) for p in paths if str(p) not in owners]
    if unowned:
        raise RuntimeError(
            f"no package owns {', '.join(unowned)} on this build host, so there "
            "is nothing porter can put in Depends: for it. apt on the client "
            "cannot deliver a file that arrived here outside dpkg -- install "
            "the distro package, or ship the library inside the payload"
        )
    return sorted({pkg for names in owners.values() for pkg in names})


def refuse_a_native_object_the_target_cannot_run(objects: list[Path]) -> None:
    """Refuse a declared native binary whose libraries do not resolve, by name.

    The same predicate `resolve_sonames` applies to a staged tree, run against
    the **source** files instead and therefore *before* 97 MB of interpreter is
    vendored on top of a payload that cannot start. `assemble` calls both: this
    one early, on `native_binaries:` as the manifest declares them, and
    `derive_depends` at the end on everything that was actually staged. Neither
    subsumes the other -- this one sees only what the manifest names, and that
    one sees only what survived staging.

    Better a red build than a package that installs and cannot start. A binary
    whose `NEEDED` entries the build host cannot resolve is one the airgapped
    client certainly cannot: apt has nothing to fetch and no network to fetch it
    over, and the failure surfaces as an exec that dies with a linker error in a
    unit's status, weeks later, on a machine nobody can ssh into.

    What counts as provided by the payload is the **declared set itself** --
    `llama-server` beside the `libggml*.so` and CUDA libraries the project
    vendors with it, which is the shape this exists for. Deliberately not the
    whole staged tree, because the tree does not exist yet; the consequence is
    that a native object linking something only the *interpreter* tree carries
    is refused here and would have passed later. Ship it in `native_binaries:`
    or accept the refusal -- there is no reading of it that is silent.
    """
    provided = {path.name for path in objects}
    cache = _library_cache()
    for obj in objects:
        missing = sorted(so for so in _sonames(obj)
                         if PurePosixPath(so).name not in provided and so not in cache)
        if missing:
            raise RuntimeError(
                f"native binary {obj.name} links {', '.join(missing)}, which "
                f"`ldconfig -p` on this build host does not resolve and this "
                f"payload does not ship ({', '.join(sorted(provided))}). The "
                "client has neither, and no network to fetch them over: the "
                "package would install at rc=0 and the binary would die at its "
                "first exec. Add the library to native_binaries:, or install "
                "the distro package that provides it on the build host"
            )


def derive_depends(tree: Path) -> list[str]:
    """The target-distro packages every native object under `tree` needs.

    Empty for a tree with no ELF in it, which is a real and correct answer: the
    desktop package with `browser: system` ships a shell launcher, a `.desktop`
    entry and a PNG, and depends on the tools its launcher runs rather than on
    anything it links. Every *other* way of arriving at an empty list is a
    refusal above.
    """
    tree = Path(tree)
    objects = _elf_objects(tree)
    if not objects:
        return []
    needed: set[str] = set()
    for obj in objects:
        needed |= _sonames(obj)
    own = _self_shipped(tree, objects)
    external = sorted(so for so in needed if PurePosixPath(so).name not in own)
    return packages_owning(resolve_sonames(external))
