"""The scheduled job: runs, writes its result, exits. Standard library only.

Two things a job on an airgapped appliance must get right, both of which this
shows:

- **Write to `$STATE_DIRECTORY`, never to a path of your own choosing.** systemd
  exports it because it created the directory -- `/var/lib/<pkg>`, owned by the
  unit's static user -- and it is the one place the package itself never writes
  (AGENTS.md rule 6). A job that writes elsewhere works on the build host and
  hits a read-only filesystem on the client, because the unit runs under
  `ProtectSystem=strict`.
- **Exit non-zero when the work did not happen.** `Type=oneshot` makes systemd
  wait for the process and record its result, so a failed run is visible in
  `systemctl status <pkg>.timer` and the journal. A job that swallows its own
  errors and exits 0 is a job nobody finds out about until the data is missing.
"""
import datetime
import os
import pathlib
import sys


def main() -> None:
    state = pathlib.Path(os.environ.get("STATE_DIRECTORY", "."))
    retention = int(os.environ.get("RETENTION_DAYS", "14"))

    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    report = state / "report.txt"
    with report.open("a") as fh:
        fh.write(f"{stamp} ran, keeping {retention} days\n")

    # Trim to the retention window's worth of lines, so the job has a reason to
    # read what it wrote and the example shows state surviving between runs.
    lines = report.read_text().splitlines()[-retention:]
    report.write_text("\n".join(lines) + "\n")

    print(f"job: wrote {report} ({len(lines)} runs retained)",
          file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
