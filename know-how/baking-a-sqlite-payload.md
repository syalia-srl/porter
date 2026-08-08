# know-how: shipping a SQLite database inside a package

*When to reach for it:* a package's payload includes a SQLite database — a
corpus, a search index, a beaver store — built by a `bake:` step. Also when a
client reports empty results, missing recent rows, or
`attempt to write a readonly database`, none of which reproduce on the build
host.

**The rule:** a shipped database must be **checkpointed** and **out of WAL
mode**. Two separate mistakes, two separate one-line fixes, and neither is
visible on the machine that built it.

```python
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # rows into the file
con.execute("PRAGMA journal_mode=delete")        # file readable without sidecars
con.close()
```

`porter.bake` refuses both, but the fix belongs in the build script — the guards
exist to catch the day someone removes these lines.

## Mistake 1 — rows left in the write-ahead log

beaver runs SQLite in WAL mode. Rows committed by a process that did not
checkpoint live in `<db>-wal`, and `<db>` stays a valid, readable, **stale**
database. Reads on the build host see everything, because the WAL is sitting
next to the file. The package carries `<db>` alone.

Measured 2026-08-08 — two runs of 500 inserts, the first checkpointed and the
second not:

| file | size | rows a reader sees |
|---|---|---|
| `corpus.db` + `corpus.db-wal` in place | 49,152 + 49,472 | 1000 |
| `corpus.db` copied out on its own | 49,152 | **500** |

Every existence check passes. Every magnitude check passes. Half the corpus is
gone. This shipped once for real.

The database does **not** have to be young or small for this: the file is only
as fresh as its last checkpoint, so a long-lived corpus with one uncheckpointed
incremental step loses exactly that step.

## Mistake 2 — a checkpointed database still in WAL mode

Checkpointing is necessary and not sufficient. A reader of a WAL-mode database
creates `<db>-shm` beside it — even `mode=ro`, even with a 0-byte WAL, because
WAL readers coordinate through that file. Baked data is staged into
`/usr/share/<pkg>/`, which is root-owned, and the service runs as the static
non-root user of rule 8.

Measured 2026-08-08, same file in a `chmod 555` directory:

| journal mode | read from a writable dir | read from a read-only dir |
|---|---|---|
| `wal` | 1 row | `attempt to write a readonly database` |
| `delete` | 1 row | 1 row |

So the package builds at rc=0, lints, installs at rc=0, and dies at the first
query on a client with no network. The build host never sees it: the bake
directory belongs to whoever ran the build.

`PRAGMA journal_mode=delete` removes `-wal` and `-shm` outright and leaves one
self-contained file. `immutable=1` in the reader's URI also works and is worse:
it puts the fix in every caller instead of in the artifact.

## Checking without destroying the evidence

**Never open the database to check it.** SQLite checkpoints when the last
connection closes, so `sqlite3.connect(db)` — even just to count rows — moves
the stranded frames into the file and the check then reports it clean. A guard
that repairs what it is looking for is worse than no guard.

Everything `porter.bake` checks is read off the filesystem:

| question | answer |
|---|---|
| is this a SQLite file? | first 16 bytes are `SQLite format 3\0` — by content, never by suffix (`corpus.db`, `.beaver/store`) |
| is data stranded? | `<db>-wal` exists and is larger than 0 bytes |
| is it in WAL mode? | header byte 18 (file-format write version) is `2`; `1` is a rollback journal |

`<db>-shm` is deliberately **not** checked: it survives a TRUNCATE checkpoint at
full size (32,768 bytes beside a 0-byte WAL), so keying on it would refuse every
correctly checkpointed bake.

The WAL check refuses a `wal_checkpoint(FULL)` result too, where the frames
*have* reached the database and copying `<db>` alone would in fact be safe.
That is deliberate. Telling FULL from no-checkpoint means matching the WAL's
frame salts against the database header — reimplementing recovery to decide
whether to trust a file — and the fix is the same word either way.

## In tests

To *produce* a stranded WAL, the writer must not exit normally: a clean
interpreter shutdown closes the connection and checkpoints, so the bug repairs
itself before the assertion runs. Use `os._exit(0)`. That is not artificial — a
killed ETL, a `docker stop` mid-build, or a builder that never calls `close()`
is the same thing.

To *read* without repairing, open `file:<db>?mode=ro` with `uri=True`. A
read-write connection checkpoints on close and the next assertion then measures
the fix rather than the bug.

To reproduce the read-only-directory failure, copy the database into a **fresh**
directory before `chmod 555`. Reading it once while the directory is still
writable creates `<db>-shm`, and once that exists the read needs no write
permission at all — the check then passes no matter what. That is how the first
version of `test_a_checkpointed_database_left_in_wal_mode_is_refused` passed
spuriously.

And assert on the **archive**, not the unpacked directory. `dpkg-deb -c` lists
what the package contains; an extracted tree is a directory any earlier test may
have opened a database in, leaving sidecars that were never shipped.
