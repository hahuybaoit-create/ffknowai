import logging
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from agent import _resolve_followup_query, answer_query
from paths import APP_DATA_ROOT

load_dotenv()

LOGGER = logging.getLogger(__name__)
ZALO_SEND_MESSAGE_URL = "https://openapi.zalo.me/v3.0/oa/message/cs"
ZALO_REFRESH_TOKEN_URL = "https://oauth.zaloapp.com/v4/oa/access_token"
ZALO_TOKEN_EXPIRED_CODES = {-216, "-216"}
ZALO_TOKEN_CACHE_FILE = Path(
    os.getenv("ZALO_TOKEN_CACHE_FILE", str(APP_DATA_ROOT / "zalo_tokens.json"))
).resolve()
USER_LAST_QUESTIONS: dict[str, str] = {}


@dataclass
class ZaloIncomingMessage:
    event_name: str
    user_id: str
    text: str


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip('"').strip("'")


def _access_token() -> str:
    token = _env("ZALO_OA_ACCESS_TOKEN") or _token_cache().get("access_token", "")
    if not token:
        raise RuntimeError("Missing ZALO_OA_ACCESS_TOKEN")
    return token


def _token_cache() -> dict[str, Any]:
    try:
        if not ZALO_TOKEN_CACHE_FILE.exists():
            return {}
        with ZALO_TOKEN_CACHE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception:
        LOGGER.warning("Could not read Zalo token cache at %s", ZALO_TOKEN_CACHE_FILE)
        return {}


def _save_token_cache(data: dict[str, Any]) -> None:
    try:
        ZALO_TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with ZALO_TOKEN_CACHE_FILE.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=True, indent=2)
    except Exception:
        LOGGER.warning("Could not write Zalo token cache at %s", ZALO_TOKEN_CACHE_FILE)


def _refresh_token() -> str:
    refresh_token = _env("ZALO_OA_REFRESH_TOKEN") or _token_cache().get("refresh_token", "")
    app_id = _env("ZALO_APP_ID")
    app_secret = _env("ZALO_APP_SECRET")
    if not refresh_token:
        raise RuntimeError(
            "Zalo access token expired and ZALO_OA_REFRESH_TOKEN is not configured"
        )
    if not app_id or not app_secret:
        raise RuntimeError("ZALO_APP_ID and ZALO_APP_SECRET are required to refresh Zalo token")

    response = requests.post(
        ZALO_REFRESH_TOKEN_URL,
        headers={
            "secret_key": app_secret,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "app_id": app_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError:
        LOGGER.error(
            "Zalo refresh token failed. status=%s body=%s",
            response.status_code,
            response.text,
        )
        raise

    data = response.json()
    if data.get("error") not in (None, 0, "0"):
        LOGGER.error("Zalo refresh token returned API error: %s", data)
        raise RuntimeError(f"Zalo refresh token returned API error: {data}")

    access_token = str(data.get("access_token") or "")
    new_refresh_token = str(data.get("refresh_token") or refresh_token)
    if not access_token:
        raise RuntimeError(f"Zalo refresh token response missing access_token: {data}")

    os.environ["ZALO_OA_ACCESS_TOKEN"] = access_token
    os.environ["ZALO_OA_REFRESH_TOKEN"] = new_refresh_token
    _save_token_cache(
        {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "expires_in": data.get("expires_in"),
            "refreshed_at": int(time.time()),
        }
    )
    LOGGER.info("Zalo OA access token refreshed successfully")
    return access_token


def get_token_status() -> dict[str, Any]:
    cache = _token_cache()
    return {
        "access_token_set": bool(_env("ZALO_OA_ACCESS_TOKEN")),
        "refresh_token_set": bool(_env("ZALO_OA_REFRESH_TOKEN")),
        "app_id_set": bool(_env("ZALO_APP_ID")),
        "app_secret_set": bool(_env("ZALO_APP_SECRET")),
        "token_cache_exists": ZALO_TOKEN_CACHE_FILE.exists(),
        "token_cache_has_access_token": bool(cache.get("access_token")),
        "token_cache_has_refresh_token": bool(cache.get("refresh_token")),
        "token_cache_refreshed_at": cache.get("refreshed_at"),
    }


def _max_reply_chars() -> int:
    try:
        return max(200, int(_env("ZALO_MAX_REPLY_CHARS", "1800")))
    except ValueError:
        return 1800


def _allowed_user_ids() -> set[str]:
    return {
        user_id.strip()
        for user_id in _env("ZALO_ALLOWED_USER_IDS").split(",")
        if user_id.strip()
    }


def is_user_allowed(user_id: str) -> bool:
    allowed_user_ids = _allowed_user_ids()
    return not allowed_user_ids or user_id in allowed_user_ids


def _unauthorized_reply() -> str:
    return _env(
        "ZALO_UNAUTHORIZED_REPLY",
        "Tài khoản Zalo này chưa được cấp quyền sử dụng Flexfit - Know AI. "
        "Vui lòng liên hệ quản trị viên để được hỗ trợ.",
    )


def verify_webhook_secret(received_secret: str | None) -> bool:
    expected_secret = _env("ZALO_WEBHOOK_SECRET")
    if not expected_secret:
        return True
    return bool(received_secret) and received_secret == expected_secret


def parse_incoming_message(payload: dict[str, Any]) -> ZaloIncomingMessage | None:
    event_name = str(payload.get("event_name") or "")
    message = payload.get("message") or {}
    sender = payload.get("sender") or {}

    user_id = str(
        sender.get("id")
        or sender.get("user_id")
        or payload.get("user_id")
        or payload.get("user_id_by_app")
        or ""
    )
    text = str(
        message.get("text")
        or message.get("content")
        or payload.get("text")
        or payload.get("content")
        or ""
    ).strip()

    if not user_id or not text:
        LOGGER.info(
            "Ignored Zalo payload because user_id/text is missing. event_name=%s keys=%s",
            event_name,
            sorted(payload.keys()),
        )
        return None

    allowed_events = {
        event.strip().lower()
        for event in _env("ZALO_ALLOWED_EVENTS", "user_send_text").split(",")
        if event.strip()
    }
    if allowed_events and event_name and event_name.lower() not in allowed_events:
        LOGGER.info("Ignored Zalo event_name=%s. allowed_events=%s", event_name, allowed_events)
        return None

    return ZaloIncomingMessage(event_name=event_name, user_id=user_id, text=text)


def _split_reply(text: str) -> list[str]:
    limit = _max_reply_chars()
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        chunk = remaining[:limit]
        split_at = max(chunk.rfind("\n\n"), chunk.rfind("\n"), chunk.rfind(". "))
        if split_at < limit // 2:
            split_at = limit
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return [part for part in parts if part]


def _send_text_part(user_id: str, part: str, index: int, access_token: str) -> None:
    headers = {
        "access_token": access_token,
        "Content-Type": "application/json",
    }

    payload = {
        "recipient": {"user_id": user_id},
        "message": {"text": part},
    }
    LOGGER.info(
        "Sending Zalo OA message part=%s user_id=%s text_len=%s",
        index,
        user_id,
        len(part),
    )
    response = requests.post(
        ZALO_SEND_MESSAGE_URL,
        headers=headers,
        json=payload,
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError:
        LOGGER.error(
            "Zalo send message failed. status=%s body=%s",
            response.status_code,
            response.text,
        )
        raise
    try:
        data = response.json()
    except ValueError:
        data = {}
    if data.get("error") in ZALO_TOKEN_EXPIRED_CODES:
        raise RuntimeError("Zalo access token expired")
    if data.get("error") not in (None, 0, "0"):
        LOGGER.error("Zalo send message returned API error: %s", data)
        raise RuntimeError(f"Zalo send message returned API error: {data}")
    LOGGER.info("Zalo OA message sent successfully. response=%s", data)


def send_text_message(user_id: str, text: str) -> None:
    access_token = _access_token()

    for index, part in enumerate(_split_reply(text), start=1):
        try:
            _send_text_part(user_id, part, index, access_token)
        except RuntimeError as exc:
            if "access token expired" not in str(exc).lower():
                raise
            LOGGER.warning("Zalo access token expired. Refreshing token and retrying once.")
            access_token = _refresh_token()
            _send_text_part(user_id, part, index, access_token)


def answer_zalo_message(message: ZaloIncomingMessage) -> str:
    LOGGER.info("Answering Zalo OA message from user_id=%s", message.user_id)
    if not is_user_allowed(message.user_id):
        LOGGER.warning("Rejected unauthorized Zalo user_id=%s", message.user_id)
        answer = _unauthorized_reply()
        send_text_message(message.user_id, answer)
        return answer

    conversation_context = USER_LAST_QUESTIONS.get(message.user_id)
    effective_query = _resolve_followup_query(message.text, conversation_context)
    answer_result = answer_query(
        effective_query,
        include_file_links=True,
    )
    USER_LAST_QUESTIONS[message.user_id] = effective_query
    answer = answer_result.text
    LOGGER.info(
        "AI answer generated for Zalo user_id=%s answer_len=%s",
        message.user_id,
        len(answer),
    )
    send_text_message(message.user_id, answer)
    return answer
