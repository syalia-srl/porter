import os
import subprocess
import sys
from pathlib import Path

import pytest

from porter import interpreter
from porter.interpreter import vendor, install


def test_vendored_root_is_a_real_directory_not_a_symlink(vendored: Path):
    """uv's managed python dir IS a symlink; cp -a would copy the link and
    vendor nothing. This is the P1b bug, as a regression test."""
    assert (vendored / "python").is_dir()
    assert not (vendored / "python").is_symlink()


def test_vendored_tree_is_substantial(vendored: Path):
    """A magnitude check: a link-copy produces 2 entries, a real one ~3000."""
    assert len(list((vendored / "python").rglob("*"))) > 1000


def test_externally_managed_marker_removed(vendored: Path):
    assert not (vendored / "python/lib/python3.12/EXTERNALLY-MANAGED").exists()


def test_interpreter_runs(vendored: Path):
    out = subprocess.run(
        [str(vendored / "python/bin/python3.12"), "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True, text=True, check=True,
    )
    assert "(3, 12)" in out.stdout


def test_install_puts_packages_in_the_vendored_site_packages(vendored: Path):
    install(vendored / "python/bin/python3.12", ["idna"])
    hits = list((vendored / "python/lib/python3.12/site-packages").glob("idna"))
    assert hits, "idna did not land in the vendored site-packages"


def _fake_venv(root: Path, version: str = "3.12") -> Path:
    """The shape `uv venv` produces, reduced to what vendor() looks at.

    Note it satisfies the naive checks: `bin/python<version>` and
    `lib/python<version>/` both exist, and the binary is a symlink to a real
    interpreter, so even the post-copy `binary.exists()` passes on this host.
    Its tells are `pyvenv.cfg` and the absent stdlib.
    """
    (root / "bin").mkdir(parents=True)
    (root / f"lib/python{version}/site-packages").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text(f"home = /elsewhere/bin\nversion_info = {version}\n")
    binary = root / "bin" / f"python{version}"
    binary.symlink_to(sys.executable)
    return binary


def test_vendor_refuses_a_virtualenv_masquerading_as_an_interpreter_root(tmp_path, monkeypatch):
    """`uv python find <v>` answers with a project virtualenv when its version
    matches, and two directories up from that is the venv, not a
    python-build-standalone root. Copying it would ship a bin/python that is an
    absolute symlink into the build host -- a wrong package, produced silently."""
    found = _fake_venv(tmp_path / "project" / ".venv")
    monkeypatch.setattr(
        interpreter,
        "_run",
        lambda cmd: str(found) if cmd[:3] == ["uv", "python", "find"] else "",
    )

    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError, match="virtualenv"):
        vendor(dest)
    assert not (dest / "python").exists(), "vendor() copied the venv before refusing it"


def test_vendor_refuses_a_root_with_no_interpreter_tree(tmp_path, monkeypatch):
    """The same derivation can land on an ordinary source directory -- porter's
    own repo root, say. Nothing about it is an interpreter; say so and stop."""
    repo = tmp_path / "repo"
    (repo / "src" / "porter").mkdir(parents=True)
    (repo / ".git").mkdir()
    found = repo / "bin" / "python3.12"  # never created
    monkeypatch.setattr(
        interpreter,
        "_run",
        lambda cmd: str(found) if cmd[:3] == ["uv", "python", "find"] else "",
    )

    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError, match="not a python-build-standalone tree"):
        vendor(dest)
    assert not (dest / "python").exists()


def test_vendor_ignores_a_matching_virtualenv_in_the_cwd(tmp_path, monkeypatch, require_uv):
    """The same hazard end to end, against the real `uv python find`: uv
    discovers a venv from the cwd alone, no $VIRTUAL_ENV needed. This is what
    the --system --managed-python flags are for."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["uv", "venv", "--python", "3.12", str(work / ".venv")], check=True,
                   capture_output=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("VIRTUAL_ENV", str(work / ".venv"))

    binary = vendor(tmp_path / "dest")

    # The stdlib is the discriminator: a venv's lib/python3.12/ holds only
    # site-packages, so this is absent unless a real standalone tree was copied.
    assert (binary.parent.parent / "lib/python3.12/os.py").is_file()
    assert not (binary.parent.parent / "pyvenv.cfg").exists()


def _fake_standalone_tree(root: Path, tag: str, version: str = "3.12") -> Path:
    """A tree that passes every *layout* probe vendor() applies, so that only
    provenance can tell two of them apart.

    This is the shape of `/usr` as much as of a managed tree: no `pyvenv.cfg`,
    a `bin/python<version>`, a `lib/python<version>/`, and a stdlib `os.py`.
    Verified on zion 2026-08-07 against the real thing --
    `_standalone_root(Path("/usr/bin/python3.14"), "3.14")` returned `/usr`.
    `tag` lands in a PROVENANCE file so a test can assert *which* tree was
    copied rather than merely that something was.
    """
    (root / "bin").mkdir(parents=True)
    (root / f"lib/python{version}").mkdir(parents=True)
    (root / f"lib/python{version}/os.py").write_text("# stdlib\n")
    (root / "PROVENANCE").write_text(tag)
    binary = root / "bin" / f"python{version}"
    binary.write_text("#!/bin/false\n")
    binary.chmod(0o755)
    return binary


def _fake_run(found: Path, managed: Path):
    """A stand-in for interpreter._run covering both commands the guard uses."""

    def run(cmd: list[str]) -> str:
        if cmd[:3] == ["uv", "python", "find"]:
            return str(found)
        if cmd[:3] == ["uv", "python", "dir"]:
            return str(managed)
        return ""

    return run


def test_vendor_refuses_a_root_outside_uvs_managed_python_dir(tmp_path, monkeypatch):
    """`/usr` clears every layout probe -- no pyvenv.cfg, a bin/python<v>, a
    stdlib os.py -- so layout alone cannot stop `uv python find --system` from
    handing over the system interpreter and vendor() copytree-ing the whole of
    `/usr`, successfully. Provenance stops it: uv did not install that tree, so
    it is not under `uv python dir`."""
    found = _fake_standalone_tree(tmp_path / "usr", "system")
    managed = tmp_path / "uv" / "python"
    managed.mkdir(parents=True)
    monkeypatch.setattr(interpreter, "_run", _fake_run(found, managed))

    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError, match="managed python directory"):
        vendor(dest)
    assert not (dest / "python").exists(), "vendor() copied the system tree before refusing it"


def test_vendor_accepts_an_identical_tree_under_uvs_managed_python_dir(tmp_path, monkeypatch):
    """Positive control for the refusal above. The same tree, differing only in
    where it lives, is vendored -- so the refusal is the provenance predicate
    biting, not a guard that rejects everything."""
    managed = tmp_path / "uv" / "python"
    found = _fake_standalone_tree(managed / "cpython-3.12-linux-x86_64-gnu", "managed")
    monkeypatch.setattr(interpreter, "_run", _fake_run(found, managed))

    binary = vendor(tmp_path / "dest")

    assert binary.is_file()
    assert (binary.parent.parent / "PROVENANCE").read_text() == "managed"


FAKE_UV = """#!/bin/sh
# A stand-in for uv on PATH, so this runs through the real subprocess layer.
# It answers `python find` the way a build host that has a system interpreter
# of the requested version does: the managed tree only when asked for it.
echo "$*" >> "$FAKE_UV_LOG"
case "$1 $2" in
"python dir")
    echo "$FAKE_UV_MANAGED_DIR"
    ;;
"python install")
    ;;
"python find")
    if echo "$*" | grep -q -- "--system" && echo "$*" | grep -q -- "--managed-python"; then
        echo "$FAKE_UV_MANAGED_BIN"
    else
        echo "$FAKE_UV_SYSTEM_BIN"
    fi
    ;;
*)
    echo "fake uv: unexpected argv: $*" >&2
    exit 64
    ;;
esac
"""


def test_vendor_asks_uv_for_both_system_and_managed_python(tmp_path, monkeypatch):
    """`--system` means "not a virtualenv", not "uv-managed" -- on Ubuntu 24.04
    `uv python find --system 3.12` answers /usr/bin/python3.12. Only
    `--managed-python` excludes it, and no test constrained that half of the
    pair: on zion, where no system 3.12 exists, dropping it leaves the whole
    suite green while the same code vendors /usr on the real build host.

    A fake uv on PATH supplies the system interpreter zion hasn't got, so the
    flag is constrained here rather than on a host we don't have.
    """
    managed = tmp_path / "uv-python"
    managed_bin = _fake_standalone_tree(managed / "cpython-3.12-linux-x86_64-gnu", "managed")
    system_bin = _fake_standalone_tree(tmp_path / "usr", "system")

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake_uv = bindir / "uv"
    fake_uv.write_text(FAKE_UV)
    fake_uv.chmod(0o755)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UV_LOG", str(log))
    monkeypatch.setenv("FAKE_UV_MANAGED_DIR", str(managed))
    monkeypatch.setenv("FAKE_UV_MANAGED_BIN", str(managed_bin))
    monkeypatch.setenv("FAKE_UV_SYSTEM_BIN", str(system_bin))

    binary = vendor(tmp_path / "dest")

    # Which tree was copied, not merely that one was.
    assert (binary.parent.parent / "PROVENANCE").read_text() == "managed"
    # And the argv that got it, so a rewrite that keeps the outcome by accident
    # still has to keep the flags.
    finds = [line for line in log.read_text().splitlines() if line.startswith("python find")]
    assert len(finds) == 1, finds
    assert {"--system", "--managed-python"} <= set(finds[0].split())


def test_install_succeeds_when_the_externally_managed_marker_is_present(vendored_copy):
    """The check that can fail on `--break-system-packages`.

    The plain install test cannot: vendor() deletes EXTERNALLY-MANAGED two
    steps earlier, so on its tree the flag is a no-op and removing it changes
    nothing. Restore the marker and the flag becomes the only thing standing
    between install() and uv's refusal -- measured on zion 2026-08-07, uv
    0.11.29: rc=2, "The interpreter at ... is externally managed".
    """
    marker = vendored_copy / "python/lib/python3.12/EXTERNALLY-MANAGED"
    marker.write_text("[externally-managed]\nError=managed by uv\n")

    install(vendored_copy / "python/bin/python3.12", ["idna"])

    assert (vendored_copy / "python/lib/python3.12/site-packages/idna").is_dir()
    assert marker.exists(), "install() must not paper over the marker by deleting it"


def _installed_version(python_bin: Path, package: str) -> str:
    out = subprocess.run(
        [str(python_bin), "-c",
         f"import importlib.metadata as m; print(m.version({package!r}))"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def test_install_honours_a_constraints_file(vendored_copy):
    """`constraints=` is shipped behaviour that nothing exercised. The
    unconstrained install is the positive control: it pins what uv picks when
    left alone, so the pinned run cannot pass by coincidence."""
    python_bin = vendored_copy / "python/bin/python3.12"
    install(python_bin, ["idna"])
    unconstrained = _installed_version(python_bin, "idna")

    pin = "3.6"
    assert unconstrained != pin, f"pick a pin uv would not have chosen anyway (got {unconstrained})"
    constraints = vendored_copy / "constraints.txt"
    constraints.write_text(f"idna=={pin}\n")

    install(python_bin, ["idna"], constraints=constraints)

    assert _installed_version(python_bin, "idna") == pin
