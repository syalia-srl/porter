"""The dependent: refuses to serve until both upstreams answer.

**Exiting non-zero is the design, not a failure to handle an error.** systemd
has no `condition: service_healthy`, so the only thing that can decide this
service is ready is this service, and the only way it can say "not yet" is to
die. `Restart=on-failure` plus `RestartSec` is then the retry loop, and the
convergence it produces is what tests/test_ordering.py boots systemd to
measure.

The alternative -- retry in a loop inside the process and stay up -- looks
tidier and is worse on an appliance: the unit reports `active` while nothing it
promises works, so `systemctl is-active` lies to every operator and every
monitor for as long as the upstream is down.
"""
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"gateway\n")

    def log_message(self, fmt: str, *args) -> None:
        pass


def check(url: str) -> None:
    """Refuse to start if `url` does not answer. Short timeout: a hung upstream
    must not hold the start job open until systemd's own timeout kills it, which
    would turn a three-second retry into a ninety-second one."""
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status != 200:
                raise RuntimeError(f"{url} answered {response.status}")
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        print(f"gateway: upstream {url} is not ready ({exc}); exiting to retry",
              file=sys.stderr, flush=True)
        raise SystemExit(1)


def main() -> None:
    for key in ("ALPHA_URL", "BETA_URL"):
        check(os.environ[key])
    port = int(os.environ["PORT"])
    print(f"gateway: upstreams ready, listening on {port}", file=sys.stderr, flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
