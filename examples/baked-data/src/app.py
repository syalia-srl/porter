"""A read-only HTTP view of the baked corpus. Stdlib only.

Opens the database **read-only** through a URI, which is the right mode for a
package payload and also the honest one: `/usr/share/<pkg>/` is package-owned,
and a service that could write there would be writing into a directory dpkg
replaces on the next upgrade. Client state belongs in `/var/lib/<pkg>/`, which
this package never touches.
"""
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer

DB_PATH = os.environ.get("DB_PATH", "/usr/share/baked-data/corpus.db")
PORT = int(os.environ.get("PORT", "9100"))


def count() -> int:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return con.execute("SELECT count(*) FROM article").fetchone()[0]
    finally:
        con.close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's spelling
        body = json.dumps({"articles": count(), "db": DB_PATH}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Silence the per-request stderr line: journald already timestamps."""


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
