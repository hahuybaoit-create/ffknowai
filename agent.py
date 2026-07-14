import os
import re
import io
import json
import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

from chromadb.config import Settings
from dotenv import load_dotenv
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, TextLoader
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from pypdf import PdfReader
from document_files import (
    FileReference,
    build_document_reference_answer,
    build_document_shortcut_answer,
    build_form_set_answer,
    build_single_form_answer,
    document_files,
    find_related_files,
    format_file_references,
    manifest_item_for_path,
)
from paths import APP_DATA_ROOT, CHROMA_DB_DIR, DATA_DIR

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHROMA_DB_DIR = str(CHROMA_DB_DIR)
OCR_CACHE_DIR = APP_DATA_ROOT / "ocr_cache"


@dataclass
class AgentAnswer:
    text: str
    files: list[FileReference]

ANSWER_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """
Bạn là trợ lý AI nội bộ của Flexfit, chỉ trả lời dựa trên trích đoạn tài
liệu SharePoint được cung cấp trong mỗi câu hỏi.

Luật bắt buộc:
- Không dùng kiến thức chung, internet, kinh nghiệm cá nhân, mẫu email/mẫu đơn
  chung, hoặc giả định ngoài tài liệu.
- Phân tích đúng ý định câu hỏi và chỉ trả lời trong phạm vi ý định đó.
- Trả lời trực tiếp trọng tâm trước; chỉ bổ sung chi tiết liên quan khi thật cần.
- Không viết lan man, không đưa thông tin ngoài phạm vi câu hỏi.
- Không viết các câu kiểu "thông thường", "phổ biến", "tùy công ty", trừ khi chính tài liệu nói như vậy.
- Nếu tài liệu không đủ căn cứ, chỉ trả lời:
  "Không tìm thấy tài liệu phù hợp trên SharePoint."
- Mỗi ý quan trọng phải có trích dẫn [Nguồn: tên_file, trang X]. Nếu nguồn
  không có số trang, dùng [Nguồn: tên_file].
- Khi context có nhiều tài liệu phù hợp, ưu tiên tài liệu có ngày cập nhật mới nhất.
- Nếu câu hỏi hỏi về quy trình, biểu mẫu, template, form hoặc file tải về, phải nêu đúng tài liệu/biểu mẫu và đường dẫn SharePoint nếu có trong context.
- Nếu câu hỏi yêu cầu danh sách có số lượng cụ thể, ví dụ "10 chiêu thức",
  và context có đủ các mục, phải liệt kê đủ đúng số mục theo tài liệu, không
  tự ý rút gọn bỏ mục.
- Trả lời tiếng Việt, ngắn gọn, bám sát nguyên văn tài liệu. Cấu trúc mặc định:
  1) Câu trả lời chính.
  2) Chi tiết cần biết, nếu có.
  3) Link mẫu/tài liệu theo câu: "Bạn có thể tham khảo mẫu ... tại [đây]".
""",
        ),
        (
            "human",
            """
CONTEXT DUY NHẤT ĐƯỢC PHÉP SỬ DỤNG:
{context}

CÂU HỎI:
{question}

Hãy trả lời chỉ từ context trên. Nếu context chỉ có hướng dẫn thao tác, chỉ trả
lời hướng dẫn thao tác. Nếu context không có quy định chi tiết hoặc không khớp
câu hỏi, trả lời đúng câu: "Không tìm thấy tài liệu phù hợp trên SharePoint."
""",
        ),
    ]
)

NO_SHAREPOINT_MATCH = "Không tìm thấy tài liệu phù hợp trên SharePoint."
MISSING_SYSTEM_INFO_MESSAGE = (
    "Thông tin này hiện chưa có trong hệ thống, "
    "FlexFit sẽ cập nhật và hỗ trợ bạn sớm."
)
OUT_OF_SCOPE_MESSAGE = (
    "FF Know AI được hỗ trợ để tra cứu thông tin nội bộ của FlexFit, "
    "vui lòng đặt câu hỏi liên quan khác."
)


def _gemini_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'")


def _source_label(doc: Document) -> str:
    source = doc.metadata.get("source") or doc.metadata.get("file_path") or "Không rõ nguồn"
    filename = os.path.basename(str(source))
    category = doc.metadata.get("source_category")
    if category:
        filename = f"{category}/{filename}"
    page = doc.metadata.get("page")
    if page is None:
        return filename

    try:
        return f"{filename}, trang {int(page) + 1}"
    except (TypeError, ValueError):
        return f"{filename}, trang {page}"


def _format_context(docs: Iterable[Document]) -> str:
    formatted = []
    for index, doc in enumerate(docs, start=1):
        content = " ".join(doc.page_content.split())
        if not content:
            continue
        updated = doc.metadata.get("last_modified") or "không rõ"
        web_url = doc.metadata.get("web_url") or ""
        source_line = f"[Đoạn {index} | Nguồn: {_source_label(doc)} | Cập nhật: {updated}"
        if web_url:
            source_line += f" | SharePoint: {web_url}"
        source_line += "]"
        formatted.append(f"{source_line}\n{content}")
    return "\n\n".join(formatted)


def _unique_sources(docs: Iterable[Document]) -> list[str]:
    sources: list[str] = []
    for doc in docs:
        label = _source_label(doc)
        if label not in sources:
            sources.append(label)
    return sources


def _unique_source_links(docs: Iterable[Document], limit: int = 3) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for doc in docs:
        metadata = doc.metadata or {}
        url = metadata.get("web_url")
        if not url:
            manifest_item = manifest_item_for_path(
                str(
                    metadata.get("relative_path")
                    or metadata.get("sharepoint_relative_path")
                    or metadata.get("source")
                    or metadata.get("file_name")
                    or ""
                )
            )
            url = manifest_item.get("web_url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        links.append((_source_label(doc).split(", trang", 1)[0], str(url)))
        if len(links) >= limit:
            break
    return links


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return text.replace("đ", "d")


def _query_terms(query: str) -> list[str]:
    normalized = _normalize_text(query)
    stop_words = {
        "nhu",
        "the",
        "nao",
        "la",
        "gi",
        "co",
        "cua",
        "cho",
        "toi",
        "ve",
        "quy",
        "dinh",
        "chinh",
        "sach",
        "nam",
    }
    terms = []
    for term in re.findall(r"[a-z0-9]+", normalized):
        if len(term) >= 3 and term not in stop_words and term not in terms:
            terms.append(term)
    return terms


def _intent(query: str) -> str:
    normalized = _normalize_text(query)
    if any(term in normalized for term in ("tai lieu tham khao", "link tai lieu", "duong dan", "nguon tai lieu")):
        return "document_reference"
    if any(
        term in normalized
        for term in (
            "bieu mau",
            "mau don",
            "template",
            "form",
            "phieu",
            "don xin",
            "tai file",
            "download",
            "bo nghi viec",
            "mau cong tac",
        )
    ):
        return "form"
    if any(term in normalized for term in ("quy trinh", "huong dan", "cac buoc", "thao tac", "lam the nao", "nhu the nao")):
        return "process"
    if any(
        term in normalized
        for term in (
            "quy dinh",
            "chinh sach",
            "quy che",
            "noi quy",
            "che do",
            "di muon",
            "di tre",
            "vao muon",
            "vao tre",
            "phat",
            "cham cong",
            "tinh luong",
        )
    ):
        return "policy"
    return "general"


def _is_out_of_scope_query(query: str) -> bool:
    normalized = _normalize_text(query)
    allowed_lookup_terms = (
        "quy trinh",
        "quy dinh",
        "chinh sach",
        "bieu mau",
        "mau",
        "form",
        "template",
        "tai lieu",
        "link",
        "duong dan",
        "ho so",
        "bo nghi viec",
        "don xin nghi viec",
        "di muon",
        "di tre",
        "vao muon",
        "vao tre",
        "phat",
        "cham cong",
        "tinh luong",
    )
    if any(term in normalized for term in allowed_lookup_terms):
        return False

    personal_action_patterns = (
        "toi muon nghi",
        "em muon nghi",
        "minh muon nghi",
        "muon nghi han",
        "muon nghi viec",
        "xin nghi han",
        "nghi han viec",
        "lien he ai",
        "gap ai",
        "hoi ai",
        "bao ai",
        "nen lam gi",
        "toi can lam gi",
        "em can lam gi",
        "tu van",
    )
    hr_action_topics = ("nghi viec", "nghi han", "thoi viec", "xin nghi")
    return any(pattern in normalized for pattern in personal_action_patterns) and any(
        topic in normalized for topic in hr_action_topics
    )


def _is_known_missing_system_info_query(query: str) -> bool:
    normalized = _normalize_text(query)
    missing_patterns = (
        ("tien do", "dat chuan"),
        ("tien do", "phai nhu the nao"),
        ("tien do", "tieu chuan"),
        ("tien do", "chuan"),
    )
    return any(all(term in normalized for term in pattern) for pattern in missing_patterns)


def _preferred_source_terms(query: str) -> list[tuple[str, ...]]:
    normalized = _normalize_text(query)
    attendance_terms = (
        "di muon",
        "di tre",
        "vao muon",
        "vao tre",
        "phat bao nhieu",
        "phat tien",
        "cham cong",
        "quen cham cong",
        "tinh luong hang thang",
    )
    if any(term in normalized for term in attendance_terms):
        return [
            ("cham", "cong", "tinh", "luong", "hang", "thang"),
            ("noi", "quy", "cong", "ty"),
            ("quy", "che", "luong", "thuong", "phuc", "loi"),
        ]
    if "ho so thanh toan" in normalized:
        return [("khoi", "ho", "huong", "dan", "ho", "so", "thanh", "toan")]
    if "hoa hong" in normalized and "ctv" in normalized:
        return [("hoa", "hong", "ctv")]
    if "co che" in normalized and "luong" in normalized:
        return [("co", "che", "tinh", "luong", "bu")]
    if (
        "phoi hop lien phong ban" in normalized
        or ("phoi hop" in normalized and "phong ban" in normalized)
        or ("phoi hop" in normalized and "lien phong" in normalized)
    ):
        return [("quy", "trinh", "phoi", "hop", "lien", "phong", "ban")]
    if "nguyen tac huan luyen" in normalized or "huan luyen thuc chien" in normalized:
        return [("nguyen", "tac", "huan", "luyen", "thuc", "chien")]
    if (
        "bo khung van hanh" in normalized
        or "phat trien doi tac" in normalized
        or ("van hanh" in normalized and "doi tac" in normalized)
    ):
        return [("bo", "khung", "van", "hanh", "phat", "trien", "doi", "tac")]
    return []


def _ocr_cache_path(file_path: Path) -> Path:
    stat = file_path.stat()
    raw = f"{file_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
    return OCR_CACHE_DIR / f"{hashlib.sha1(raw).hexdigest()}.json"


def _load_ocr_cache(file_path: Path) -> list[str] | None:
    cache_path = _ocr_cache_path(file_path)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pages = data.get("pages")
    if not isinstance(pages, list):
        return None
    return [str(page) for page in pages]


def _save_ocr_cache(file_path: Path, pages: list[str]) -> None:
    try:
        OCR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _ocr_cache_path(file_path).write_text(
            json.dumps({"pages": pages}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def _ocr_pdf_with_gemini(file_path: Path) -> list[Document]:
    if os.getenv("ENABLE_GEMINI_PDF_OCR", "true").strip().lower() in {"0", "false", "no"}:
        return []
    cached_pages = _load_ocr_cache(file_path)
    if cached_pages is not None:
        page_texts = cached_pages
    else:
        try:
            import google.generativeai as genai
            from PIL import Image
        except ImportError:
            return []
        try:
            api_key = _gemini_key()
            if not api_key:
                return []
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(os.getenv("GEMINI_OCR_MODEL", "gemini-2.5-flash"))
            reader = PdfReader(str(file_path))
            max_pages = int(os.getenv("GEMINI_OCR_MAX_PAGES", "12"))
            page_texts = []
            for page_index, page in enumerate(reader.pages[:max_pages]):
                images = list(getattr(page, "images", []) or [])
                if not images:
                    page_texts.append("")
                    continue
                image = Image.open(io.BytesIO(images[0].data))
                response = model.generate_content(
                    [
                        (
                            "Trích xuất toàn bộ chữ tiếng Việt trong ảnh tài liệu này. "
                            "Giữ nguyên số tiền, tỷ lệ, bảng, điều khoản và xuống dòng quan trọng. "
                            "Chỉ trả về nội dung OCR, không giải thích."
                        ),
                        image,
                    ]
                )
                page_texts.append((getattr(response, "text", "") or "").strip())
            _save_ocr_cache(file_path, page_texts)
        except Exception:
            return []

    relative_path = str(file_path.relative_to(DATA_DIR)).replace("\\", "/")
    manifest_item = manifest_item_for_path(relative_path)
    docs: list[Document] = []
    for page_index, text in enumerate(page_texts):
        if not text.strip():
            continue
        metadata = {
            "source": str(file_path),
            "file_name": file_path.name,
            "relative_path": relative_path,
            "page": page_index,
            "source_type": "sharepoint" if manifest_item else "local",
            "ocr": True,
        }
        if manifest_item.get("web_url"):
            metadata["web_url"] = manifest_item["web_url"]
        if manifest_item.get("last_modified"):
            metadata["last_modified"] = manifest_item["last_modified"]
        if manifest_item.get("relative_path"):
            metadata["sharepoint_relative_path"] = manifest_item["relative_path"]
        docs.append(Document(page_content=text, metadata=metadata))
    return docs


def _load_local_file_documents(file_path: Path) -> list[Document]:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        docs = PyPDFLoader(str(file_path)).load()
    elif suffix == ".docx":
        docs = Docx2txtLoader(str(file_path)).load()
    elif suffix == ".txt":
        docs = TextLoader(str(file_path), encoding="utf-8").load()
    else:
        return []

    if suffix == ".pdf" and not any(doc.page_content.strip() for doc in docs):
        docs = _ocr_pdf_with_gemini(file_path)

    relative_path = str(file_path.relative_to(DATA_DIR)).replace("\\", "/")
    manifest_item = manifest_item_for_path(relative_path)
    for doc in docs:
        doc.metadata["source"] = str(file_path)
        doc.metadata["file_name"] = file_path.name
        doc.metadata["relative_path"] = relative_path
        doc.metadata["source_type"] = "sharepoint" if manifest_item else "local"
        if manifest_item.get("web_url"):
            doc.metadata["web_url"] = manifest_item["web_url"]
        if manifest_item.get("last_modified"):
            doc.metadata["last_modified"] = manifest_item["last_modified"]
        if manifest_item.get("relative_path"):
            doc.metadata["sharepoint_relative_path"] = manifest_item["relative_path"]
    return docs


def get_preferred_source_documents(query: str) -> list[Document]:
    preferred_groups = _preferred_source_terms(query)
    if not preferred_groups:
        return []

    for source_terms in preferred_groups:
        docs: list[Document] = []
        for file_path in document_files():
            relative_path = str(file_path.relative_to(DATA_DIR)).replace("\\", "/")
            normalized_path = _normalize_text(relative_path)
            if not all(term in normalized_path for term in source_terms):
                continue
            docs.extend(_load_local_file_documents(file_path))
        ranked_docs = _rank_and_filter_documents(docs, query)[:24]
        if ranked_docs:
            return ranked_docs

    return []


def _preferred_source_file_links(query: str, limit: int = 5) -> list[tuple[str, str]]:
    preferred_groups = _preferred_source_terms(query)
    if not preferred_groups:
        return []

    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source_terms in preferred_groups:
        for file_path in document_files():
            relative_path = str(file_path.relative_to(DATA_DIR)).replace("\\", "/")
            normalized_path = _normalize_text(relative_path)
            if not all(term in normalized_path for term in source_terms):
                continue
            if relative_path in seen:
                continue
            seen.add(relative_path)
            manifest_item = manifest_item_for_path(relative_path)
            links.append((file_path.name, str(manifest_item.get("web_url") or relative_path)))
            if len(links) >= limit:
                return links
    return links


def _has_clear_topic(query: str) -> bool:
    normalized = _normalize_text(query)
    topic_terms = (
        "cong tac",
        "nghi viec",
        "thanh toan",
        "tam ung",
        "nghi phep",
        "luong",
        "thuong",
        "kpi",
        "dong phuc",
        "bao ho",
        "thu viec",
        "tuyen dung",
        "nhan su",
        "phoi hop",
        "phong ban",
        "lien phong ban",
        "huan luyen",
        "thuc chien",
        "van hanh",
        "doi tac",
        "di muon",
        "di tre",
        "vao muon",
        "vao tre",
        "phat",
        "cham cong",
        "tinh luong",
        "quy trinh",
        "quy dinh",
        "chinh sach",
        "bieu mau",
        "template",
        "form",
    )
    return any(term in normalized for term in topic_terms)


def _resolve_followup_query(query: str, conversation_context: str | None = None) -> str:
    if not conversation_context or _has_clear_topic(query):
        return query

    normalized = _normalize_text(query)
    followup_terms = (
        "mau",
        "bieu mau",
        "link",
        "tai",
        "download",
        "quy trinh",
        "huong dan",
        "cai nay",
        "cai do",
        "bo nay",
        "nhung gi",
        "gom",
        "them",
    )
    is_short = len(normalized.split()) <= 8
    if is_short or any(term in normalized for term in followup_terms):
        return f"{conversation_context}\nCâu hỏi tiếp theo: {query}"
    return query


def _metadata_text(doc: Document) -> str:
    metadata = doc.metadata or {}
    values = [
        metadata.get("file_name"),
        metadata.get("relative_path"),
        metadata.get("sharepoint_relative_path"),
        metadata.get("source_category"),
        metadata.get("source"),
    ]
    return " ".join(str(value) for value in values if value)


def _document_date(doc: Document) -> str:
    metadata = doc.metadata or {}
    explicit = str(metadata.get("last_modified") or "")
    if explicit:
        return explicit
    source = str(metadata.get("file_name") or metadata.get("source") or "")
    match = re.search(r"\b(20\d{2})(\d{2})(\d{2})\b", source)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}T00:00:00Z"
    match = re.search(r"\b(20\d{2})(\d{2})\b", source)
    if match:
        return f"{match.group(1)}-{match.group(2)}-01T00:00:00Z"
    return ""


def _enrich_sharepoint_metadata(doc: Document) -> Document:
    metadata = dict(doc.metadata or {})
    if metadata.get("web_url") and metadata.get("last_modified"):
        return doc

    candidates = [
        metadata.get("relative_path"),
        metadata.get("sharepoint_relative_path"),
        metadata.get("source"),
        metadata.get("file_name"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        manifest_item = manifest_item_for_path(str(candidate))
        if not manifest_item:
            continue
        metadata.setdefault("web_url", manifest_item.get("web_url"))
        metadata.setdefault("last_modified", manifest_item.get("last_modified"))
        metadata.setdefault("sharepoint_relative_path", manifest_item.get("relative_path"))
        metadata["source_type"] = "sharepoint"
        return Document(page_content=doc.page_content, metadata=metadata)
    return doc


def _doc_relevance_score(doc: Document, query: str, intent: str) -> int:
    terms = _query_terms(query)
    normalized_content = _normalize_text(doc.page_content)
    normalized_metadata = _normalize_text(_metadata_text(doc))
    score = 0
    score += sum(3 for term in terms if term in normalized_metadata)
    score += sum(1 for term in terms if term in normalized_content)

    if intent == "form":
        if any(term in normalized_metadata for term in ("bieu mau", "template", "form", "phieu", "don")):
            score += 12
    elif intent == "process":
        if any(term in normalized_metadata for term in ("quy trinh", "huong dan")):
            score += 6
    elif intent == "policy":
        if any(term in normalized_metadata for term in ("quy dinh", "chinh sach", "quy che", "noi quy")):
            score += 6

    if doc.metadata.get("web_url") or doc.metadata.get("source_type") == "sharepoint":
        score += 2
    return score


def _rank_and_filter_documents(docs: Iterable[Document], query: str) -> list[Document]:
    intent = _intent(query)
    ranked: list[tuple[int, str, int, Document]] = []
    for index, doc in enumerate(docs):
        enriched = _enrich_sharepoint_metadata(doc)
        score = _doc_relevance_score(enriched, query, intent)
        if score <= 0:
            continue
        ranked.append((score, _document_date(enriched), index, enriched))

    ranked.sort(key=lambda item: item[2])
    ranked.sort(key=lambda item: item[1], reverse=True)
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [doc for _, _, _, doc in ranked]


def _is_numbered_list_query(query: str) -> bool:
    normalized = _normalize_text(query)
    has_list_signal = bool(re.search(r"\b\d+\b", normalized)) or any(
        term in normalized for term in ("bao nhieu", "liet ke", "danh sach", "cac")
    )
    has_list_topic = any(
        term in normalized
        for term in (
            "bi kip",
            "chieu",
            "nguyen tac",
            "buoc",
            "muc",
            "noi dung",
            "mo hinh",
            "quy trinh",
            "tieu chi",
            "quy tac",
        )
    )
    return has_list_signal and has_list_topic


def _is_tam_phap_10_chieu_query(query: str) -> bool:
    normalized = _normalize_text(query)
    if "10 chieu" in normalized or "chieu thuc" in normalized:
        return True
    return (
        ("10" in normalized or "muoi" in normalized)
        and ("chieu" in normalized or "tam phap" in normalized)
    )


def _is_tam_phap_bi_kip_query(query: str) -> bool:
    normalized = _normalize_text(query)
    if "bi kip" in normalized:
        return True
    return (
        ("quan ly" in normalized or "lanh dao" in normalized)
        and ("tam phap" in normalized or "flexfit" in normalized or "8" in normalized)
    )


def _collection_documents(vector_store: Chroma) -> list[tuple[int, str, dict, Document]]:
    collection = vector_store._collection.get(include=["documents", "metadatas"])
    rows: list[tuple[int, str, dict, Document]] = []
    for index, content in enumerate(collection.get("documents") or []):
        metadata = (collection.get("metadatas") or [{}])[index] or {}
        source = str(metadata.get("source") or metadata.get("file_name") or "")
        rows.append(
            (
                index,
                source,
                metadata,
                Document(page_content=content or "", metadata=metadata),
            )
        )
    return rows


def _page_number(metadata: dict) -> int | None:
    page = metadata.get("page")
    try:
        return int(page)
    except (TypeError, ValueError):
        return None


def get_documents_by_source_pages(
    vector_store: Chroma,
    source_terms: Iterable[str],
    pages: set[int],
) -> list[Document]:
    ranked: list[tuple[int, int, int, Document]] = []
    normalized_source_terms = [_normalize_text(term) for term in source_terms]

    for index, _, metadata, doc in _collection_documents(vector_store):
        file_name = metadata.get("file_name") or os.path.basename(str(metadata.get("source") or ""))
        normalized_file_name = _normalize_text(str(file_name))
        if not all(term in normalized_file_name for term in normalized_source_terms):
            continue

        page_number = _page_number(metadata)
        if page_number is None or page_number not in pages:
            continue

        normalized_content = _normalize_text(doc.page_content)
        item_numbers = [
            number
            for number in range(1, 21)
            if normalized_content.find(f"{number}.") >= 0
        ]
        section_position = min(item_numbers) if item_numbers else 999_999
        ranked.append((page_number, section_position, index, doc))

    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [doc for _, _, _, doc in ranked]


def get_documents_by_source_terms(
    vector_store: Chroma,
    source_terms: Iterable[str],
) -> list[Document]:
    normalized_source_terms = [_normalize_text(term) for term in source_terms]
    ranked: list[tuple[str, int, int, Document]] = []
    for index, _, metadata, doc in _collection_documents(vector_store):
        source_text = " ".join(
            str(value)
            for value in (
                metadata.get("file_name"),
                metadata.get("relative_path"),
                metadata.get("sharepoint_relative_path"),
                metadata.get("source"),
            )
            if value
        )
        normalized_source = _normalize_text(source_text)
        if not all(term in normalized_source for term in normalized_source_terms):
            continue
        ranked.append((_document_date(doc), _page_number(metadata) or 0, index, doc))

    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [doc for _, _, _, doc in ranked]


def get_neighbor_documents(
    vector_store: Chroma,
    seed_docs: Iterable[Document],
    page_radius: int = 1,
) -> list[Document]:
    wanted_pages_by_source: dict[str, set[int]] = {}

    for doc in seed_docs:
        source = str(doc.metadata.get("source") or doc.metadata.get("file_name") or "")
        page_number = _page_number(doc.metadata)
        if not source or page_number is None:
            continue

        wanted_pages_by_source.setdefault(source, set()).update(
            range(max(0, page_number - page_radius), page_number + page_radius + 1)
        )

    if not wanted_pages_by_source:
        return []

    ranked: list[tuple[str, int, int, Document]] = []
    for index, source, metadata, doc in _collection_documents(vector_store):
        page_number = _page_number(metadata)
        if page_number is None or page_number not in wanted_pages_by_source.get(source, set()):
            continue

        ranked.append((source, page_number, index, doc))

    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [doc for _, _, _, doc in ranked]


def get_tam_phap_action_documents(vector_store: Chroma) -> list[Document]:
    return get_documents_by_source_pages(vector_store, ("flexfit", "tam phap"), set(range(10)))


def get_tam_phap_management_documents(vector_store: Chroma) -> list[Document]:
    return get_documents_by_source_pages(vector_store, ("flexfit", "tam phap"), set(range(10)))


def get_vector_store() -> Chroma:
    if not os.path.exists(CHROMA_DB_DIR):
        raise FileNotFoundError(
            f"Chưa có Vector DB tại '{CHROMA_DB_DIR}'. Hãy chạy indexer.py trước."
        )

    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=_gemini_key(),
    )
    return Chroma(
        persist_directory=CHROMA_DB_DIR,
        embedding_function=embeddings,
        client_settings=Settings(anonymized_telemetry=False),
    )


def get_keyword_documents(vector_store: Chroma, query: str, limit: int = 16) -> list[Document]:
    terms = _query_terms(query)
    if not terms:
        return []

    normalized_query = _normalize_text(query)
    phrase_boosts = [
        "10 chieu",
        "chieu thuc",
        "tam phap",
        "8 bi kip",
        "bi kip",
        "quan ly",
        "lanh dao",
        "nghi phep",
        "phep nam",
        "nguon phep",
        "cong tac phi",
        "tam ung",
        "hoan ung",
        "thanh toan",
        "dong phuc",
        "bao ho lao dong",
        "phoi hop lien phong ban",
        "phoi hop phong ban",
        "lien phong ban",
        "nguyen tac huan luyen",
        "huan luyen thuc chien",
        "bo khung van hanh",
        "phat trien doi tac",
        "thu viec",
        "kpi",
        "luong",
        "thuong",
        "phuc loi",
    ]
    relevant_phrases = [phrase for phrase in phrase_boosts if phrase in normalized_query]

    collection = vector_store._collection.get(include=["documents", "metadatas"])
    ranked: list[tuple[int, int, Document]] = []
    for index, content in enumerate(collection.get("documents") or []):
        normalized_content = _normalize_text(content or "")
        score = sum(1 for term in terms if term in normalized_content)
        score += sum(10 for phrase in relevant_phrases if phrase in normalized_content)
        if score == 0:
            continue
        metadata = (collection.get("metadatas") or [{}])[index] or {}
        ranked.append((score, len(content or ""), Document(page_content=content, metadata=metadata)))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [doc for _, _, doc in ranked[:limit]]


def _merge_documents(*document_groups: Iterable[Document]) -> list[Document]:
    merged: list[Document] = []
    seen: set[tuple[str, int | str | None, str]] = set()
    for docs in document_groups:
        for doc in docs:
            key = (
                str(doc.metadata.get("source") or doc.metadata.get("file_name") or ""),
                doc.metadata.get("page") if doc.metadata.get("page") is not None else doc.metadata.get("sheet"),
                doc.page_content[:120],
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(doc)
    return merged


def get_relevant_documents(query: str) -> list[Document]:
    preferred_local_docs = get_preferred_source_documents(query)
    if preferred_local_docs:
        return preferred_local_docs
    if _preferred_source_terms(query):
        return []

    vector_store = get_vector_store()
    retriever = vector_store.as_retriever(
        search_kwargs={
            "k": 20,
        },
    )
    priority_docs = []
    if _is_tam_phap_10_chieu_query(query):
        priority_docs = get_tam_phap_action_documents(vector_store)
    elif _is_tam_phap_bi_kip_query(query):
        priority_docs = get_tam_phap_management_documents(vector_store)
    else:
        for source_terms in _preferred_source_terms(query):
            priority_docs.extend(get_documents_by_source_terms(vector_store, source_terms))

    if priority_docs:
        return _rank_and_filter_documents(priority_docs, query)[:24] or priority_docs[:24]

    keyword_docs = get_keyword_documents(vector_store, query)
    vector_docs = retriever.invoke(query)
    seed_docs = _merge_documents(priority_docs, keyword_docs, vector_docs)
    expanded_docs = []
    if _is_numbered_list_query(query) and not priority_docs:
        expanded_docs = get_neighbor_documents(vector_store, seed_docs[:5], page_radius=2)

    docs = _merge_documents(priority_docs, expanded_docs, keyword_docs, vector_docs)
    return _rank_and_filter_documents(docs, query)[:32]


def answer_query(
    query: str,
    include_file_links: bool = True,
    conversation_context: str | None = None,
) -> AgentAnswer:
    try:
        effective_query = _resolve_followup_query(query, conversation_context)
        if _is_out_of_scope_query(effective_query):
            return AgentAnswer(text=OUT_OF_SCOPE_MESSAGE, files=[])
        if _is_known_missing_system_info_query(effective_query):
            return AgentAnswer(text=MISSING_SYSTEM_INFO_MESSAGE, files=[])

        single_form_answer = build_single_form_answer(effective_query, include_file_links)
        if single_form_answer:
            text, files = single_form_answer
            return AgentAnswer(text=text, files=files)

        form_set_answer = build_form_set_answer(effective_query, include_file_links)
        if form_set_answer:
            text, files = form_set_answer
            return AgentAnswer(text=text, files=files)

        document_reference_answer = build_document_reference_answer(effective_query, include_file_links)
        if document_reference_answer:
            text, files = document_reference_answer
            return AgentAnswer(text=text, files=files)

        document_shortcut_answer = build_document_shortcut_answer(effective_query, include_file_links)
        if document_shortcut_answer:
            text, files = document_shortcut_answer
            return AgentAnswer(text=text, files=files)

        intent = _intent(effective_query)
        if intent == "form":
            form_files = find_related_files(effective_query, None, limit=5)
            if form_files:
                text = (
                    "Bạn có thể tham khảo các mẫu phù hợp trên SharePoint:"
                    "\n"
                    + format_file_references(form_files, include_file_links)
                )
                return AgentAnswer(text=text, files=form_files)

        docs = get_relevant_documents(effective_query)
        context = _format_context(docs)
        files = find_related_files(effective_query, docs[:8]) if intent == "form" else []

        if not context:
            source_links = _preferred_source_file_links(effective_query)
            if source_links:
                lines = [
                    "Tôi tìm thấy tài liệu liên quan trên SharePoint, nhưng hiện chưa trích xuất được nội dung chi tiết từ file để trả lời chắc chắn.",
                    "Bạn có thể mở các tài liệu này để kiểm tra trực tiếp:",
                ]
                lines.extend(f"- {name}: {url}" for name, url in source_links)
                lines.append(
                    "Nếu các file này là PDF scan, hãy bật OCR Gemini hoặc rebuild lại Vector DB sau khi OCR để bot trả lời được nội dung bên trong."
                )
                return AgentAnswer(text="\n".join(lines), files=[])

            text = MISSING_SYSTEM_INFO_MESSAGE
            if files:
                if intent == "form":
                    text = "Bạn có thể tham khảo các mẫu phù hợp trên SharePoint:"
                text += "\n" + format_file_references(files, include_file_links)
            return AgentAnswer(text=text, files=files)

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0,
            google_api_key=_gemini_key(),
        )
        chain = ANSWER_PROMPT | llm | StrOutputParser()
        answer = chain.invoke({"question": effective_query, "context": context}).strip()

        if answer.strip() == NO_SHAREPOINT_MATCH and files and intent == "form":
            answer = "Bạn có thể tham khảo các mẫu phù hợp trên SharePoint:"
        elif answer.strip() == NO_SHAREPOINT_MATCH:
            return AgentAnswer(text=MISSING_SYSTEM_INFO_MESSAGE, files=[])

        sources = _unique_sources(docs[:6])
        if sources:
            answer += "\n\nNguồn đã tra cứu:\n" + "\n".join(f"- {source}" for source in sources)

        source_links = _unique_source_links(docs[:6])
        if source_links:
            answer += "\n\nLink tài liệu tham khảo:\n" + "\n".join(
                f"- {name}: {url}" for name, url in source_links
            )

        if files and intent == "form":
            answer += "\n\nBạn có thể tham khảo mẫu liên quan:\n" + format_file_references(files, include_file_links)

        return AgentAnswer(text=answer, files=files)
    except Exception as e:
        return AgentAnswer(text=f"Lỗi trong quá trình xử lý: {e}", files=[])


def ask_agent(query: str) -> str:
    return answer_query(query).text
