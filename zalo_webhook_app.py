import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from zalo_oa import answer_zalo_message, parse_incoming_message, verify_webhook_secret

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="FF Know AI - Zalo OA Webhook")


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "FF Know AI - Zalo OA Webhook",
        "status": "ok",
        "health_check": "/healthz",
        "webhook": "/zalo/webhook",
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/zalo/webhook")
def zalo_webhook_check(
    secret: str | None = Query(default=None),
) -> dict[str, str]:
    if not verify_webhook_secret(secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    return {"status": "ok"}


@app.post("/zalo/webhook")
async def zalo_webhook(
    request: Request,
    secret: str | None = Query(default=None),
) -> dict[str, Any]:
    if not verify_webhook_secret(secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    payload = await request.json()
    message = parse_incoming_message(payload)
    if message is None:
        LOGGER.info("Ignored unsupported Zalo OA webhook payload: %s", payload)
        return {"status": "ignored"}

    try:
        answer_zalo_message(message)
    except Exception as exc:
        LOGGER.exception("Failed to process Zalo OA webhook")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "ok"}
