"""stateful-demo 1.10's payload: schema 2, and it will not run on schema 1.

The refusal is the point. If this build started happily against an unmigrated
state file -- reading `notes` as strings and writing them back as dicts, or
serving half of each -- then "the service came up after the upgrade" would say
nothing about whether the migration ran, and the e2e test's central assertion
would be untestable. A service that exits rather than guess is also just the
right behaviour: an airgapped client has nobody to notice the difference.

Schema 2 makes each note an object, so a note can grow fields later:
    1: {"schema": 1, "notes": ["hola"]}
    2: {"schema": 2, "notes": [{"text": "hola"}]}
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCHEMA = 2
STATE = Path(os.environ["STATE_DIR"]) / "state.json"


def load() -> dict:
    """The state file, created on first start if the client has none yet."""
    if not STATE.exists():
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(
            {"schema": SCHEMA, "notes": [{"text": os.environ["GREETING"]}]}))
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
    state = load()
    if state["schema"] != SCHEMA:
        sys.exit(
            f"stateful-demo: {STATE} is schema {state['schema']} and this build "
            f"needs {SCHEMA}. The migration in the postinst did not run; refusing "
            "to start rather than serve half-migrated state"
        )
    HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
