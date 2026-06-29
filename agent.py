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
from document_files import FileReference, find_related_files, format_file_references
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
liệu được cung cấp trong mỗi câu hỏi.

Luật bắt buộc:
- Không dùng kiến thức chung, internet, kinh nghiệm cá nhân, mẫu email/mẫu đơn
  chung, hoặc giả định ngoài tài liệu.
- Không viết các câu kiểu "thông thường", "phổ biến", "tùy công ty", trừ khi
  chính tài liệu nói như vậy.
- Nếu tài liệu không đủ căn cứ, chỉ trả lời:
  "Tôi chưa tìm thấy thông tin này trong bộ tài liệu đã cung cấp."
- Mỗi ý quan trọng phải có trích dẫn [Nguồn: tên_file, trang X]. Nếu nguồn
  không có số trang, dùng [Nguồn: tên_file].
- Nếu câu hỏi yêu cầu danh sách có số lượng cụ thể, ví dụ "10 chiêu thức",
  và context có đủ các mục, phải liệt kê đủ đúng số mục theo tài liệu, không
  tự ý rút gọn bỏ mục.
- Trả lời tiếng Việt, ngắn gọn, bám sát nguyên văn tài liệu.
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
lời hướng dẫn thao tác. Nếu context không có quy định chi tiết, nói rõ chưa tìm
thấy trong tài liệu.
""",
        ),
    ]
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
        formatted.append(f"[Đoạn {index} | Nguồn: {_source_label(doc)}]\n{content}")
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
        return priority_docs[:24]

    keyword_docs = get_keyword_documents(vector_store, query)
    vector_docs = retriever.invoke(query)
    seed_docs = _merge_documents(priority_docs, keyword_docs, vector_docs)
    expanded_docs = []
    if _is_numbered_list_query(query) and not priority_docs:
        expanded_docs = get_neighbor_documents(vector_store, seed_docs[:5], page_radius=2)

    return _merge_documents(priority_docs, expanded_docs, keyword_docs, vector_docs)[:32]


def answer_query(query: str, include_file_links: bool = False) -> AgentAnswer:
    try:
        docs = get_relevant_documents(query)
        context = _format_context(docs)
        files = find_related_files(query, docs[:8])

        if not context:
            text = "Tôi chưa tìm thấy thông tin này trong bộ tài liệu đã cung cấp."
            if files:
                text += "\n\nFile liên quan:\n" + format_file_references(files, include_file_links)
            return AgentAnswer(text=text, files=files)
            return "Tôi chưa tìm thấy thông tin này trong bộ tài liệu đã cung cấp."

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0,
            google_api_key=_gemini_key(),
        )
        chain = ANSWER_PROMPT | llm | StrOutputParser()
        answer = chain.invoke({"question": query, "context": context}).strip()

        sources = _unique_sources(docs[:6])
        if sources:
            answer += "\n\nNguồn đã tra cứu:\n" + "\n".join(f"- {source}" for source in sources)

        if files:
            answer += "\n\nFile liên quan:\n" + format_file_references(files, include_file_links)

        return AgentAnswer(text=answer, files=files)
        return answer
    except Exception as e:
        return AgentAnswer(text=f"Lỗi trong quá trình xử lý: {e}", files=[])
        return f"Lỗi trong quá trình xử lý: {e}"


def ask_agent(query: str) -> str:
    return answer_query(query).text
