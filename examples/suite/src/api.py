"""The technical machine's HTTP backend. Standard library only.

Kept to `http.server` on purpose: the suite's subject is how several packages
are delivered as one name, and a third-party dependency here would put a wheel
download between a reader and that subject. `examples/service-fastapi` is where
requirements are the point.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"suite-api\n")

    def log_message(self, fmt: str, *args) -> None:
        pass  # the journal already timestamps every line


def main() -> None:
    port = int(os.environ["PORT"])
    print(f"suite-api: listening on 127.0.0.1:{port}", file=sys.stderr, flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
