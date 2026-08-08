"""The service half of the shared-interpreter example.

FastAPI on purpose, and not stdlib: this component is what proves a component's
own requirements still reach it when the interpreter is somebody else's package.
They are installed into /usr/lib/<pkg>/ -- this file's own directory, which is
the unit's WorkingDirectory -- rather than into the shared interpreter's
site-packages, which belongs to a package every component installs.
"""
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict:
    import sys
    return {"status": "ok", "python": sys.executable}
