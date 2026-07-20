import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")


@app.get("/")
def root():
    return {"status": "Fami API funcionando"}


@app.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if (
        hub_mode == "subscribe"
        and hub_verify_token == VERIFY_TOKEN
        and hub_challenge is not None
    ):
        return hub_challenge

    raise HTTPException(status_code=403, detail="Token de verificación incorrecto")
