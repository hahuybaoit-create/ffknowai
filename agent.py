import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

from dotenv import load_dotenv
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from document_files import (
    FileReference,
    build_document_shortcut_answer,
    build_form_set_answer,
    build_single_form_answer,
    find_related_files,
    format_file_references,
    manifest_item_for_path,
)
from paths import CHROMA_DB_DIR

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHROMA_DB_DIR = str(CHROMA_DB_DIR)


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
    if any(term in normalized for term in ("quy dinh", "chinh sach", "quy che", "noi quy", "che do")):
        return "policy"
    return "general"


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
    return Chroma(persist_directory=CHROMA_DB_DIR, embedding_function=embeddings)


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
        single_form_answer = build_single_form_answer(effective_query, include_file_links)
        if single_form_answer:
            text, files = single_form_answer
            return AgentAnswer(text=text, files=files)

        form_set_answer = build_form_set_answer(effective_query, include_file_links)
        if form_set_answer:
            text, files = form_set_answer
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
            text = NO_SHAREPOINT_MATCH
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

        sources = _unique_sources(docs[:6])
        if sources:
            answer += "\n\nNguồn đã tra cứu:\n" + "\n".join(f"- {source}" for source in sources)

        if files and intent == "form":
            answer += "\n\nBạn có thể tham khảo mẫu liên quan:\n" + format_file_references(files, include_file_links)

        return AgentAnswer(text=answer, files=files)
    except Exception as e:
        return AgentAnswer(text=f"Lỗi trong quá trình xử lý: {e}", files=[])


def ask_agent(query: str) -> str:
    return answer_query(query).text
