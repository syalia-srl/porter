"""bake: data built before packaging, and proven fresh.

The centrepiece of this file is `test_rows_stranded_in_a_write_ahead_log_...`,
and everything else is scaffolding around it. leyes-cuba rebuilds its corpus
before every package and beaver runs SQLite in WAL mode, so rows committed by a
process that did not checkpoint sit in `<db>-wal` while `<db>` stays a valid,
readable, *stale* database. The build host sees every row -- it reads through
the WAL lying next to the file -- and the client sees whatever was last
checkpointed. That is porter's characteristic bug in its purest form: a wrong
artefact at rc=0, invisible on the machine that produced it.

So that test carries its own positive control. It does not merely assert the
refusal; it copies the database out on its own, the way the package would, and
shows the rows are *gone* -- 500 of 1000, from a file large enough to satisfy
every magnitude check in this repo. Remove the WAL check and the bake succeeds
and packages that.

Two habits from the rest of the suite. Nothing here opens a database to check
it, because SQLite checkpoints when the last connection closes and such a check
would repair the artefact and then report it clean. And the example is asserted
on *inside the built .deb*, not in the source tree: `dpkg-deb -x` and then count
the rows, because the source tree is exactly the place where the stale-WAL bug
looks fine.
"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import _require_uv

from porter.assemble import assemble
from porter.bake import bake
from porter.types import BakeArtifact, Component, Python

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "baked-data"


def _component(**kw) -> Component:
    """The minimum a bake needs. `assemble`'s other fields are irrelevant here."""
    base = dict(name="corpus", package="porter-corpus", description="d",
                kind="service", module="app")
    base.update(kw)
    return Component(**base)


def _artifact(path: str, min_bytes: int | None = 1) -> BakeArtifact:
    return BakeArtifact(path=path, min_bytes=min_bytes)


def _script(root: Path, name: str, body: str) -> str:
    """Write a helper script and return the step that runs it.

    `sys.executable` rather than `python3`: these steps must run on any host the
    suite runs on, and what is on PATH is not this test's subject. The example's
    own manifest does write `python3`, which is what an adopter writes.
    """
    (root / name).write_text(body)
    return f"{sys.executable} {name}"


# --- steps: order, working directory, and the rc ------------------------------

def test_every_step_runs_in_src_root_in_the_order_declared(tmp_path):
    """Order and cwd, asserted from what the steps wrote rather than from `.ran`.

    `.ran` is bake's own account of itself and would happily agree with a
    reversed loop. The log file is the artefact.
    """
    steps = [f"pwd >> log.txt && echo {n} >> log.txt" for n in (1, 2, 3)]
    result = bake(_component(bake_steps=steps,
                             bake_artifacts=[_artifact("log.txt")]), tmp_path)

    lines = (tmp_path / "log.txt").read_text().split()
    assert lines == [str(tmp_path), "1", str(tmp_path), "2", str(tmp_path), "3"]
    assert result.ran == steps


def test_a_failing_step_aborts_the_bake_naming_the_step_and_its_rc(tmp_path):
    """And the steps after it do not run.

    A bake that carries on past a failed ETL stage builds the rest of the
    package on top of last week's data -- which is the same stale-payload
    failure the WAL check is about, arriving by a different road.
    """
    steps = ["echo one > one.txt", "exit 3", "echo three > three.txt"]
    with pytest.raises(RuntimeError) as err:
        bake(_component(bake_steps=steps,
                        bake_artifacts=[_artifact("one.txt")]), tmp_path)

    assert "step 2 of 3" in str(err.value)
    assert "rc=3" in str(err.value)
    assert "exit 3" in str(err.value), "the message must name the step that failed"
    assert (tmp_path / "one.txt").exists()
    assert not (tmp_path / "three.txt").exists()


def test_a_step_whose_failure_is_hidden_by_a_pipe_still_aborts(tmp_path):
    """`builder | tee build.log` is the shape, and sh reports tee's rc.

    The positive control is the first assertion: under a plain shell this exact
    command line succeeds. So the refusal below cannot be coming from the
    command being obviously broken -- it comes from `pipefail`, and without it
    porter would run a corpus builder that died, read 0, and package whatever
    was already on disk.
    """
    laundered = subprocess.run(["sh", "-c", "exit 7 | cat"], cwd=tmp_path)
    assert laundered.returncode == 0, (
        "the control is broken: this pipeline is supposed to hide its failure")

    with pytest.raises(RuntimeError, match="rc="):
        bake(_component(bake_steps=["exit 7 | cat"],
                        bake_artifacts=[_artifact("nothing")]), tmp_path)


def test_steps_that_declare_no_artifacts_are_refused(tmp_path):
    """A bake whose entire output is an rc verifies nothing.

    An ETL script that meets a missing input, writes an empty table and exits 0
    is the ordinary case. With no `artifacts:`, porter reports a clean bake and
    packages it.
    """
    with pytest.raises(ValueError, match="no artifacts"):
        bake(_component(bake_steps=["true"]), tmp_path)


# --- artifacts: existence is not the check ------------------------------------

def test_an_artifact_the_steps_did_not_produce_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        bake(_component(bake_steps=["true"],
                        bake_artifacts=[_artifact("data/corpus.db")]), tmp_path)


def test_an_empty_database_exists_and_is_refused_for_its_size(tmp_path):
    """The magnitude check, stated in the terms that make it necessary.

    This artefact is a real SQLite database. It opens, it answers queries, and
    it holds nothing -- so every existence check in the world passes it. The
    step "succeeded": `sqlite3.connect` created the file exactly as asked.
    """
    step = _script(tmp_path, "mk.py",
                   "import sqlite3; sqlite3.connect('empty.db').close()\n")
    component = _component(bake_steps=[step],
                           bake_artifacts=[_artifact("empty.db", min_bytes=65536)])

    with pytest.raises(RuntimeError, match="below the declared minimum"):
        bake(component, tmp_path)

    # The control: the refused file is a working database, not a stub.
    assert (tmp_path / "empty.db").exists()
    con = sqlite3.connect(tmp_path / "empty.db")
    assert con.execute("select 1").fetchone() == (1,)
    con.close()


def test_a_directory_artifact_is_measured_by_what_is_inside_it(tmp_path):
    """`dist/` is the other canonical bake output, and `stat` lies about it.

    A directory entry is a few kilobytes whether it holds a compiled bundle or
    nothing, so a magnitude check that used `Path.stat()` on it would pass an
    empty build directory.
    """
    step = _script(tmp_path, "mk.py",
                   "import pathlib; pathlib.Path('dist').mkdir(exist_ok=True)\n")
    with pytest.raises(RuntimeError, match="below the declared minimum"):
        bake(_component(bake_steps=[step],
                        bake_artifacts=[_artifact("dist", min_bytes=4096)]), tmp_path)

    assert (tmp_path / "dist").is_dir()
    (tmp_path / "dist/bundle.js").write_text("x" * 8192)
    assert bake(_component(bake_steps=[step],
                           bake_artifacts=[_artifact("dist", min_bytes=4096)]),
                tmp_path).ran == [step]


def test_an_artifact_with_no_declared_minimum_is_refused(tmp_path):
    """`artifacts: [- path: x]` with no size is an existence check in disguise."""
    (tmp_path / "corpus.db").write_text("")
    with pytest.raises(ValueError, match="no min_bytes"):
        bake(_component(bake_steps=["true"],
                        bake_artifacts=[_artifact("corpus.db", min_bytes=None)]),
             tmp_path)


def test_a_declared_minimum_of_zero_is_refused(tmp_path):
    """The other way to write an existence check while appearing to declare one."""
    (tmp_path / "corpus.db").write_text("")
    with pytest.raises(ValueError, match="minimum of zero"):
        bake(_component(bake_steps=["true"],
                        bake_artifacts=[_artifact("corpus.db", min_bytes=0)]),
             tmp_path)


# --- the WAL, which is what this task exists for ------------------------------

# Three builders, differing only in how they end.
#
# All of them leave via `os._exit`, because a normal interpreter shutdown closes
# the connection, SQLite checkpoints when the *last* connection closes, and the
# bug would repair itself before the test could look at it. A killed ETL, a
# `docker stop` mid-build, or a builder that simply never calls close() is the
# real-world shape of the same thing.
_CHECKPOINTED = """\
import os, sqlite3
con = sqlite3.connect("corpus.db")
con.execute("pragma journal_mode=wal")
con.execute("create table if not exists doc(id integer primary key, body text)")
con.executemany("insert into doc(body) values (?)",
                [("row " + "x" * 60,) for _ in range(500)])
con.commit()
con.execute("pragma wal_checkpoint(truncate)")
os._exit(0)
"""

# Rows committed and left in the WAL: `<db>` is stale, `<db>-wal` has the rest.
_STRANDED = _CHECKPOINTED.replace(
    'con.execute("pragma wal_checkpoint(truncate)")\n', "")

# The shippable shape: checkpointed AND out of WAL mode, so the file is complete
# and needs no sidecar to read.
_SHIPPABLE = _CHECKPOINTED.replace(
    'os._exit(0)', 'con.execute("pragma journal_mode=delete")\nos._exit(0)')


def _rows(db: Path) -> int:
    """Count without repairing.

    `mode=ro` is not decoration. A read-write connection checkpoints when it
    closes, so the obvious `sqlite3.connect(db)` here would move the stranded
    rows into the database and the next assertion would measure the fix rather
    than the bug.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("select count(*) from doc").fetchone()[0]
    finally:
        con.close()


def test_rows_stranded_in_a_write_ahead_log_are_refused(tmp_path):
    """The canonical porter failure, reproduced and then refused.

    Two steps. The first builds 500 rows and checkpoints, so `corpus.db` is a
    large, complete, correct database. The second adds 500 more and does not
    checkpoint. What is on disk afterwards passes everything: the file exists,
    it is 48 KB, it opens, and read from this directory it reports 1000 rows.

    The positive control is the copy, taken before anything reads either file.
    `corpus.db` alone -- which is precisely what the package carries -- reports
    **500**. The other 500 are in `corpus.db-wal`, which is not an artifact, is
    not declared, and is not staged.

    Matched on `uncheckpointed`, a word only this refusal uses. The WAL-mode
    refusal below would also fire on this fixture (Trap 4: a database with a
    non-empty WAL is necessarily in WAL mode), so a looser `match="WAL"` would
    stay green with the stranding check deleted and report a guard that is gone
    as present.
    """
    steps = [_script(tmp_path, "seed.py", _CHECKPOINTED),
             _script(tmp_path, "more.py", _STRANDED)]
    component = _component(bake_steps=steps,
                           bake_artifacts=[_artifact("corpus.db", min_bytes=32768)])

    with pytest.raises(RuntimeError, match="uncheckpointed"):
        bake(component, tmp_path)

    db = tmp_path / "corpus.db"
    assert db.stat().st_size >= 32768, (
        "the control is broken: this database must be big enough to pass the "
        "magnitude check, or the WAL check is not what refused it")
    assert (tmp_path / "corpus.db-wal").stat().st_size > 0

    shipped = tmp_path / "as-packaged"
    shipped.mkdir()
    shutil.copy2(db, shipped / "corpus.db")
    assert _rows(db) == 1000, "read beside its WAL, the build host sees everything"
    assert _rows(shipped / "corpus.db") == 500, (
        "the whole point: packaged on its own, the database is stale and nothing "
        "about it looks wrong")


def test_a_checkpointed_database_left_in_wal_mode_is_refused(tmp_path):
    """Checkpointing is necessary and not sufficient, which the brief did not say.

    This database is complete: one step, 500 rows, `wal_checkpoint(TRUNCATE)`, a
    0-byte WAL. The stranding check above passes it. It is still in WAL *mode*,
    and that is enough to break the client.

    The positive control reproduces the client rather than describing it: put
    the file in a directory the reader cannot write to -- which is what
    `/usr/share/<pkg>/` is to a non-root service -- and the query fails with
    `attempt to write a readonly database`, because a WAL reader must create
    `<db>-shm` and cannot. On the build host, where the bake directory belongs
    to the builder, the identical read succeeds. That asymmetry is the bug.
    """
    step = _script(tmp_path, "seed.py", _CHECKPOINTED)
    component = _component(bake_steps=[step],
                           bake_artifacts=[_artifact("corpus.db", min_bytes=32768)])

    with pytest.raises(RuntimeError, match="journal_mode=delete"):
        bake(component, tmp_path)

    assert (tmp_path / "corpus.db-wal").stat().st_size == 0, (
        "the control is broken: with a non-empty WAL this is the stranding case "
        "and says nothing about journal mode")

    # A pristine copy in each. Reading the same copy twice would prove nothing:
    # the first read creates `corpus.db-shm`, and once that file exists the
    # second read needs no write permission at all. The package ships no
    # sidecar, so a fresh directory is the honest reproduction -- this test
    # passed spuriously until it had one.
    for name in ("writable", "readonly"):
        (tmp_path / name).mkdir()
        shutil.copy2(tmp_path / "corpus.db", tmp_path / name / "corpus.db")

    assert _rows(tmp_path / "writable/corpus.db") == 500, (
        "the control: in a directory it can write to, the same file reads fine")
    (tmp_path / "readonly").chmod(0o555)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            _rows(tmp_path / "readonly/corpus.db")
    finally:
        (tmp_path / "readonly").chmod(0o755)


def test_the_bake_passes_once_the_step_checkpoints_and_leaves_wal_mode(tmp_path):
    """What a shippable artifact looks like, and the proof that it is one.

    Same two steps as the stranding case with `journal_mode=delete` appended to
    the second, so both refusals above are shown to be about the WAL and not
    about anything else in these fixtures. The final assertion is the one that
    matters: all 1000 rows, read from a copy on its own, in a directory the
    reader cannot write to. That is the client.
    """
    steps = [_script(tmp_path, "seed.py", _CHECKPOINTED),
             _script(tmp_path, "more.py", _SHIPPABLE)]
    result = bake(_component(bake_steps=steps,
                             bake_artifacts=[_artifact("corpus.db", min_bytes=32768)]),
                  tmp_path)

    assert result.ran == steps
    # Leaving WAL mode removes both sidecars outright: nothing to strand and
    # nothing to recreate.
    assert not (tmp_path / "corpus.db-wal").exists()
    assert not (tmp_path / "corpus.db-shm").exists()

    shipped = tmp_path / "as-installed"
    shipped.mkdir()
    shutil.copy2(tmp_path / "corpus.db", shipped / "corpus.db")
    shipped.chmod(0o555)
    try:
        assert _rows(shipped / "corpus.db") == 1000
    finally:
        shipped.chmod(0o755)


def test_a_database_inside_a_directory_artifact_is_checked_too(tmp_path):
    """`artifacts: [- path: data]` is the obvious declaration, and it must not
    be the one that switches the WAL check off.

    Detection is by the file's magic bytes and not its suffix, so the database
    here is deliberately named `store` with no extension -- beaver's shape.
    """
    (tmp_path / "data").mkdir()
    body = _STRANDED.replace('"corpus.db"', '"data/store"')
    step = _script(tmp_path, "mk.py", body)

    with pytest.raises(RuntimeError, match="uncheckpointed"):
        bake(_component(bake_steps=[step],
                        bake_artifacts=[_artifact("data", min_bytes=1024)]), tmp_path)


def test_a_non_sqlite_artifact_is_not_subject_to_the_wal_check(tmp_path):
    """A tarball with a `-wal` sibling is somebody's file, not a stranded WAL.

    The check keys on the SQLite magic, so this passes -- a refusal here would
    fire on correct manifests, which is the failure mode a guard cannot afford.
    """
    (tmp_path / "payload.tar").write_bytes(b"\0" * 4096)
    (tmp_path / "payload.tar-wal").write_bytes(b"unrelated")
    assert bake(_component(bake_artifacts=[_artifact("payload.tar", min_bytes=4096)]),
                tmp_path).ran == []


# --- the stamp ----------------------------------------------------------------

def _git_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(root), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.com",
                    "-c", "user.name=t", "commit", "-qm", "seed"], check=True)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True)
    return head.stdout.strip()


def _fields(stamp: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in stamp.strip().splitlines())


def test_the_stamp_names_the_package_the_version_and_the_commit(tmp_path):
    """What a client fault report needs, and what the package version cannot say:
    two builds of `1.0` from different corpora are indistinguishable."""
    sha = _git_repo(tmp_path)
    fields = _fields(bake(_component(version="2.5"), tmp_path).stamp)

    assert fields["package"] == "porter-corpus"
    assert fields["version"] == "2.5"
    assert fields["commit"] == sha
    assert fields["built"].endswith("Z") and fields["built"][4] == "-", fields["built"]


def test_the_stamp_marks_a_tree_that_was_edited_after_its_last_commit(tmp_path):
    """A SHA that does not describe what was built is worse than no SHA: it
    invites the next reader to check that commit out and conclude the fault
    report is impossible."""
    sha = _git_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("edited\n")
    assert _fields(bake(_component(), tmp_path).stamp)["commit"] == f"{sha}-dirty"


def test_the_stamp_says_unknown_outside_a_git_repository(tmp_path):
    """A source tree delivered as a tarball is a legitimate build input."""
    assert _fields(bake(_component(), tmp_path).stamp)["commit"] == "unknown"


def test_a_component_with_no_bake_block_bakes_trivially(tmp_path):
    """Not an error, and still stamped: provenance is not a property of having
    an ETL. Most components have none and every one of them can fail on a
    client."""
    result = bake(_component(), tmp_path)
    assert result.ran == []
    assert _fields(result.stamp)["package"] == "porter-corpus"


# --- examples/baked-data, asserted inside the .deb ----------------------------

def test_the_example_manifest_declares_the_bake_the_code_reads(tmp_path):
    """The gallery is the schema. Read through `from_manifest`, so a `bake:`
    block the loader stops parsing takes this red."""
    component, _ = Component.from_manifest(
        yaml.safe_load((EXAMPLE / "porter.yaml").read_text()))

    assert component.bake_steps == ["python3 scripts/build_corpus.py"]
    assert component.bake_artifacts == [
        BakeArtifact(path="data/corpus.db", min_bytes=65536)]
    assert component.data_paths == ["data/corpus.db"]
    assert "data/corpus.db" not in component.source_paths, (
        "baked data belongs in /usr/share/<pkg>, not in the import root")


@pytest.mark.parametrize("name", ["VERSION", "env.example"])
def test_a_data_entry_that_would_collide_with_a_file_porter_writes_is_refused(
        tmp_path, name):
    """`assemble` stages `data:` into /usr/share/<pkg>/ and then writes VERSION
    and env.example into the same directory, each unconditionally.

    So a corpus called `VERSION` was staged and then silently replaced by the
    provenance stamp: the .deb builds at rc=0, the manifest still lists the
    entry, `dpkg-deb --contents` still shows the path, and the bytes are
    porter's. Narrow, and exactly the shape this repo refuses -- a wrong
    artefact reported as a right one.

    Refused before anything is staged, and the message names the entry.
    """
    component = _component(data_paths=[f"data/{name}"])
    with pytest.raises(ValueError, match=name.replace(".", r"\.")):
        assemble(component, Python(), tmp_path, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.fixture(scope="session")
def example_deb(tmp_path_factory) -> Path:
    """`porter build` on the gallery entry, run the way a user reaches it.

    Copied out of the repo first, so the bake writes its corpus into a scratch
    tree rather than into the checkout -- three agents share this one.
    """
    _require_uv()
    exe = shutil.which("porter")
    if exe is None:
        pytest.fail(
            "the `porter` console script is not on PATH, so this run would have "
            "verified the bake stage by never invoking it. Use "
            "`uv run --extra dev pytest`.", pytrace=False)

    work = tmp_path_factory.mktemp("baked")
    shutil.copytree(EXAMPLE, work / "example")
    proc = subprocess.run([exe, "build", "example/porter.yaml"], cwd=work,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    debs = sorted((work / "dist").glob("*.deb"))
    assert [d.name for d in debs] == ["baked-data_1.0_amd64.deb"]
    assert debs[0].stat().st_size > 20_000_000, (
        f"{debs[0]} is {debs[0].stat().st_size} bytes: a vendored interpreter "
        "cannot be that small, so this is a truncated build")
    return debs[0]


@pytest.fixture(scope="session")
def built_example(example_deb, tmp_path_factory) -> Path:
    """The package, unpacked -- what dpkg will put on the client's disk."""
    unpacked = tmp_path_factory.mktemp("unpacked")
    extract = subprocess.run(["dpkg-deb", "-x", str(example_deb), str(unpacked)],
                             capture_output=True, text=True)
    assert extract.returncode == 0, extract.stderr
    return unpacked


def test_the_corpus_inside_the_deb_holds_every_row_the_bake_built(
        built_example, tmp_path):
    """The assertion that matters, made on the package under the client's terms.

    The build tree is exactly where a stranded WAL looks fine, so this reads the
    database extracted from the .deb. And it reads it from a directory it cannot
    write to, because that is what `/usr/share/<pkg>/` is to the non-root user
    the unit runs as: a WAL-mode payload would fail here and pass everywhere
    else.
    """
    db = built_example / "usr/share/baked-data/corpus.db"
    assert db.exists(), sorted(p.name for p in
                               (built_example / "usr/share/baked-data").iterdir())
    assert db.stat().st_size >= 65536, f"{db} is only {db.stat().st_size} bytes"

    as_installed = tmp_path / "usr-share"
    as_installed.mkdir()
    shutil.copy2(db, as_installed / "corpus.db")
    as_installed.chmod(0o555)
    try:
        con = sqlite3.connect(f"file:{as_installed / 'corpus.db'}?mode=ro", uri=True)
        try:
            assert con.execute("select count(*) from article").fetchone()[0] == 2000
        finally:
            con.close()
    finally:
        as_installed.chmod(0o755)


def test_the_package_carries_no_write_ahead_log_beside_the_corpus(example_deb):
    """A `-wal` in the package would mean the bake shipped its ambiguity along
    with the data -- and dpkg does not know the two files belong together, so an
    upgrade can replace one and not the other.

    Read out of the .deb with `dpkg-deb -c` rather than off an unpacked
    directory, and that is not fastidiousness: the first version of this test
    listed the extracted tree and failed, because the test *above* had opened
    the database and SQLite had created `corpus.db-shm` next to it. The
    directory on disk is not the package. Only the archive is.
    """
    listing = subprocess.run(["dpkg-deb", "-c", str(example_deb)],
                             capture_output=True, text=True, check=True).stdout
    shipped = sorted(line.split()[-1] for line in listing.splitlines()
                     if "/usr/share/baked-data/" in line and not line.startswith("d"))

    assert shipped == ["./usr/share/baked-data/VERSION",
                       "./usr/share/baked-data/corpus.db",
                       "./usr/share/baked-data/env.example"]


def test_the_package_carries_the_provenance_stamp(built_example):
    """`usr/share/<pkg>/VERSION`: package-owned, replaced on upgrade, inside the
    FHS top-level allowlist -- and readable on an airgapped client with `cat`."""
    fields = _fields((built_example / "usr/share/baked-data/VERSION").read_text())
    assert fields["package"] == "baked-data"
    assert fields["version"] == "1.0"
    assert fields["built"].endswith("Z")
    assert fields["commit"], "an empty commit line is worse than the word unknown"


def test_the_baked_corpus_is_not_staged_into_the_import_root(built_example):
    """/usr/lib/<pkg> is the unit's WorkingDirectory and therefore sys.path.
    A corpus there is one `import corpus` away from being a module, and a
    `data/` tree with a stray .py in it shadows the stdlib for the service."""
    libdir = built_example / "usr/lib/baked-data"
    assert (libdir / "app.py").exists()
    assert not (libdir / "corpus.db").exists()
