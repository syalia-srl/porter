"""The Python side of a package whose real payload is compiled.

`python -m runner` is rule 3's entry point, and all it does is hand over to the
native binary staged beside it. That is the ainbox engine's shape: the service
porter packages is a thin Python wrapper around a compiled server.

The path is derived from `__file__` rather than written out. Both live in
/usr/lib/<pkg>/ -- the payload root, which is the wrapper's PYTHONPATH and a
unit's WorkingDirectory -- so the directory is knowable without the package name
appearing in the source at all. A hardcoded /usr/lib/porter-example-native/ is
a second place the package name is spelled, and the two drift the first time one
of them is renamed.
"""
import os
import sys

BINARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe")


def main() -> None:
    # execv, not subprocess: there is nothing for this process to do afterwards,
    # and replacing it keeps the native program's exit status and its stdio
    # exactly as the operator's shell sees them.
    os.execv(BINARY, [BINARY, *sys.argv[1:]])


if __name__ == "__main__":
    main()
