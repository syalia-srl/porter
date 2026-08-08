"""The payload half of examples/service-fastapi.

/health reports where each of its two values came from, which is what makes the
split observable from outside the package: `tuning` proves the package-owned
defaults were read, `greeting` proves the admin-owned env was read *after* them
and won.

os.environ[...] and not .get(...): a missing key must fail loudly at the first
request. A default here would let a unit that never read its EnvironmentFile
serve a plausible answer, which is the shape of failure this whole design is
built to refuse.
"""
import os

from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"greeting": os.environ["GREETING"], "tuning": os.environ["TUNING"]}
