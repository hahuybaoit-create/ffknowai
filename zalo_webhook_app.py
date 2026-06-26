import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from zalo_oa import answer_zalo_message, parse_incoming_message, verify_webhook_secret

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="FF Know AI - Zalo OA Webhook")


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    verify_content = os.getenv("ZALO_VERIFY_META_CONTENT", "").strip()
    verify_meta = ""
    if verify_content:
        verify_meta = (
            '<meta name="zalo-platform-site-verification" '
            f'content="{verify_content}" />'
        )

    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  {verify_meta}
  <title>FF Know AI - Zalo OA Webhook</title>
</head>
<body>
  <h1>FF Know AI - Zalo OA Webhook</h1>
  <p>Status: ok</p>
  <p>Health check: <a href="/healthz">/healthz</a></p>
  <p>Webhook: /zalo/webhook</p>
</body>
</html>"""


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/zalo/oauth/callback", response_class=HTMLResponse)
def zalo_oauth_callback(request: Request) -> str:
    params = dict(request.query_params)
    status = "success" if not params.get("error") else "error"
    rows = "\n".join(
        f"<tr><th>{key}</th><td><code>{value}</code></td></tr>"
        for key, value in params.items()
    )
    if not rows:
        rows = "<tr><td colspan=\"2\">No query parameters received.</td></tr>"

    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Zalo OA Permission Callback</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.5; }}
    table {{ border-collapse: collapse; min-width: 520px; }}
    th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; }}
    th {{ background: #f6f8fa; width: 180px; }}
    .success {{ color: #067d33; }}
    .error {{ color: #b42318; }}
  </style>
</head>
<body>
  <h1>Zalo OA Permission Callback</h1>
  <p>Status: <strong class="{status}">{status}</strong></p>
  <table>{rows}</table>
  <p>Neu co tham so <code>code</code> va/hoac <code>oa_id</code>, buoc cap quyen da redirect ve webhook service.</p>
</body>
</html>"""


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
