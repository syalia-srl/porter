"""Upstream B: serves immediately. Standard library only.

Its whole job is to be a *second* dependency, so that the gateway's `After=`
and `Requires=` carry a list. One entry cannot distinguish a list that was
emitted from a list that was truncated to its first element.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"beta\n")

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    port = int(os.environ["PORT"])
    print(f"beta: listening on {port}", file=sys.stderr, flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
