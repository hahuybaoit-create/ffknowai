import base64
import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv

from paths import DATA_DIR

load_dotenv()

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


@dataclass(frozen=True)
class FileReference:
    name: str
    relative_path: str
    path: Path
    mime: str
    web_url: str | None = None
    download_url: str | None = None
    last_modified: str | None = None


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip('"').strip("'")


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
    terms: list[str] = []
    for term in _normalize_text(query).replace("_", " ").split():
        term = "".join(char for char in term if char.isalnum())
        if len(term) >= 2 and term not in stop_words and term not in terms:
            terms.append(term)
    return terms


def _is_download_query(query: str) -> bool:
    normalized = _normalize_text(query)
    return any(term in normalized for term in DOWNLOAD_QUERY_TERMS)


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _manifest_by_relative_path() -> dict[str, dict]:
    manifest = _load_manifest()
    items: dict[str, dict] = {}
    for item in manifest.get("files", []):
        for key in ("local_path", "relative_path"):
            value = item.get(key)
            if value:
                items[_normalize_relative_path(value)] = item
    return items


def _normalize_relative_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _public_base_url() -> str:
    return (
        _env("PUBLIC_BASE_URL")
        or _env("APP_PUBLIC_BASE_URL")
        or _env("RENDER_EXTERNAL_URL")
    ).rstrip("/")


def _download_secret() -> str:
    return (
        _env("FILE_DOWNLOAD_SECRET")
        or _env("ADMIN_SYNC_SECRET")
        or _env("ZALO_WEBHOOK_SECRET")
    )


def make_file_key(relative_path: str) -> str:
    raw = _normalize_relative_path(relative_path).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_file_key(file_key: str) -> str:
    padding = "=" * (-len(file_key) % 4)
    return base64.urlsafe_b64decode((file_key + padding).encode("ascii")).decode("utf-8")


def resolve_file_key(file_key: str) -> Path:
    relative_path = decode_file_key(file_key)
    candidate = (DATA_DIR / relative_path).resolve()
    data_root = DATA_DIR.resolve()
    if candidate != data_root and data_root not in candidate.parents:
        raise ValueError("Invalid file path")
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(relative_path)
    if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("Unsupported file type")
    return candidate


def document_files() -> list[Path]:
    if not DATA_DIR.exists():
        return []
    return sorted(
        path
        for path in DATA_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _file_url(relative_path: str) -> str | None:
    base_url = _public_base_url()
    secret = _download_secret()
    if not base_url or not secret:
        return None
    query = urlencode({"secret": secret})
    return f"{base_url}/files/{make_file_key(relative_path)}?{query}"


def _to_file_reference(path: Path, manifest_items: dict[str, dict]) -> FileReference:
    relative_path = _normalize_relative_path(str(path.relative_to(DATA_DIR)))
    manifest_item = manifest_items.get(relative_path) or {}
    return FileReference(
        name=path.name,
        relative_path=relative_path,
        path=path,
        mime=DOWNLOAD_MIME_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        web_url=manifest_item.get("web_url"),
        download_url=_file_url(relative_path),
        last_modified=manifest_item.get("last_modified"),
    )


def manifest_item_for_path(path: str) -> dict:
    manifest_items = _manifest_by_relative_path()
    normalized_path = _normalize_relative_path(path)
    item = manifest_items.get(normalized_path)
    if item:
        return item
    path_name = Path(normalized_path).name
    for relative_path, manifest_item in manifest_items.items():
        if relative_path.endswith(normalized_path) or Path(relative_path).name == path_name:
            return manifest_item
    return {}


def find_related_files(query: str, docs: list | None = None, limit: int = 6) -> list[FileReference]:
    files = document_files()
    if not files:
        return []

    manifest_items = _manifest_by_relative_path()
    terms = [
        term
        for term in _query_terms(query)
        if term not in {"bieu", "mau", "form", "tai", "download", "file"}
    ]
    normalized_query = _normalize_text(query)
    phrase_boosts = [
        "thanh toan",
        "tam ung",
        "nghi phep",
        "cong tac",
        "may tinh",
        "dieu chuyen",
        "cham dut",
        "bo nhiem",
    ]
    relevant_phrases = [phrase for phrase in phrase_boosts if phrase in normalized_query]
    wants_form = any(term in normalized_query for term in ("bieu mau", "mau", "form", "template"))

    source_paths: set[str] = set()
    for doc in docs or []:
        metadata = getattr(doc, "metadata", {}) or {}
        for key in ("relative_path", "sharepoint_relative_path"):
            value = metadata.get(key)
            if value:
                source_paths.add(_normalize_relative_path(str(value)))
        source = metadata.get("source")
        if not source:
            continue
        try:
            source_paths.add(_normalize_relative_path(str(Path(source).relative_to(DATA_DIR))))
        except ValueError:
            source_paths.add(_normalize_relative_path(Path(str(source)).name))

    ranked: list[tuple[int, str, str, Path]] = []
    download_query = _is_download_query(query)
    for path in files:
        relative_path = _normalize_relative_path(str(path.relative_to(DATA_DIR)))
        normalized_path = _normalize_text(relative_path)
        manifest_item = manifest_items.get(relative_path) or {}
        score = 0
        form_like = any(
            term in normalized_path
            for term in ("bieu mau", "template", "mau", "phieu", "don", "de nghi", "de xuat")
        )

        matched_source = relative_path in source_paths or path.name in source_paths
        if not download_query and not matched_source:
            continue
        if wants_form and not form_like:
            continue
        if wants_form and relevant_phrases and not any(phrase in normalized_path for phrase in relevant_phrases):
            continue

        if matched_source:
            score += 20
        if form_like:
            score += 7
        if download_query:
            score += 3
        score += sum(2 for term in terms if term in normalized_path)
        score += sum(8 for phrase in relevant_phrases if phrase in normalized_path)

        if score <= 0:
            continue
        ranked.append((score, manifest_item.get("last_modified", ""), relative_path.lower(), path))

    ranked.sort(key=lambda item: item[2])
    ranked.sort(key=lambda item: item[1], reverse=True)
    ranked.sort(key=lambda item: item[0], reverse=True)
    references: list[FileReference] = []
    seen: set[str] = set()
    for _, _, _, path in ranked:
        relative_path = _normalize_relative_path(str(path.relative_to(DATA_DIR)))
        if relative_path in seen:
            continue
        seen.add(relative_path)
        references.append(_to_file_reference(path, manifest_items))
        if len(references) >= limit:
            break
    return references


def format_file_references(files: list[FileReference], include_links: bool = False) -> str:
    if not files:
        return ""
    lines = []
    for file in files:
        if file.web_url:
            lines.append(f"- {file.name}: {file.web_url}")
        elif include_links and file.download_url:
            lines.append(f"- {file.name}: {file.download_url}")
        else:
            lines.append(f"- {file.name} ({file.relative_path})")
    return "\n".join(lines)


def file_references_to_downloads(files: list[FileReference]) -> list[dict[str, str]]:
    return [
        {
            "name": file.name,
            "path": str(file.path),
            "relative_path": file.relative_path,
            "mime": file.mime,
        }
        for file in files
    ]
