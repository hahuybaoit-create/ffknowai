import base64
import json
import os
import re
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
    "tai lieu",
    "tai lieu tham khao",
    "tham khao",
    "link",
    "duong dan",
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

FORM_SET_DEFINITIONS = {
    "cong_tac": {
        "triggers": ("cong tac", "di cong tac", "qd cong tac", "quyet dinh cong tac"),
        "intro": "Để thực hiện công tác, bạn có thể tham khảo các biểu mẫu sau:",
        "link_intro": "Bạn có thể tham khảo mẫu công tác tại các liên kết sau:",
        "items": (
            {
                "label": "Phiếu kế hoạch công tác",
                "patterns": ("phieu ke hoach cong tac",),
            },
            {
                "label": "Quyết định công tác",
                "patterns": ("mau qd cong tac", "quyet dinh cong tac"),
            },
            {
                "label": "Đề nghị tạm ứng, hoàn ứng, thanh toán",
                "patterns": ("bieu mau tam ung thanh toan", "tam ung thanh toan"),
            },
        ),
    },
    "nghi_viec": {
        "triggers": ("nghi viec", "thoi viec", "cham dut hdld", "bo nghi viec"),
        "intro": "Bộ nghỉ việc gồm các tài liệu sau:",
        "link_intro": "Bạn có thể tham khảo mẫu nghỉ việc tại các liên kết sau:",
        "items": (
            {
                "label": "Đơn xin nghỉ việc",
                "patterns": ("don xin nghi viec",),
            },
            {
                "label": "Quyết định nghỉ việc",
                "patterns": ("quyet dinh cham dut hdld", "quyet dinh cham dut"),
            },
            {
                "label": "Biên bản thanh lý HĐLĐ",
                "patterns": ("bien ban thanh ly hdtv hdld", "bien ban thanh ly"),
            },
            {
                "label": "Xác nhận thôi việc",
                "patterns": ("xac nhan nghi viec", "xac nhan thoi viec"),
            },
        ),
    },
}

SINGLE_FORM_DEFINITIONS = {
    "danh_gia_thu_viec": {
        "triggers": (
            "danh gia thu viec",
            "mau danh gia thu viec",
            "bieu mau danh gia thu viec",
            "gui toi mau danh gia thu viec",
            "gui giup toi mau danh gia thu viec",
            "danh gia nhan su tts hoc viec thu viec dinh ky",
        ),
        "patterns": ("danh gia nhan su tts hoc viec thu viec dinh ky",),
        "intro": "Mẫu đánh giá thử việc theo quy định của Công ty như sau:",
        "form_name": "Đánh giá nhân sự TTS, học việc, thử việc, định kỳ",
    },
    "de_nghi_tam_ung": {
        "triggers": (
            "de nghi tam ung",
            "mau de nghi tam ung",
            "bieu mau de nghi tam ung",
            "gui giup toi mau de nghi tam ung",
        ),
        "patterns": ("bieu mau tam ung thanh toan", "tam ung thanh toan"),
        "intro": "Mẫu đề nghị tạm ứng theo quy định của Công ty như sau:",
        "form_code": "BMTT19.01",
        "form_name": "Đề nghị tạm ứng",
    },
}

DOCUMENT_SHORTCUT_DEFINITIONS = {
    "hoa_hong_ctv": {
        "triggers": (
            "hoa hong ctv",
            "chinh sach hoa hong ctv",
            "tai lieu tham khao chinh sach hoa hong ctv",
            "tai lieu chinh sach hoa hong ctv",
        ),
        "patterns": ("chinh sach hoa hong ctv",),
        "intro": "Tài liệu tham khảo chính sách hoa hồng CTV là:",
        "label": "Chính sách hoa hồng CTV",
        "direct_answer": False,
    },
    "co_che_luong_bu": {
        "triggers": (
            "co che luong",
            "co che tinh luong",
            "luong bu",
            "tinh luong bu",
            "chinh sach luong bu",
        ),
        "patterns": ("co che tinh luong bu",),
        "intro": "Thông tin cơ chế lương BU cần được tra cứu theo tài liệu hiện hành sau:",
        "label": "Cơ chế tính lương BU",
        "direct_answer": True,
    },
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


def _file_reference_url(file: FileReference, include_links: bool = True) -> str | None:
    if file.web_url:
        return file.web_url
    if include_links and file.download_url:
        return file.download_url
    return None


def _display_file_name(file: FileReference) -> str:
    return file.name


def _markdown_file_link(file: FileReference, include_links: bool = True) -> str:
    url = _file_reference_url(file, include_links)
    if url:
        return f"[đây]({url})"
    return file.relative_path


def _plain_file_url(file: FileReference, include_links: bool = True) -> str:
    return _file_reference_url(file, include_links) or file.relative_path


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


def _find_file_by_patterns(patterns: tuple[str, ...], files: list[Path]) -> Path | None:
    best: tuple[int, str, Path] | None = None
    for path in files:
        relative_path = _normalize_text(_normalize_relative_path(str(path.relative_to(DATA_DIR))))
        score = 0
        for pattern in patterns:
            if pattern in relative_path:
                score += 3
                continue
            pattern_terms = [term for term in pattern.split() if len(term) >= 3]
            if pattern_terms and all(term in relative_path for term in pattern_terms):
                score += 2
        if score <= 0:
            continue
        candidate = (score, relative_path, path)
        if best is None or candidate[0] > best[0] or candidate[1] < best[1]:
            best = candidate
    return best[2] if best else None


def _definition_key(query: str, definitions: dict) -> str | None:
    normalized_query = _normalize_text(query)
    for key, definition in definitions.items():
        if any(trigger in normalized_query for trigger in definition["triggers"]):
            return key
    return None


def _form_set_key(query: str) -> str | None:
    normalized_query = _normalize_text(query)
    has_form_signal = any(
        term in normalized_query
        for term in ("bieu mau", "mau", "form", "template", "bo", "tai", "download", "file")
    )
    for key, definition in FORM_SET_DEFINITIONS.items():
        if any(trigger in normalized_query for trigger in definition["triggers"]):
            if has_form_signal or key in {"cong_tac", "nghi_viec"}:
                return key
    return None


def _file_reference_for_patterns(patterns: tuple[str, ...]) -> FileReference | None:
    files = document_files()
    if not files:
        return None
    path = _find_file_by_patterns(patterns, files)
    if not path:
        return None
    return _to_file_reference(path, _manifest_by_relative_path())


def build_single_form_answer(
    query: str,
    include_links: bool = True,
) -> tuple[str, list[FileReference]] | None:
    key = _definition_key(query, SINGLE_FORM_DEFINITIONS)
    if not key:
        return None

    definition = SINGLE_FORM_DEFINITIONS[key]
    file = _file_reference_for_patterns(definition["patterns"])
    if not file:
        return None

    text = "\n".join(
        [line for line in (
            definition["intro"],
            "",
            f"* Mã biểu mẫu: {definition['form_code']}" if definition.get("form_code") else None,
            f"* Tên biểu mẫu: {definition['form_name']}",
            "",
            f"Bạn có thể tham khảo mẫu tại đường dẫn: {_plain_file_url(file, include_links)}",
        ) if line is not None]
    )
    return text, [file]


def build_document_shortcut_answer(
    query: str,
    include_links: bool = True,
    direct_only: bool = True,
) -> tuple[str, list[FileReference]] | None:
    key = _definition_key(query, DOCUMENT_SHORTCUT_DEFINITIONS)
    if not key:
        return None

    definition = DOCUMENT_SHORTCUT_DEFINITIONS[key]
    if direct_only and not definition.get("direct_answer", False):
        return None
    file = _file_reference_for_patterns(definition["patterns"])
    if not file:
        return None

    text = "\n".join(
        [
            definition["intro"],
            "",
            f"* Tài liệu: {definition['label']}",
            f"* File hiện hành: {file.name}",
            "",
            f"Bạn có thể tham khảo tài liệu tại đường dẫn: {_plain_file_url(file, include_links)}",
        ]
    )
    return text, [file]


def _is_document_reference_query(query: str) -> bool:
    normalized_query = _normalize_text(query)
    return any(
        term in normalized_query
        for term in (
            "tai lieu tham khao",
            "tai lieu",
            "link",
            "duong dan",
            "file",
            "nguon",
            "source",
        )
    )


def _business_unit_aliases(query: str) -> tuple[str, ...]:
    normalized = _normalize_text(query)
    compact = "".join(char for char in normalized if char.isalnum())
    if re.search(r"\bbu\s*1\b", normalized) or "bu1" in compact:
        return ("jd bu 1", "bu1")
    if re.search(r"\bbu\s*2\b", normalized) or "bu2" in compact:
        return ("jd bu 2", "bu2")
    if re.search(r"\bbu\s*3\b", normalized) or "bu3" in compact:
        return ("jd bu 3", "bu3")
    if re.search(r"\bbu\s*hcm\b", normalized) or "buhcm" in compact:
        return ("jd bu hcm", "buhcm")
    return ()


def _matches_business_unit(normalized_path: str, aliases: tuple[str, ...]) -> bool:
    if not aliases:
        return True
    compact_path = "".join(char for char in normalized_path if char.isalnum())
    return any(alias in normalized_path or alias.replace(" ", "") in compact_path for alias in aliases)


def _dedupe_file_name_key(path: Path) -> str:
    return "".join(char for char in _normalize_text(path.name) if char.isalnum())


def _preferred_reference_terms(query: str) -> tuple[str, ...]:
    normalized = _normalize_text(query)
    if (
        "quy trinh phoi hop lien phong ban" in normalized
        or ("phoi hop" in normalized and "phong ban" in normalized)
        or ("phoi hop" in normalized and "lien phong" in normalized)
    ):
        return ("quy", "trinh", "phoi", "hop", "lien", "phong", "ban")
    if "nguyen tac huan luyen" in normalized or "huan luyen thuc chien" in normalized:
        return ("nguyen", "tac", "huan", "luyen", "thuc", "chien")
    if (
        "bo khung van hanh" in normalized
        or "phat trien doi tac" in normalized
        or ("van hanh" in normalized and "doi tac" in normalized)
    ):
        return ("bo", "khung", "van", "hanh", "phat", "trien", "doi", "tac")
    return ()


def _matches_preferred_reference(path: Path, terms: tuple[str, ...]) -> bool:
    if not terms:
        return True
    normalized_name = _normalize_text(path.name)
    normalized_path = _normalize_text(_normalize_relative_path(str(path.relative_to(DATA_DIR))))
    if all(term in normalized_name for term in terms):
        return True
    return all(term in normalized_path for term in terms) and path.suffix.lower() == ".pdf"


def find_document_references(query: str, limit: int = 3) -> list[FileReference]:
    if not _is_document_reference_query(query):
        return []

    files = document_files()
    if not files:
        return []

    normalized_query = _normalize_text(query)
    generic_terms = {
        "anh",
        "ban",
        "cho",
        "toi",
        "xin",
        "gui",
        "giup",
        "tai",
        "lieu",
        "tham",
        "khao",
        "link",
        "duong",
        "dan",
        "file",
        "nguon",
        "chinh",
        "sach",
        "quy",
        "dinh",
        "ve",
        "cua",
        "cau",
        "hoi",
        "tiep",
        "theo",
    }
    terms = [term for term in _query_terms(query) if term not in generic_terms]
    phrase_boosts = (
        "hoa hong ctv",
        "chinh sach hoa hong ctv",
        "co che tinh luong bu",
        "co che luong bu",
        "nhan vien mua hang",
        "mua hang",
        "quy trinh phoi hop lien phong ban",
        "phoi hop lien phong ban",
        "phoi hop phong ban",
        "nguyen tac huan luyen",
        "huan luyen thuc chien",
        "bo khung van hanh",
        "phat trien doi tac",
        "tam ung thanh toan",
        "cong tac",
        "nghi viec",
    )
    relevant_phrases = [phrase for phrase in phrase_boosts if phrase in normalized_query]
    business_unit_aliases = _business_unit_aliases(query)
    preferred_reference_terms = _preferred_reference_terms(query)

    ranked: list[tuple[int, str, str, Path]] = []
    manifest_items = _manifest_by_relative_path()
    for path in files:
        relative_path = _normalize_relative_path(str(path.relative_to(DATA_DIR)))
        normalized_path = _normalize_text(relative_path)
        if business_unit_aliases and not _matches_business_unit(normalized_path, business_unit_aliases):
            continue
        if preferred_reference_terms and not _matches_preferred_reference(path, preferred_reference_terms):
            continue

        manifest_item = manifest_items.get(relative_path) or {}
        score = sum(3 for term in terms if term in normalized_path)
        score += sum(15 for phrase in relevant_phrases if phrase in normalized_path)
        if business_unit_aliases:
            score += 40
        if preferred_reference_terms:
            score += 50

        if "ctv" in normalized_query and "hoa hong" in normalized_query:
            if "hoa hong ctv" not in normalized_path:
                continue
            score += 30

        if score <= 0:
            continue
        ranked.append((score, manifest_item.get("last_modified", ""), relative_path.lower(), path))

    if not ranked:
        return []

    ranked.sort(key=lambda item: item[2])
    ranked.sort(key=lambda item: item[1], reverse=True)
    ranked.sort(key=lambda item: item[0], reverse=True)
    top_score = ranked[0][0]
    references: list[FileReference] = []
    seen: set[str] = set()
    seen_file_names: set[str] = set()
    for score, _, _, path in ranked:
        if score < max(1, top_score - 10):
            continue
        relative_path = _normalize_relative_path(str(path.relative_to(DATA_DIR)))
        if relative_path in seen:
            continue
        file_name_key = _dedupe_file_name_key(path)
        if file_name_key in seen_file_names:
            continue
        seen.add(relative_path)
        seen_file_names.add(file_name_key)
        references.append(_to_file_reference(path, manifest_items))
        if len(references) >= limit:
            break
    return references


def build_document_reference_answer(
    query: str,
    include_links: bool = True,
) -> tuple[str, list[FileReference]] | None:
    if not _is_document_reference_query(query):
        return None

    document_shortcut = build_document_shortcut_answer(query, include_links, direct_only=False)
    if document_shortcut:
        return document_shortcut

    references = find_document_references(query)
    if not references:
        return None

    lines = ["Bạn có thể tham khảo tài liệu tại đường dẫn sau:"]
    for file in references:
        lines.append(f"- {file.name}: {_plain_file_url(file, include_links)}")
    return "\n".join(lines), references


def find_form_set_files(query: str) -> list[tuple[str, FileReference]]:
    key = _form_set_key(query)
    if not key:
        return []

    files = document_files()
    if not files:
        return []

    manifest_items = _manifest_by_relative_path()
    matched: list[tuple[str, FileReference]] = []
    for item in FORM_SET_DEFINITIONS[key]["items"]:
        path = _find_file_by_patterns(item["patterns"], files)
        if path:
            matched.append((item["label"], _to_file_reference(path, manifest_items)))
    return matched


def build_form_set_answer(
    query: str,
    include_links: bool = True,
) -> tuple[str, list[FileReference]] | None:
    key = _form_set_key(query)
    if not key:
        return None

    definition = FORM_SET_DEFINITIONS[key]
    matched = find_form_set_files(query)
    if not matched:
        return None

    files = [file for _, file in matched]
    file_by_label = {label: file for label, file in matched}
    lines = [definition["intro"], ""]

    for item in definition["items"]:
        file = file_by_label.get(item["label"])
        if file:
            lines.append(f"* {item['label']}: **{_display_file_name(file)}**")
        else:
            lines.append(f"* {item['label']}")

    lines.extend(["", definition["link_intro"]])
    for index, (label, file) in enumerate(matched, start=1):
        lines.append(f"{index}. {label}: tại {_markdown_file_link(file, include_links)}")

    return "\n".join(lines), files


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
        url = _file_reference_url(file, include_links)
        if url:
            lines.append(f"- Bạn có thể tham khảo mẫu **{file.name}** tại [đây]({url})")
        else:
            lines.append(f"- Bạn có thể tham khảo mẫu **{file.name}** tại `{file.relative_path}`")
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
