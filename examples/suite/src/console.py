"""The operator CLI, installed on BOTH machines. Standard library only.

This is the component that makes the suite a suite rather than two unrelated
deployments: it is named by both metapackages, so it is delivered once, built
once, and carried on one USB. dpkg installs it for whichever role arrives
first and the second role finds its dependency already satisfied.

It takes the URL to probe as an argument rather than from a config file,
because a `command` may not declare config at all -- see examples/command for
why that is a refusal and not an omission.
"""
import sys
import urllib.request

VERSION = "1.0"


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--version":
        print(f"suite-console {VERSION}")
        return
    if not args:
        print(f"usage: suite-console <url>   (suite-console {VERSION})",
              file=sys.stderr)
        raise SystemExit(2)

    url = args[0]
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
        print(f"{url} -> {response.status} {body}")
    except OSError as exc:
        # Non-zero, because a probe that cannot reach its target and exits 0 is
        # a probe an operator will trust exactly once.
        print(f"{url} -> unreachable: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
