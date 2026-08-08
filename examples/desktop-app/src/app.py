"""The gallery's desktop entry: a local service with a page worth a window.

`/health` is what the launcher waits on before it opens anything -- a click
right after login arrives while systemd is still starting the unit, and a
browser that opens first shows connection-refused, which reads as a broken
install rather than a slow one. It answers before anything heavy is ready on
purpose: readiness here means "the socket is accepting", which is exactly the
question the launcher is asking.
"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PorterDemo</title>
  <style>
    :root { color-scheme: light dark; }
    body { font: 16px/1.6 system-ui, sans-serif; margin: 0;
           display: grid; place-items: center; min-height: 100vh; }
    main { max-width: 34rem; padding: 2rem; }
    code { background: rgba(127,127,127,.18); padding: .1em .35em;
           border-radius: .25em; }
  </style>
</head>
<body>
  <main>
    <h1>PorterDemo</h1>
    <p>This page is served by a systemd unit on this machine, from an
       interpreter that arrived inside the <code>.deb</code>. Nothing was
       downloaded and no browser was bundled: the window you are reading it in
       is the client's own browser, opened with <code>--app=</code> so it has
       no tabs and no URL bar.</p>
    <p>The launcher waited on <code>/health</code> before opening this window,
       and it is running against a browser profile of its own under
       <code>~/.local/share/porter-example-desktop/</code>.</p>
  </main>
</body>
</html>
"""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE
