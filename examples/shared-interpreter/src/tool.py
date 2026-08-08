"""The command half: the second component that shares the one interpreter.

Standard library only, so what this entry adds to the example is the *sharing*
and nothing else -- two packages, ~1 MB between them, and one 97 MB interpreter
package that both `Depends:` on by exact version.

It prints `sys.executable` because that is the observable half: on the client
that path is /usr/lib/<interpreter-package>/python/bin/python3.12, in neither
component's own directory, which is what "shared" means in one line of output.
"""
import sys


def main() -> None:
    print(f"TOOL_OK python={sys.executable}")


if __name__ == "__main__":
    main()
