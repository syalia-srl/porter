"""`build:` — the escape hatch, and the proof that it is a hatch and not a hole.

The spec's argument for it is that porter serves four repos it was not designed
around, so the first component whose shape the schema does not anticipate would
fork the tool -- porter must not become "a second thing to fight". The argument
*against* it is obvious and is the thing this file has to answer: a stage porter
did not write is a stage porter did not check, and a package that skipped the
lint is worth less than no tool at all.

So the claim under test is narrow and total. **The hatch bypasses assembly, not
the guarantees.** Everything either side of the assemble stage runs on the
hook's output exactly as it runs on porter's own, and a custom build cannot ship
anything the built-in assembler would have been refused for. Each of the FHS
lint's clauses is exercised here *through a hook* -- top-level allowlist,
`/etc/<pkg>/env`, undeclared conffiles, a pre-staged `DEBIAN/`, absolute
symlinks -- and so is the `sh -n` pass, which for a hook is not a formality: a
hook is far likelier than porter to write shell.

**Everything runs through the `porter` console script**, which is the surface an
adopter reaches and the one that has broken before (`tests/test_cli.py`). It is
also cheap here in a way it is nowhere else in this suite: a hook component
vendors no interpreter, so the whole file runs in seconds and needs no `uv`.

**Failures are asserted on the artifact, not the message.** A refusal that
raises the right sentence and writes a .deb anyway is the failure this repo is
about, so every negative test asserts `dist/` is empty. The message substring is
checked as well, and second: it says the refusal that fired was the one named,
rather than some other error that happened to abort the build first.
"""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from porter.assemble import assemble
from porter.spec import load
from porter.types import Component, Python

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "custom-build"

PACKAGE = "hatch-demo"
BASE_MANIFEST = {
    "package": PACKAGE,
    "version": "1.0",
    "description": "a component porter's schema cannot describe",
    "maintainer": "porter <porter@example.com>",
    "architecture": "all",
    "build": "build.sh",
}


def _porter() -> str:
    """The installed console script -- loud rather than skipped.

    Same bargain as `conftest._require_uv`: a run that quietly declined to
    exercise the entry point is the silence these files exist to end.
    """
    exe = shutil.which("porter")
    if exe is None:
        pytest.fail("the `porter` console script is not on PATH: use "
                    "`uv run --extra dev pytest`", pytrace=False)
    return exe


def _project(work: Path, script: str, **manifest_extra) -> Path:
    """A one-component project whose payload is produced by `script`."""
    proj = work / "project"
    proj.mkdir(parents=True)
    (proj / "build.sh").write_text(script)
    (proj / "porter.yaml").write_text(
        yaml.safe_dump({**BASE_MANIFEST, **manifest_extra}))
    return proj


def _build(work: Path, manifest: Path) -> subprocess.CompletedProcess:
    """`porter build`, run from `work` with relative paths, as a user would."""
    return subprocess.run(
        [_porter(), "build", str(manifest.relative_to(work))],
        cwd=work, capture_output=True, text=True)


def _debs(work: Path) -> list[Path]:
    return sorted((work / "dist").glob("*.deb"))


def _refused(work: Path, proc: subprocess.CompletedProcess, phrase: str) -> None:
    """A refusal is a non-zero rc AND no package. The second is the real claim.

    porter's characteristic bug is the wrong artefact produced at rc=0, so a
    test that reads only the message would keep passing against a build that
    complained and shipped anyway. The stage is checked too: `porter build`
    removes the tree it created, pass or fail, and a hook's stage is one it
    created.
    """
    assert proc.returncode != 0, f"the build SUCCEEDED: {proc.stdout}"
    assert _debs(work) == [], f"refused and wrote a package anyway: {_debs(work)}"
    assert phrase in proc.stderr, proc.stderr
    assert not (work / "build" / PACKAGE).exists(), "the stage was left behind"


def _extract(deb: Path, into: Path) -> Path:
    subprocess.run(["dpkg-deb", "-x", str(deb), str(into)], check=True)
    return into


# --- the hook runs, and its output is what ships ----------------------------

WORKING_HOOK = """
mkdir -p "$PORTER_STAGE/usr/share/$PORTER_PACKAGE"
printf 'produced by the hook\\n' > "$PORTER_STAGE/usr/share/$PORTER_PACKAGE/payload.txt"
"""


def test_the_hook_runs_instead_of_the_assembler_and_its_tree_is_the_package(
        tmp_path):
    """The headline: porter stages nothing and packages what the script wrote.

    Two magnitude assertions, in opposite directions, and both are needed. The
    payload's own bytes prove the file arrived with something in it. The .deb
    being *small* proves the assembler did not also run: every other package in
    this gallery carries a vendored interpreter and none is under 20 MB, so a
    hook build that quietly went through `assemble` as well would be an order of
    magnitude larger and every path assertion below would still pass.
    """
    proj = _project(tmp_path, WORKING_HOOK)
    proc = _build(tmp_path, proj / "porter.yaml")
    assert proc.returncode == 0, proc.stderr

    (deb,) = _debs(tmp_path)
    assert deb.name == f"{PACKAGE}_1.0_all.deb"
    tree = _extract(deb, tmp_path / "x")
    assert (tree / f"usr/share/{PACKAGE}/payload.txt").read_text() == \
        "produced by the hook\n"

    assert not (tree / "usr/lib" / PACKAGE / "python").exists(), (
        "the built-in assembler ran as well and vendored an interpreter")
    assert deb.stat().st_size < 1_000_000, (
        f"{deb} is {deb.stat().st_size} bytes: a hook package carries no "
        "vendored interpreter, so this one is carrying something porter staged")


def test_the_hook_receives_the_stage_and_the_components_config_as_environment(
        tmp_path):
    """Every variable, read back out of the package the hook wrote with it.

    Asserted against the manifest rather than against literals, so a variable
    that stops being passed cannot be papered over by editing this file: the
    values come from `BASE_MANIFEST`, which is also what porter read.

    `PORTER_STAGE` is checked by consequence and not by string comparison --
    the file below is only in the .deb if the path porter exported is the path
    porter packaged, which is the thing worth knowing.
    """
    dump = """
    d="$PORTER_STAGE/usr/share/$PORTER_PACKAGE"
    mkdir -p "$d"
    {
      printf 'name=%s\\n' "$PORTER_NAME"
      printf 'package=%s\\n' "$PORTER_PACKAGE"
      printf 'version=%s\\n' "$PORTER_VERSION"
      printf 'architecture=%s\\n' "$PORTER_ARCHITECTURE"
      printf 'maintainer=%s\\n' "$PORTER_MAINTAINER"
      printf 'description=%s\\n' "$PORTER_DESCRIPTION"
      printf 'src_root_has_manifest=%s\\n' "$([ -f "$PORTER_SRC_ROOT/porter.yaml" ] && echo yes)"
      printf 'cwd_is_src_root=%s\\n' "$([ "$PWD" = "$PORTER_SRC_ROOT" ] && echo yes)"
      printf 'stamp_names_package=%s\\n' "$(printf '%s' "$PORTER_STAMP" | grep -c "^package: $PORTER_PACKAGE$")"
    } > "$d/env.txt"
    """
    proj = _project(tmp_path, dump)
    proc = _build(tmp_path, proj / "porter.yaml")
    assert proc.returncode == 0, proc.stderr

    (deb,) = _debs(tmp_path)
    tree = _extract(deb, tmp_path / "x")
    seen = dict(line.split("=", 1) for line in
                (tree / f"usr/share/{PACKAGE}/env.txt").read_text().splitlines())

    assert seen["package"] == BASE_MANIFEST["package"]
    assert seen["version"] == BASE_MANIFEST["version"]
    assert seen["architecture"] == BASE_MANIFEST["architecture"]
    assert seen["maintainer"] == BASE_MANIFEST["maintainer"]
    assert seen["description"] == BASE_MANIFEST["description"]
    # `name:` is absent from the manifest, so it falls back to the package name.
    assert seen["name"] == BASE_MANIFEST["package"]
    assert seen["src_root_has_manifest"] == "yes"
    assert seen["cwd_is_src_root"] == "yes"
    # bake's provenance block reaches the hook, which is the only way a hook
    # can stamp /usr/share/<pkg>/VERSION -- porter will not write into its tree.
    assert seen["stamp_names_package"] == "1"


def test_a_hook_that_exits_non_zero_aborts_the_build_with_its_stderr(tmp_path):
    """The script's own diagnostic has to survive into porter's error.

    A hook fails for reasons only the hook knows -- a missing toolchain, an
    input that was not fetched -- and porter's rc alone sends the adopter to
    read porter's source instead of their own log. The text asserted on is the
    script's, not porter's, so this cannot pass on a message porter invented.
    """
    proj = _project(tmp_path, 'echo "the renderer is not installed" >&2\nexit 3\n')
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "the renderer is not installed")
    assert "rc=3" in proc.stderr, proc.stderr


def test_a_hook_that_fails_after_writing_a_partial_tree_is_still_refused(
        tmp_path):
    """The rc, on its own, with nothing else able to catch the failure.

    The test above passes even with the rc unchecked, because a hook that
    writes nothing is caught one line later by the magnitude check -- two
    guards covering one case, so neither is proved by it. This is the case only
    the rc can see: step one wrote a real payload, step two failed, and the
    stage is full of bytes that are half a package. Unchecked, porter builds it
    at rc=0 and the truncated tree ships.
    """
    proj = _project(tmp_path, WORKING_HOOK + """
    echo "the second stage died" >&2
    exit 1
    """)
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "the second stage died")


def test_a_hook_whose_script_does_not_exist_is_refused_by_name(tmp_path):
    """Before bash reports 127 about a path the adopter cannot place.

    `build:` is relative to the manifest's directory, and a shell's
    `No such file or directory` names an absolute path with no hint of what it
    was resolved against.
    """
    proj = _project(tmp_path, WORKING_HOOK)
    (proj / "build.sh").unlink()
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "relative to the manifest")


# --- the magnitude check: a hook cannot silently produce nothing ------------

def test_a_hook_that_exits_zero_having_written_nothing_is_refused(tmp_path):
    """rc=0 is the least reliable thing about a build script.

    Without this, porter packages an empty tree: a .deb that installs perfectly
    and delivers no payload, on a client with no network to notice from.
    """
    proj = _project(tmp_path, 'mkdir -p "$PORTER_STAGE/usr/lib/$PORTER_PACKAGE"\n')
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "left no files in the stage")


def test_a_hook_that_wrote_only_empty_files_is_refused_by_magnitude(tmp_path):
    """Existence is not the question, and this is the test that says so.

    Twenty files that all exist and none of which has anything in it is what a
    render step that failed halfway leaves behind, and it passes every check
    that asks whether a path is there -- including the one above. A 30,912-byte
    package once passed every path assertion in this repo (AGENTS.md), which is
    why the predicate is bytes and not entries.
    """
    proj = _project(tmp_path, """
    d="$PORTER_STAGE/usr/share/$PORTER_PACKAGE"
    mkdir -p "$d"
    for n in 1 2 3; do : > "$d/part-$n.txt"; done
    """)
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "totalling 0 bytes")


# --- the guarantees the hatch does NOT bypass -------------------------------
#
# Each of these points a custom build at a stage the built-in assembler would
# have been refused for, and asserts the build still fails. If any one of them
# shipped, `build:` would be a way around the lint rather than a way around the
# assembler -- and an adopter would reach for it precisely when their shape is
# unusual, which is exactly when the lint matters most.

def test_a_hook_staging_a_top_level_path_porter_does_not_own_still_fails(
        tmp_path):
    """The refusal that caught an ssh key packaged for an airgapped client."""
    proj = _project(tmp_path, WORKING_HOOK + """
    mkdir -p "$PORTER_STAGE/home/apiad/.ssh"
    printf 'PRIVATE KEY\\n' > "$PORTER_STAGE/home/apiad/.ssh/id_rsa"
    """)
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "top-level path porter does not own")


def test_a_hook_staging_etc_env_still_fails(tmp_path):
    """Rule 4's admin-owned half, which no package may ever carry.

    Shipped, dpkg replaces the admin's secrets on the next upgrade. This is the
    one FHS clause a hook cannot route around by declaring a conffile: porter
    derives conffiles from the tree, so a hook's `/etc` files are declared for
    it -- and `env` is refused before the declaration is even consulted.
    """
    proj = _project(tmp_path, WORKING_HOOK + """
    mkdir -p "$PORTER_STAGE/etc/$PORTER_PACKAGE"
    printf 'SECRET=hunter2\\n' > "$PORTER_STAGE/etc/$PORTER_PACKAGE/env"
    """)
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "admin-owned and never shipped")


def test_a_hook_staging_its_own_DEBIAN_still_fails(tmp_path):
    """DEBIAN/ is porter's, and a hook is exactly who would try to write one.

    Refused rather than deleted, which is deb.py's decision and the right one
    here too: silently dropping a hook's `triggers` would leave a package that
    builds at rc=0 without them.
    """
    proj = _project(tmp_path, WORKING_HOOK + """
    mkdir -p "$PORTER_STAGE/DEBIAN"
    printf '#!/bin/sh\\nexit 0\\n' > "$PORTER_STAGE/DEBIAN/postinst"
    """)
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "DEBIAN/ is porter's to build")


def test_a_hook_staging_a_write_to_a_client_owned_path_still_fails(tmp_path):
    """/var/lib is the client's. Overwritten on the *second* install."""
    proj = _project(tmp_path, WORKING_HOOK + """
    mkdir -p "$PORTER_STAGE/var/lib/$PORTER_PACKAGE"
    printf 'seed\\n' > "$PORTER_STAGE/var/lib/$PORTER_PACKAGE/state.db"
    """)
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "client-owned path")


def test_a_hook_staging_an_absolute_symlink_still_fails(tmp_path):
    """Rule 1's failure mode, and the one a hook reaches by accident.

    A hook that does `ln -s "$(command -v jq)" ...` writes a link into the build
    host that works perfectly here and dangles at the client.
    """
    proj = _project(tmp_path, WORKING_HOOK + """
    ln -s /usr/bin/env "$PORTER_STAGE/usr/share/$PORTER_PACKAGE/tool"
    """)
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "absolute symlink into the build host")


def test_a_hook_staging_a_relative_in_stage_symlink_ships(tmp_path):
    """The positive control for the two symlink refusals above.

    Without it, a lint that refused every symlink would pass both of those
    tests while making the hatch useless for any tree with a link in it -- and
    nothing would say so. A check that cannot distinguish is not a check.
    """
    proj = _project(tmp_path, WORKING_HOOK + """
    ln -s payload.txt "$PORTER_STAGE/usr/share/$PORTER_PACKAGE/latest.txt"
    """)
    proc = _build(tmp_path, proj / "porter.yaml")
    assert proc.returncode == 0, proc.stderr

    (deb,) = _debs(tmp_path)
    listing = subprocess.run(["dpkg-deb", "--contents", str(deb)],
                             capture_output=True, text=True, check=True).stdout
    assert "latest.txt -> payload.txt" in listing, listing


def test_a_hook_writing_shell_that_does_not_parse_still_fails(tmp_path):
    """`sh -n` over /usr/bin, and for a hook it is not a formality.

    porter's own generated shell is nearly all fixed strings; a hook's is
    whatever the script printed. An unbalanced quote builds, lints, installs at
    rc=0 and fails on the client with `returned error exit status 2`.
    """
    proj = _project(tmp_path, WORKING_HOOK + """
    mkdir -p "$PORTER_STAGE/usr/bin"
    printf '#!/bin/sh\\necho "unterminated\\n' > "$PORTER_STAGE/usr/bin/$PORTER_PACKAGE"
    """)
    proc = _build(tmp_path, proj / "porter.yaml")
    _refused(tmp_path, proc, "is not valid sh")


def test_every_etc_file_a_hook_staged_is_declared_a_conffile(tmp_path):
    """Derived from the tree, so the lint and the declaration cannot disagree.

    Read back off the built package rather than off the `Staged` that produced
    it: the list dpkg acts on at the client is the one in the .deb, and the two
    are the same only for as long as nothing between them edits it. Without
    this, an admin's edited config is silently replaced on every upgrade -- no
    prompt, no .dpkg-dist, no record.
    """
    proj = _project(tmp_path, WORKING_HOOK + """
    mkdir -p "$PORTER_STAGE/etc/$PORTER_PACKAGE/conf.d"
    printf 'WIDTH=72\\n' > "$PORTER_STAGE/etc/$PORTER_PACKAGE/defaults"
    printf 'EXTRA=1\\n' > "$PORTER_STAGE/etc/$PORTER_PACKAGE/conf.d/extra.conf"
    """)
    proc = _build(tmp_path, proj / "porter.yaml")
    assert proc.returncode == 0, proc.stderr

    (deb,) = _debs(tmp_path)
    declared = subprocess.run(["dpkg-deb", "-I", str(deb), "conffiles"],
                              capture_output=True, text=True, check=True)
    assert sorted(declared.stdout.split()) == [
        f"/etc/{PACKAGE}/conf.d/extra.conf", f"/etc/{PACKAGE}/defaults"]


def test_depends_is_derived_from_what_the_hook_staged(tmp_path):
    """Rule 11 holds for a hook, and it matters more here than anywhere else.

    `Depends:` is derived from the ELF headers of what is on disk, never
    hand-written -- and a hook is the likeliest place for a native binary
    porter never compiled to appear, precisely because "we build our own
    binary" is one of the shapes the schema cannot describe. A hand-kept list
    is how a package installs cleanly and then cannot start; here there is no
    list at all, because `build:` has no key to write one in.

    `/bin/true` is the smallest real dynamically-linked ELF on any Debian-family
    build host, and it is copied rather than compiled so this needs no toolchain.
    """
    proj = _project(tmp_path, """
    mkdir -p "$PORTER_STAGE/usr/lib/$PORTER_PACKAGE"
    cp /bin/true "$PORTER_STAGE/usr/lib/$PORTER_PACKAGE/tool"
    """, architecture="amd64")
    proc = _build(tmp_path, proj / "porter.yaml")
    assert proc.returncode == 0, proc.stderr

    (deb,) = _debs(tmp_path)
    depends = subprocess.run(["dpkg-deb", "--field", str(deb), "Depends"],
                             capture_output=True, text=True, check=True).stdout
    assert "libc6" in depends, (
        f"nothing was derived for a staged ELF: Depends={depends.strip()!r}. "
        "The package installs on the build host's twin and cannot start on a "
        "client whose libc came from somewhere else")


# --- the keys the hatch makes porter stop reading ---------------------------

ASSEMBLER_KEYS = [
    ("kind", "service"),
    ("exec", {"module": "app"}),
    ("source", ["src/app.py"]),
    ("data", ["corpus"]),
    ("requirements", ["fastapi"]),
    ("python", {"version": "3.11"}),
    ("env", {"PORT": "8000"}),
    ("admin_keys", ["TOKEN"]),
    ("bin_name", "demo"),
    ("after", ["alpha"]),
    ("schedule", "daily"),
    ("migrations", [{"before_version": "1.0", "script": "true"}]),
]


@pytest.mark.parametrize("key,value", ASSEMBLER_KEYS, ids=[k for k, _ in ASSEMBLER_KEYS])
def test_an_assembler_key_beside_a_build_hook_is_refused(tmp_path, key, value):
    """Read by nothing once the hook is on, so accepting it is dropping it.

    `admin_keys:` is the expensive one to get wrong: no `<pkg>-setup` is
    written, `/etc/<pkg>/env` is never created, and an operator is left looking
    for a wizard the manifest promised. Every one of these builds, lints and
    installs at rc=0 with the manifest still claiming otherwise.
    """
    manifest = tmp_path / "porter.yaml"
    manifest.write_text(yaml.safe_dump({**BASE_MANIFEST, key: value}))
    with pytest.raises(ValueError, match=f"declares build:.*{key}"):
        load(manifest)


def test_the_same_refusal_holds_for_a_component_built_in_python(tmp_path):
    """The loader is not the only door, and most of this suite uses the other one.

    A refusal that lives only in `spec.py` is one every in-process caller walks
    straight past -- including porter's own tests, which build `Component`
    objects directly. Then the behaviour the suite pins is not the behaviour the
    tool has.
    """
    component = Component(
        name="demo", package=PACKAGE, description="d", kind="custom",
        module="", build="build.sh", admin_keys=["TOKEN"])
    with pytest.raises(ValueError, match="declares build:.*admin_keys"):
        assemble(component, Python(), tmp_path, tmp_path / "stage")


def test_a_non_bundled_interpreter_beside_a_hook_is_refused(tmp_path):
    """The one key `spec.py` can name and `assemble` cannot tell from a default.

    `python:` omitted and `python: {package: system}` arrive here as the same
    defaulted `Python` in every field but this one, which is why the in-process
    refusal keys on `bundled` rather than on presence.
    """
    component = Component(name="demo", package=PACKAGE, description="d",
                          kind="custom", module="", build="build.sh")
    with pytest.raises(ValueError, match="declares build:.*python.package"):
        assemble(component, Python(package="system"), tmp_path, tmp_path / "stage")


def test_kind_custom_without_a_hook_is_refused_rather_than_assembled(tmp_path):
    """`custom` is what a hook component's absent `kind:` defaults to, and it
    must not become a silent no-op kind for anything else.

    It is not in `SUPPORTED_KINDS`, so a component carrying it down the ordinary
    path is refused by name -- rather than staged as whichever branch porter
    guessed, which would be a payload with neither a unit nor a wrapper.
    """
    component = Component(name="demo", package=PACKAGE, description="d",
                          kind="custom", module="app")
    with pytest.raises(ValueError, match="unknown component kind 'custom'"):
        assemble(component, Python(), tmp_path, tmp_path / "stage")


def test_a_hook_component_still_declares_its_control_fields(tmp_path):
    """`build:` drops the assembly keys and not the package's identity."""
    manifest = tmp_path / "porter.yaml"
    manifest.write_text(yaml.safe_dump(
        {k: v for k, v in BASE_MANIFEST.items() if k != "description"}))
    with pytest.raises(ValueError, match="missing.*'description'"):
        load(manifest)


def test_a_hook_will_not_build_on_top_of_someone_elses_tree(tmp_path):
    """The hook is handed the same empty stage the assembler would have got.

    A hook that appends to whatever it finds would otherwise inherit the
    previous component's payload and ship both, at rc=0 -- and the extra files
    are precisely the ones nobody re-reads.
    """
    component = Component(name="demo", package=PACKAGE, description="d",
                          kind="custom", module="", build="build.sh")
    stage = tmp_path / "stage"
    (stage / "usr").mkdir(parents=True)
    (stage / "usr/leftover").write_text("someone else's payload\n")
    with pytest.raises(ValueError, match="stage root is not empty"):
        assemble(component, Python(), tmp_path, stage)


# --- the gallery entry ------------------------------------------------------

def test_the_custom_build_example_builds_from_a_clean_tree(tmp_path):
    """The acceptance test: `porter build examples/custom-build/porter.yaml`.

    Built from the gallery file itself and not a copy -- the acceptance test for
    an example is that *it* builds, and a copy is one indirection away from the
    file an adopter reads.

    What it asserts is that the example is the shape it claims to be: a package
    with no Python in it. Every path comes out of the manifest, so an example
    that renames its package takes this red rather than passing against a tree
    that has moved on.
    """
    manifest = yaml.safe_load((EXAMPLE / "porter.yaml").read_text())
    pkg = manifest["package"]
    proc = subprocess.run(
        [_porter(), "build", str(EXAMPLE / "porter.yaml"),
         "--out", str(tmp_path / "dist"), "--stage", str(tmp_path / "build")],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    (deb,) = sorted((tmp_path / "dist").glob("*.deb"))
    assert deb.name == f"{pkg}_{manifest['version']}_{manifest['architecture']}.deb"
    tree = _extract(deb, tmp_path / "x")

    assert not (tree / "usr/lib" / pkg).exists(), (
        "the example claims to carry no Python and staged an interpreter")
    tool = tree / "usr/bin/porter-report"
    assert tool.exists() and tool.stat().st_mode & 0o111, (
        f"{tool} is not executable: dpkg preserves the mode and the operator "
        "cannot run it")
    # The generated assets, checked for content: the whole claim of the example
    # is that its payload is produced by the script and copied from nothing.
    assert "built by the porter build hook" in \
        (tree / "usr/share" / pkg / "summary.txt").read_text()
    assert f"package: {pkg}" in (tree / "usr/share" / pkg / "VERSION").read_text()


def test_the_examples_shipped_tool_runs_against_its_own_conffile(tmp_path):
    """The payload is exercised, not merely listed.

    `sh -n` proves the tool parses; it does not prove the tool runs, and the two
    are different questions. This one caught a real bug while the example was
    being written: the description carries an apostrophe, it was interpolated
    unquoted into `/etc/<pkg>/report.conf`, and the tool -- which sources that
    file -- died with `Unterminated quoted string`. The .deb built, linted and
    installed at rc=0, and porter's `sh -n` pass could not see it, because the
    file that did not parse is under /etc and porter only reads /usr/bin.
    """
    manifest = yaml.safe_load((EXAMPLE / "porter.yaml").read_text())
    pkg = manifest["package"]
    proc = subprocess.run(
        [_porter(), "build", str(EXAMPLE / "porter.yaml"),
         "--out", str(tmp_path / "dist"), "--stage", str(tmp_path / "build")],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    (deb,) = sorted((tmp_path / "dist").glob("*.deb"))
    root = _extract(deb, tmp_path / "x")

    # Run it with its own absolute paths rewritten to the extracted tree: the
    # package is not installed here, and rewriting is the honest way to reach
    # the same code with the same conffile. The container tests are where an
    # installed package is exercised in place.
    tool = root / "usr/bin/porter-report"
    tool.write_text(tool.read_text().replace("/etc/", f"{root}/etc/")
                    .replace("/usr/share/", f"{root}/usr/share/"))
    ran = subprocess.run(["sh", str(tool)], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert pkg in ran.stdout and "built by the porter build hook" in ran.stdout
