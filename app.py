import json
import os
import time
import unicodedata
from pathlib import Path

import streamlit as st

from agent import answer_query
from document_files import file_references_to_downloads
from paths import CHROMA_DB_DIR, DATA_DIR

BASE_DIR = Path(__file__).resolve().parent
LOGO_PATH = BASE_DIR / "logo.png"
INDEX_STATE_PATH = CHROMA_DB_DIR / "_source_manifest.json"
INDEX_SCHEMA_VERSION = 3
MANIFEST_PATH = DATA_DIR / "_sharepoint_manifest.json"
SUPPORTED_SUFFIXES = {
    ".doc",
    ".docx",
    ".pdf",
    ".ppt",
    ".pptm",
    ".pptx",
    ".txt",
    ".xls",
    ".xlsx",
}
DOWNLOAD_QUERY_TERMS = {
    "bieu mau",
    "mau",
    "form",
    "tai",
    "download",
    "file",
    "don",
    "phieu",
}
DOWNLOAD_MIME_TYPES = {
    ".doc": "application/msword",
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
}


def _document_files() -> list[Path]:
    if not DATA_DIR.exists():
        return []
    return sorted(
        path
        for path in DATA_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _index_matches_sources() -> bool:
    manifest = _load_json(MANIFEST_PATH)
    index_state = _load_json(INDEX_STATE_PATH)
    return (
        bool(manifest)
        and index_state.get("index_schema_version") == INDEX_SCHEMA_VERSION
        and index_state.get("files") == manifest.get("files", [])
    )


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return text.replace("đ", "d")


def _query_terms(query: str) -> list[str]:
    stop_words = {
        "anh",
        "ban",
        "can",
        "cho",
        "cua",
        "duoc",
        "giup",
        "hay",
        "la",
        "minh",
        "mot",
        "nay",
        "toi",
        "ve",
        "xin",
    }
    terms = []
    for term in _normalize_text(query).replace("_", " ").split():
        term = "".join(char for char in term if char.isalnum())
        if len(term) >= 2 and term not in stop_words and term not in terms:
            terms.append(term)
    return terms


def _is_download_query(query: str) -> bool:
    normalized = _normalize_text(query)
    return any(term in normalized for term in DOWNLOAD_QUERY_TERMS)


def _download_matches(query: str, limit: int = 10) -> list[dict[str, str]]:
    if not _is_download_query(query):
        return []

    files = _document_files()
    if not files:
        return []

    normalized_query = _normalize_text(query)
    terms = [
        term
        for term in _query_terms(query)
        if term not in {"bieu", "mau", "form", "tai", "download", "file"}
    ]

    ranked: list[tuple[int, str, Path]] = []
    for path in files:
        relative_path = str(path.relative_to(DATA_DIR))
        normalized_path = _normalize_text(relative_path)
        in_form_folder = "bieu mau" in normalized_path or "biểu mẫu" in relative_path.lower()
        score = 0
        if in_form_folder:
            score += 5
        if "bieu mau" in normalized_query or "form" in normalized_query:
            score += 3 if in_form_folder else 0
        score += sum(2 for term in terms if term in normalized_path)
        if score == 0 and terms:
            continue
        if score == 0 and not in_form_folder:
            continue
        ranked.append((score, relative_path.lower(), path))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    matches = []
    for _, _, path in ranked[:limit]:
        matches.append(
            {
                "name": path.name,
                "path": str(path),
                "relative_path": str(path.relative_to(DATA_DIR)),
                "mime": DOWNLOAD_MIME_TYPES.get(path.suffix.lower(), "application/octet-stream"),
            }
        )
    return matches


def _render_downloads(downloads: list[dict[str, str]], key_prefix: str) -> None:
    if not downloads:
        return

    st.markdown("**Tệp có thể tải:**")
    for index, item in enumerate(downloads):
        path = Path(item["path"])
        if not path.exists():
            st.caption(f"Không tìm thấy file: {item['relative_path']}")
            continue
        with path.open("rb") as file:
            st.download_button(
                label=f"Tải {item['name']}",
                data=file,
                file_name=item["name"],
                mime=item["mime"],
                key=f"{key_prefix}_{index}_{item['relative_path']}",
            )
        st.caption(item["relative_path"])


def _maybe_auto_sync() -> None:
    if os.getenv("AUTO_SYNC_ON_START", "false").lower() not in {"1", "true", "yes"}:
        return

    interval_minutes = int(os.getenv("AUTO_SYNC_INTERVAL_MINUTES", "60"))
    interval_seconds = max(interval_minutes, 1) * 60
    now = time.time()
    last_sync = st.session_state.get("last_auto_sync_at", 0)
    if now - last_sync < interval_seconds:
        return

    st.session_state["last_auto_sync_at"] = now
    with st.spinner("Đang tự cập nhật tài liệu từ SharePoint..."):
        from sync_documents import sync_documents

        try:
            sync_documents()
        except Exception as exc:
            st.warning(f"Chưa tự cập nhật được tài liệu từ SharePoint: {exc}")


st.set_page_config(page_title="FF - Know AI", page_icon="🤖", layout="wide")


def _require_app_password() -> None:
    expected_password = os.getenv("APP_ACCESS_PASSWORD", "").strip().strip('"').strip("'")
    if not expected_password or st.session_state.get("app_access_granted"):
        return

    st.title("FF - Know AI")
    password = st.text_input("Nhập mật khẩu truy cập", type="password")
    if st.button("Đăng nhập"):
        if password == expected_password:
            st.session_state["app_access_granted"] = True
            st.rerun()
        st.error("Mật khẩu không đúng hoặc tài khoản chưa được cấp quyền.")
    st.stop()


_require_app_password()

_maybe_auto_sync()

if LOGO_PATH.exists():
    logo_col, title_col = st.columns([1, 7])
    with logo_col:
        st.image(str(LOGO_PATH), width=120)
    with title_col:
        st.title("🤖 Trợ lý AI tra cứu tài liệu Công ty Flexfit")
else:
    st.title("🤖 Trợ lý AI tra cứu tài liệu Công ty Flexfit")

st.markdown(
    """
Chào mừng bạn đến với AI Agent nội bộ!
Bạn có thể hỏi các thông tin liên quan đến chính sách, quy định hoặc tài liệu
đang lưu trong bộ tài liệu nội bộ của Flexfit.
"""
)

if not CHROMA_DB_DIR.exists():
    if DATA_DIR.exists() and any(DATA_DIR.iterdir()):
        st.warning(
            "Đã có tài liệu SharePoint nhưng chưa có Vector DB. "
            "Vui lòng liên hệ quản trị viên để hoàn tất bước lập chỉ mục."
        )
    else:
        st.error(
            "🚨 Cảnh báo: Chưa tìm thấy dữ liệu Vector DB. "
            "Vui lòng liên hệ quản trị viên để cấu hình dữ liệu."
        )
    st.stop()

if not DATA_DIR.exists():
    st.error(
        f"🚨 Cảnh báo: Thư mục nguồn `{DATA_DIR}` không tồn tại. "
        "Vui lòng kiểm tra lại bước tải tài liệu."
    )
    st.stop()

document_files = _document_files()
if not document_files:
    st.error(
        f"🚨 Cảnh báo: Thư mục nguồn `{DATA_DIR}` hiện đang trống. "
        "Vui lòng kiểm tra lại bước tải tài liệu."
    )
    st.stop()

index_is_current = _index_matches_sources()

if "messages" not in st.session_state:
    st.session_state["messages"] = []


def _last_user_context() -> str | None:
    for message in reversed(st.session_state["messages"]):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return None

for message in st.session_state["messages"]:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message["role"] == "assistant":
            _render_downloads(message.get("downloads", []), f"history_{id(message)}")

if prompt := st.chat_input("Hãy nhập câu hỏi của bạn, ví dụ: Quy định nghỉ phép năm là gì?"):
    conversation_context = _last_user_context()
    st.session_state["messages"].append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Đang tra cứu tài liệu..."):
            answer_result = answer_query(prompt, conversation_context=conversation_context)
            response = answer_result.text
            st.markdown(response)
            downloads = file_references_to_downloads(answer_result.files) or _download_matches(prompt)
            _render_downloads(downloads, f"current_{len(st.session_state['messages'])}")

    st.session_state["messages"].append(
        {"role": "assistant", "content": response, "downloads": downloads}
    )

with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=180)

    st.header("Chat")
    if st.button("Xóa lịch sử Chat"):
        st.session_state["messages"] = []
        st.rerun()

    st.markdown("---")
    st.markdown("### Trạng thái dữ liệu")
    st.markdown(f"- Số file tài liệu: `{len(document_files)}`")
    index_status = "Có" if index_is_current else "Chưa"
    st.markdown(f"- Vector DB khớp nguồn: `{index_status}`")
    if MANIFEST_PATH.exists():
        st.markdown("- Nguồn: `SharePoint manifest đã sẵn sàng`")
