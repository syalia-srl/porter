"""Data built before packaging, and proven fresh.

Stage 1 of the pipeline (docs/design-spec.md): the repo's own ETL, frontend
build or corpus render runs here, before `assemble` stages anything, and
whatever it was supposed to produce is *checked* before 97 MB of interpreter is
vendored on top of it.

**The failure this module exists to refuse.** leyes-cuba rebuilds its corpus
before every package. beaver runs SQLite in **WAL mode**, so rows committed by a
process that did not checkpoint live entirely in `<db>-wal` -- and `<db>` itself
is a perfectly valid database that opens, answers queries, and is *stale*.
Package only `<db>` and the build host sees every row (it reads through the WAL
sitting next to it) while the client gets whatever was last checkpointed, which
may be nothing at all. Measured here 2026-08-08: two runs of 500 inserts, the
first checkpointed and the second not, leave a 49,152-byte `corpus.db` holding
500 rows and a 49,472-byte `corpus.db-wal` holding the other 500. Every
existence check passes. Every magnitude check passes. The package is wrong.

That shape has already shipped once in real life, which is why the WAL check is
the point of this module and not a nicety.

**And a second failure hiding behind it**, found while writing this task's
example. Checkpointing is not enough: a database left in WAL *mode* makes any
reader create `<db>-shm` beside it, and baked data is staged into
`/usr/share/<pkg>/`, which is root-owned while the service runs as a non-root
user. The first query then fails with `attempt to write a readonly database` --
on the client only, because the build host's bake directory belongs to the
builder. So an artifact must be checkpointed *and* out of WAL mode, and both are
refused separately because the fixes are different lines.

**Three consequences for how the checks are written.**

*Never open the database.* SQLite checkpoints on the close of the last
connection, so a check implemented as "connect, count rows" would silently
*repair* the artifact and then report it clean -- a guard that destroys the
evidence it was looking for. Everything here reads bytes off the filesystem and
nothing imports `sqlite3`.

*Magnitude, never existence.* Every artifact declares `min_bytes`, and a
declared minimum of zero is refused: an empty SQLite file exists.

*Read the rc directly, and do not let a pipe launder it.* Steps run under
`bash -e -o pipefail`, so `build_corpus.py | tee build.log` reports the
builder's failure and not `tee`'s success. Without `pipefail` that step is a
gate that cannot fail, which is worth less than no gate at all (AGENTS.md).
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from porter.types import BakeArtifact, Component

# The first 16 bytes of every SQLite database file, including an empty one.
# Detection is by content and never by suffix: leyes-cuba's corpus is
# `corpus.db`, beaver's store is `.beaver/store`, and a WAL check that only
# fires on `*.db` is a WAL check that does not fire.
SQLITE_MAGIC = b"SQLite format 3\x00"

# Offset 18 of the SQLite header is the file-format WRITE version: 1 for a
# rollback journal, 2 for WAL. It is a property of the file itself, so the
# journal mode a database will be opened in can be read without opening it --
# which matters, because opening it is the one thing these checks may not do.
_WRITE_VERSION_OFFSET = 18
_WAL_FORMAT = 2


@dataclass(frozen=True)
class BakeResult:
    """What ran, and the provenance line that names this build.

    `stamp` is text and not a file, because the file it becomes lives at
    `usr/share/<pkg>/VERSION` inside a stage that does not exist yet -- and
    `assemble` refuses to build into a stage anyone else has touched. bake
    computes it; assemble writes it.
    """

    ran: list[str]
    stamp: str


def _refuse_steps_that_check_nothing(component: Component) -> None:
    """A bake that builds something and verifies nothing is a bake that lies.

    Its whole output is an rc, and an ETL script's rc is the least reliable
    thing about it: a corpus builder that hits a missing input, writes an empty
    table and exits 0 is the ordinary case, not the exotic one. With no
    `artifacts:` porter would then report a successful bake, stage the empty
    database and ship it.
    """
    if component.bake_steps and not component.bake_artifacts:
        raise ValueError(
            "bake declares steps but no artifacts: porter would run them and then "
            "check nothing, so a step that exits 0 having produced an empty "
            "database would package it. Declare what each step must have built, "
            "with a min_bytes for each"
        )


def _bash() -> str:
    """The shell steps run under, refused rather than substituted when absent.

    Falling back to `sh` would quietly drop `pipefail` -- the difference between
    a step that reports its builder's failure and one that reports `tee`'s
    success. A gate that disarms itself on a host missing a tool is the exact
    silence AGENTS.md's three PORTER_REQUIRE_* variables exist to end.
    """
    bash = shutil.which("bash")
    if bash is None:
        raise RuntimeError(
            "bake steps need bash: they run under `bash -e -o pipefail` so a "
            "failing command in a pipeline fails the step. sh has no pipefail, "
            "and falling back to it would turn every piped step into a gate that "
            "cannot fail"
        )
    return bash


def _run_steps(steps: list[str], src_root: Path) -> list[str]:
    """Run each step in `src_root`, in order, aborting on the first failure.

    Output is *not* captured. An ETL step is minutes long and its own log is the
    diagnostic; swallowing it to re-print a tail on failure trades the useful
    half for the tidy half. What porter adds is the rc, read directly off the
    completed process and reported with the step that produced it -- a bake
    whose error says "rc=1" and not which of nine steps returned it is a bake
    you debug by bisection.
    """
    ran: list[str] = []
    bash = _bash()
    for n, step in enumerate(steps, start=1):
        proc = subprocess.run([bash, "-e", "-o", "pipefail", "-c", step],
                              cwd=src_root)
        if proc.returncode != 0:
            raise RuntimeError(
                f"bake step {n} of {len(steps)} failed with rc={proc.returncode}: "
                f"{step!r} (run in {src_root})"
            )
        ran.append(step)
    return ran


def _size(path: Path) -> int:
    """Bytes on disk: the file itself, or everything under a directory.

    A directory artifact is the frontend build (`dist/`), and its magnitude is
    the sum of its files -- `Path.stat()` on a directory reports the size of the
    directory *entry*, which is a few kilobytes whether it holds a compiled
    bundle or nothing at all.
    """
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return path.stat().st_size


def _sqlite_files(path: Path) -> list[Path]:
    """Every SQLite database at or under `path`, found by magic bytes.

    A directory artifact is walked, because `artifacts: [data/]` is the obvious
    thing to declare and a WAL check that only looked at the declared path
    itself would not fire on the database inside it. That hole is the whole
    failure this module is about, one directory further out.
    """
    candidates = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
    found = []
    for p in candidates:
        with p.open("rb") as fh:
            if fh.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC:
                found.append(p)
    return found


def _refuse_data_stranded_in_a_write_ahead_log(db: Path) -> None:
    """A SQLite database whose newest rows are in a file porter is not shipping.

    The refusal is on `<db>-wal` being non-empty, and it is deliberately
    conservative: it also refuses a WAL left behind by
    `wal_checkpoint(FULL)`, where the frames *have* reached the database and
    copying `<db>` alone would in fact be safe. Two reasons to keep it that way.
    Telling FULL from no-checkpoint means matching the WAL's frame salts against
    the database header -- reimplementing recovery to decide whether to trust
    the file. And a package with a live WAL beside its database is ambiguous
    even when it is correct: the fix in both cases is one word,
    `wal_checkpoint(TRUNCATE)`, which empties the file and leaves nothing to
    reason about.

    `<db>-shm` is not checked. It survives a TRUNCATE checkpoint at full size
    (measured 2026-08-08: 32,768 bytes next to a 0-byte WAL) and holds no
    committed data -- keying on it would refuse every correctly checkpointed
    bake there is.
    """
    wal = db.with_name(db.name + "-wal")
    if not wal.exists() or wal.stat().st_size == 0:
        return
    raise RuntimeError(
        f"{db} has {wal.stat().st_size} bytes of uncheckpointed write-ahead log "
        f"in {wal.name}: rows committed there are not in the database file, and "
        "porter packages the database file. It reads correctly on this host, "
        "because the WAL is sitting next to it, and arrives stale or blank on the "
        "client. End the bake step with PRAGMA wal_checkpoint(TRUNCATE)"
    )


def _refuse_a_database_still_in_wal_mode(db: Path) -> None:
    """A checkpointed WAL database is complete, and still unreadable at the client.

    Measured 2026-08-08, and it is a second failure hiding behind the first.
    Opening a WAL-mode database -- even `mode=ro`, even with a 0-byte WAL --
    makes SQLite create `<db>-shm` beside it, because WAL readers coordinate
    through that file. Baked data is staged in `/usr/share/<pkg>/`, which is
    root-owned, and the service runs as the static non-root user of rule 8. So
    the first query raises `attempt to write a readonly database` on the client
    and nowhere else: on the build host the bake directory is the builder's own
    and the sidecar is created without complaint.

    Package builds at rc=0, installs at rc=0, dies at the first request, on a
    machine with no network. Refused here, with the one-line fix named: end the
    bake with `PRAGMA journal_mode=delete`, which drops `-wal` and `-shm`
    outright and leaves a single self-contained file.

    Read from the header rather than by opening the database, for this module's
    standing reason: a connection would checkpoint on close and destroy the
    evidence the check above is looking for.
    """
    header = db.open("rb").read(_WRITE_VERSION_OFFSET + 1)
    if len(header) <= _WRITE_VERSION_OFFSET:
        return  # a 16-byte stub is somebody's fixture, and the size check has it
    if header[_WRITE_VERSION_OFFSET] != _WAL_FORMAT:
        return
    raise RuntimeError(
        f"{db} is still in WAL journal mode, so a reader must create {db.name}-shm "
        "beside it. It is staged into /usr/share/<pkg>/, which is root-owned, and "
        "the service runs as a non-root user -- so the first query fails with "
        "'attempt to write a readonly database' on the client and never here. End "
        "the bake step with PRAGMA journal_mode=delete, which removes both sidecars "
        "and leaves one self-contained file"
    )


def _refuse_an_artifact_the_bake_did_not_really_build(
        artifact: BakeArtifact, src_root: Path) -> None:
    """Exists, is big enough, and is not hiding half of itself in a WAL."""
    if artifact.min_bytes is None:
        raise ValueError(
            f"bake artifact {artifact.path!r} declares no min_bytes: without one "
            "this is an existence check, and an empty SQLite database exists -- "
            "it opens, it answers queries, and it has nothing in it"
        )
    if artifact.min_bytes <= 0:
        raise ValueError(
            f"bake artifact {artifact.path!r} declares min_bytes={artifact.min_bytes}: "
            "a minimum of zero is an existence check wearing a magnitude check's "
            "clothes. Declare the size below which the build is known to be wrong"
        )

    path = src_root / artifact.path
    if not path.exists():
        raise RuntimeError(
            f"bake artifact {artifact.path!r} does not exist after the bake "
            f"(looked in {src_root}): the steps reported success and produced "
            "nothing porter can package"
        )

    size = _size(path)
    if size < artifact.min_bytes:
        raise RuntimeError(
            f"bake artifact {artifact.path!r} is {size} bytes, below the declared "
            f"minimum of {artifact.min_bytes}: it was created and not filled. This "
            "is the check that separates 'the file is there' from 'the file is "
            "real'"
        )

    # Stranding first, mode second. The two overlap -- a database with a
    # non-empty WAL is necessarily in WAL mode, so the mode check alone would
    # refuse everything the stranding check refuses -- but they are different
    # diagnoses with different one-line fixes (`wal_checkpoint(TRUNCATE)` versus
    # `journal_mode=delete`), and a message that names the wrong one costs an
    # afternoon. Order decides which an adopter is told about first, and
    # "your rows are not in the file" is the more urgent of the two.
    for db in _sqlite_files(path):
        _refuse_data_stranded_in_a_write_ahead_log(db)
        _refuse_a_database_still_in_wal_mode(db)


def _commit(src_root: Path) -> str:
    """The commit `src_root` was built from, marked `-dirty` when it was not.

    A SHA that does not describe the tree that was actually built is worse than
    no SHA: it invites someone to check out that commit and conclude the fault
    report is impossible. `unknown` when there is no git -- a source tree
    delivered as a tarball is a legitimate build input, not an error.

    Scoped to `src_root` itself (`-- .`), so an unrelated edit elsewhere in a
    monorepo does not mark every package dirty.
    """
    if shutil.which("git") is None:
        return "unknown"
    head = subprocess.run(["git", "-C", str(src_root), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if head.returncode != 0:
        return "unknown"
    sha = head.stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(src_root), "status", "--porcelain", "--", "."],
        capture_output=True, text=True)
    if dirty.returncode != 0 or dirty.stdout.strip():
        return f"{sha}-dirty"
    return sha


def _stamp(component: Component, src_root: Path) -> str:
    """The provenance block that becomes `usr/share/<pkg>/VERSION`.

    une-tools ships exactly this today -- app, date, and the commits it was
    built from -- and it is what makes a fault report from an airgapped client
    actionable. The package version alone does not: two builds of `1.0` from
    different corpora are indistinguishable at the client, and the client is
    where the only copy of the evidence is.

    Plain `key: value` lines so an operator can read it over a phone and `grep`
    can pull one field out of it.
    """
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"package: {component.package}\n"
        f"version: {component.version}\n"
        f"built: {built}\n"
        f"commit: {_commit(src_root)}\n"
    )


def bake(component: Component, src_root: Path) -> BakeResult:
    """Run the component's bake steps, prove what they built, stamp the build.

    A component with no `bake:` block bakes trivially: nothing runs, nothing is
    checked, and it still gets a stamp. Provenance is not a property of having
    an ETL -- a package whose payload is pure source still needs to name the
    commit it came from when a client reports a fault.
    """
    src_root = Path(src_root)
    _refuse_steps_that_check_nothing(component)
    ran = _run_steps(component.bake_steps, src_root)
    for artifact in component.bake_artifacts:
        _refuse_an_artifact_the_bake_did_not_really_build(artifact, src_root)
    return BakeResult(ran=ran, stamp=_stamp(component, src_root))
