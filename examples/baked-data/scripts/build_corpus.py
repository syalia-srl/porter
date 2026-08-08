"""Build the corpus this package ships. Run by the `bake:` stage, never at install.

Stands in for leyes-cuba's real ETL, and reproduces the one discipline that
matters. SQLite in WAL mode writes committed rows into `<db>-wal`, and they
reach `<db>` only at a checkpoint. A builder that ends without one leaves a
database that is complete on this host -- readers see through the WAL beside it
-- and stale in the package, because the package carries `<db>` alone.

So this script ends with `PRAGMA wal_checkpoint(TRUNCATE)`, which moves every
frame into the database and empties the WAL to zero bytes.

And then with `PRAGMA journal_mode=delete`, which is the half that is easy to
miss. A checkpointed database is complete and still in WAL *mode*, and any
reader of a WAL-mode database creates `<db>-shm` next to it. This file is staged
into `/usr/share/baked-data/`, which is root-owned, and the service runs as a
non-root user -- so the first query would fail with `attempt to write a readonly
database` on the client, and never here, where the bake directory belongs to
whoever ran the build. Switching journal mode drops both sidecars and leaves one
self-contained file.

porter refuses the build on either mistake, but the fixes belong here: the
guards exist to catch the day someone removes these two lines.
"""
import sqlite3
from pathlib import Path

ROWS = 2000
DB = Path(__file__).resolve().parent.parent / "data" / "corpus.db"


def main() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    # Rebuilt from scratch every time, sidecars included. A bake step that
    # appends to whatever the last run left behind makes the artifact a
    # function of the build host's history rather than of the source tree.
    for suffix in ("", "-wal", "-shm"):
        DB.with_name(DB.name + suffix).unlink(missing_ok=True)

    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=wal")
    con.execute("CREATE TABLE article(id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
    con.executemany(
        "INSERT INTO article(title, body) VALUES (?, ?)",
        [(f"Article {i}", f"Body of article {i}. " * 4) for i in range(ROWS)],
    )
    con.commit()

    # The two lines the whole example is about. Neither substitutes for the
    # other: the first puts the rows in the file, the second makes the file
    # readable from a directory the service cannot write to.
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode=delete")
    con.close()
    print(f"built {DB} with {ROWS} rows ({DB.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
