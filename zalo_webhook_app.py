import logging
import os
import time
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from zalo_oa import answer_zalo_message, get_token_status, parse_incoming_message, verify_webhook_secret
from document_files import DOWNLOAD_MIME_TYPES, resolve_file_key
from paths import CHROMA_DB_DIR, DATA_DIR

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="FF Know AI - Zalo OA Webhook")
APP_CODE_VERSION = "attendance-fallback-2026-07-14"
SYNC_STATUS: dict[str, Any] = {
    "state": "idle",
    "started_at": None,
    "finished_at": None,
    "error": None,
}


def _admin_secret_ok(secret: str | None) -> bool:
    expected_secret = os.getenv("ADMIN_SYNC_SECRET") or os.getenv("ZALO_WEBHOOK_SECRET")
    if not expected_secret:
        return False
    return bool(secret) and secret == expected_secret


def _file_download_secret_ok(secret: str | None) -> bool:
    expected_secret = (
        os.getenv("FILE_DOWNLOAD_SECRET")
        or os.getenv("ADMIN_SYNC_SECRET")
        or os.getenv("ZALO_WEBHOOK_SECRET")
    )
    if not expected_secret:
        return False
    return bool(secret) and secret == expected_secret


def _sync_documents_background(force_index: bool = True, skip_download: bool = False) -> None:
    SYNC_STATUS.update(
        {
            "state": "running",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "finished_at": None,
            "error": None,
        }
    )
    try:
        from sync_documents import sync_documents

        sync_documents(force_index=force_index, skip_download=skip_download)
    except Exception as exc:
        LOGGER.exception("Admin sync failed")
        SYNC_STATUS.update(
            {
                "state": "failed",
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "error": str(exc),
            }
        )
        return

    SYNC_STATUS.update(
        {
            "state": "done",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": None,
        }
    )


def _answer_message_background(message: Any) -> None:
    try:
        answer_zalo_message(message)
    except Exception:
        LOGGER.exception("Failed to process Zalo OA message in background")


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


@app.get("/debugz")
def debugz(
    secret: str | None = Query(default=None),
) -> dict[str, Any]:
    if not verify_webhook_secret(secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    return {
        "status": "ok",
        "code_version": APP_CODE_VERSION,
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "chroma_db_dir": str(CHROMA_DB_DIR),
        "chroma_db_exists": CHROMA_DB_DIR.exists(),
        "chroma_db_files": sorted(path.name for path in CHROMA_DB_DIR.iterdir())
        if CHROMA_DB_DIR.exists()
        else [],
        "zalo_token_set": bool(os.getenv("ZALO_OA_ACCESS_TOKEN")),
        "zalo_token_status": get_token_status(),
        "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")),
    }


@app.get("/files/{file_key}")
def download_file(
    file_key: str,
    secret: str | None = Query(default=None),
) -> FileResponse:
    if not _file_download_secret_ok(secret):
        raise HTTPException(status_code=401, detail="Invalid file secret")
    try:
        file_path = resolve_file_key(file_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=DOWNLOAD_MIME_TYPES.get(
            file_path.suffix.lower(),
            "application/octet-stream",
        ),
    )


@app.get("/admin/sync")
def admin_sync(
    background_tasks: BackgroundTasks,
    secret: str | None = Query(default=None),
    skip_download: bool = Query(default=False),
) -> dict[str, Any]:
    if not _admin_secret_ok(secret):
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    if SYNC_STATUS.get("state") == "running":
        return {"status": "already_running", "sync": SYNC_STATUS}

    background_tasks.add_task(
        _sync_documents_background,
        True,
        skip_download,
    )
    return {"status": "accepted", "sync": SYNC_STATUS}


@app.get("/admin/sync/status")
def admin_sync_status(
    secret: str | None = Query(default=None),
) -> dict[str, Any]:
    if not _admin_secret_ok(secret):
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    data_ready = DATA_DIR.exists() and CHROMA_DB_DIR.exists()
    return {
        "status": "ok",
        "sync": SYNC_STATUS,
        "data_dir_exists": DATA_DIR.exists(),
        "chroma_db_exists": CHROMA_DB_DIR.exists(),
        "data_dir": str(DATA_DIR),
        "chroma_db_dir": str(CHROMA_DB_DIR),
        "next_action": None
        if data_ready or SYNC_STATUS.get("state") == "running"
        else "Open /admin/sync?secret=<ADMIN_SYNC_SECRET> to start SharePoint sync and build Vector DB.",
    }


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
    background_tasks: BackgroundTasks,
    secret: str | None = Query(default=None),
) -> dict[str, Any]:
    if not verify_webhook_secret(secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    payload = await request.json()
    message = parse_incoming_message(payload)
    if message is None:
        LOGGER.info("Ignored unsupported Zalo OA webhook payload: %s", payload)
        return {"status": "ignored"}

    os.environ.setdefault("PUBLIC_BASE_URL", str(request.base_url).rstrip("/"))

    LOGGER.info(
        "Accepted Zalo OA webhook event_name=%s user_id=%s text_len=%s",
        message.event_name,
        message.user_id,
        len(message.text),
    )
    background_tasks.add_task(_answer_message_background, message)
    return {"status": "accepted"}
