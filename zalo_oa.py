import logging
import os
from dataclasses import dataclass
from typing import Any

import requests
from dotenv import load_dotenv

from agent import answer_query

load_dotenv()

LOGGER = logging.getLogger(__name__)
ZALO_SEND_MESSAGE_URL = "https://openapi.zalo.me/v3.0/oa/message/cs"


@dataclass
class ZaloIncomingMessage:
    event_name: str
    user_id: str
    text: str


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip('"').strip("'")


def _access_token() -> str:
    token = _env("ZALO_OA_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("Missing ZALO_OA_ACCESS_TOKEN")
    return token


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


def send_text_message(user_id: str, text: str) -> None:
    headers = {
        "access_token": _access_token(),
        "Content-Type": "application/json",
    }

    for index, part in enumerate(_split_reply(text), start=1):
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
        if data.get("error") not in (None, 0, "0"):
            LOGGER.error("Zalo send message returned API error: %s", data)
            raise RuntimeError(f"Zalo send message returned API error: {data}")
        LOGGER.info("Zalo OA message sent successfully. response=%s", data)


def answer_zalo_message(message: ZaloIncomingMessage) -> str:
    LOGGER.info("Answering Zalo OA message from user_id=%s", message.user_id)
    if not is_user_allowed(message.user_id):
        LOGGER.warning("Rejected unauthorized Zalo user_id=%s", message.user_id)
        answer = _unauthorized_reply()
        send_text_message(message.user_id, answer)
        return answer

    answer_result = answer_query(message.text, include_file_links=True)
    answer = answer_result.text
    LOGGER.info(
        "AI answer generated for Zalo user_id=%s answer_len=%s",
        message.user_id,
        len(answer),
    )
    send_text_message(message.user_id, answer)
    return answer
