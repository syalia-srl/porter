"""Upstream A: initialises first, then serves. Standard library only.

The warm-up is the point. A service that systemd reports `active` is a service
whose process has been forked, not one that is answering -- and a dependent
that trusts `After=` alone will connect to a closed port and fail. porter's
answer is that the dependent retries; this is the upstream that makes it have
to.
"""
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"alpha\n")

    def log_message(self, fmt: str, *args) -> None:
        pass  # the journal already timestamps every line; this doubles it


def main() -> None:
    warmup = float(os.environ.get("WARMUP_SECONDS", "0"))
    if warmup:
        # Written to stderr, which systemd routes to the journal, so an operator
        # watching `systemctl status` sees why the port is not open yet.
        print(f"alpha: initialising for {warmup}s", file=sys.stderr, flush=True)
        time.sleep(warmup)
    port = int(os.environ["PORT"])
    print(f"alpha: listening on {port}", file=sys.stderr, flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
