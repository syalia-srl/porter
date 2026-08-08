"""stateful-demo 1.9's payload: the version whose state schema 2 replaces.

It writes `/var/lib/stateful-demo/state.json` at schema 1, where `notes` is a
list of bare strings. The first note is GREETING, which the admin set with
`stateful-demo-setup` -- so the state file this produces is genuinely the
client's own data and not a fixture, which is the only thing that makes
"the client's data survived the migration" worth asserting.

Nothing here knows about schema 2 or about migrations. It is a previous
release, frozen.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCHEMA = 1
STATE = Path(os.environ["STATE_DIR"]) / "state.json"


def load() -> dict:
    """The state file, created on first start if the client has none yet."""
    if not STATE.exists():
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(
            {"schema": SCHEMA, "notes": [os.environ["GREETING"]]}))
    return json.loads(STATE.read_text())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(load()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    # At startup, not at the first request: the state file has to exist for the
    # upgrade to have something to migrate, and a service that has been started
    # is the ordinary way a client comes to have state.
    load()
    HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
