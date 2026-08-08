"""The corporate machine's front end. Standard library only.

It reads `API_URL` and does *not* connect to it at start-up. That is the whole
of what a cross-machine dependency can be: the technical machine's API is on
another box, so `After=` cannot express the relationship and neither can
`Requires=` -- systemd orders units on one host and knows nothing about the
other. A front end that refused to start until its remote backend answered
would be a service that is down for the whole of any network outage, on a
machine whose operator can see nothing wrong with it.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

API_URL = os.environ.get("API_URL", "http://127.0.0.1:9101/")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"suite-desk -> {API_URL}\n".encode())

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    port = int(os.environ["PORT"])
    print(f"suite-desk: listening on 127.0.0.1:{port}, api at {API_URL}",
          file=sys.stderr, flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
