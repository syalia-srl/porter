"""The technical machine's scheduled job: runs, writes, exits.

`$STATE_DIRECTORY` and not a path of its own choosing -- systemd exports it
because it created `/var/lib/<pkg>` and owns it to the unit's static user, and
that is the one tree the package itself never writes to (AGENTS.md rule 6).
Under `ProtectSystem=strict` anywhere else is read-only anyway, so a job that
picks its own directory works on the build host and fails on the client.
"""
import datetime
import os
import pathlib
import sys


def main() -> None:
    state = pathlib.Path(os.environ.get("STATE_DIRECTORY", "."))
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    report = state / "sweep.txt"
    with report.open("a") as fh:
        fh.write(f"{stamp} swept\n")
    print(f"suite-sweep: appended to {report}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
