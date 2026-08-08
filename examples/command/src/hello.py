"""The command payload: reads its arguments, prints, exits. Stdlib only.

A `command` component is the shape with the least machinery around it, and the
things it must get right are therefore the things nothing else is watching:

- **It runs as whoever typed it.** There is no unit, no `User=`, no static
  system user and no `StateDirectory=`. So it may not assume a writable
  directory anywhere: `/usr/lib/<pkg>` is root-owned and read-only to the
  operator, and `/var/lib/<pkg>` does not exist because no postinst created it.
  Anything this writes goes where the caller asked it to go.
- **Relative paths are the caller's, not the payload's.** The wrapper porter
  installs sets `PYTHONPATH` and does not `cd`, precisely so that
  `porter-hello ./notes.txt` names the file in the operator's own directory.
  This prints `Path(...).resolve()` so that the property is observable rather
  than merely intended -- `assemble._wrapper` documents the failure, and this
  is where a test can see it.
- **`__main__` is not the entry point; `main()` is.** rule 3 says
  `python -m hello`, so this module is executed as `__main__` by the wrapper.
  A console script would have been the obvious alternative and its shebang is
  an absolute build-host path.
"""
import pathlib
import sys

VERSION = "1.0"


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--version":
        print(f"porter-hello {VERSION}")
        return

    # sys.executable is the vendored interpreter's installed path on the
    # client. Printed because it is the one line that distinguishes "a command
    # ran" from "the command porter shipped ran": on a client with no system
    # python3 there is nothing else that could have answered, and on a
    # developer's laptop this is what tells the two apart.
    print(f"porter-hello {VERSION} running on {sys.executable}")
    for arg in args:
        print(f"  {arg} -> {pathlib.Path(arg).resolve()}")


if __name__ == "__main__":
    main()
