import gc
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

from chromadb.config import Settings
from dotenv import load_dotenv
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, TextLoader
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from gemini_api import GoogleGenAIEmbeddings, new_client, upload_pdf
from paths import APP_DATA_ROOT, CHROMA_DB_DIR, CHROMA_DB_TMP_DIR, DATA_DIR

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".xlsx"}
IGNORED_FILENAMES = {"_sharepoint_manifest.json"}
MANIFEST_PATH = DATA_DIR / "_sharepoint_manifest.json"
OCR_CACHE_DIR = Path(os.getenv("GEMINI_OCR_CACHE_DIR", str(APP_DATA_ROOT / "ocr_cache"))).resolve()


class OcrError(RuntimeError):
    pass


class EmptyOcrError(OcrError):
    pass


def _redact_sensitive_error(exc: Exception) -> str:
    message = str(exc)
    api_key = os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'")
    if api_key:
        message = message.replace(api_key, "<redacted>")
    return re.sub(r"([?&]key=)[^&\s\"']+", r"\1<redacted>", message)


def _clean_text(text: str) -> str:
    return " ".join(str(text).split())


def _normalize_relative_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _load_sharepoint_manifest() -> dict[str, dict]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    items: dict[str, dict] = {}
    for item in manifest.get("files", []):
        for key in ("local_path", "relative_path"):
            value = item.get(key)
            if value:
                items[_normalize_relative_path(value)] = item
    return items


def _load_excel(file_path: Path) -> list[Document]:
    import pandas as pd

    documents: list[Document] = []
    sheets = pd.read_excel(file_path, sheet_name=None)
    for sheet_name, df in sheets.items():
        if df.empty:
            continue

        text = _clean_text(df.fillna("").to_string(index=False))
        if not text:
            continue

        documents.append(
            Document(
                page_content=text,
                metadata={
                    "source": str(file_path),
                    "file_name": file_path.name,
                    "sheet": sheet_name,
                },
            )
        )
    return documents


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


def _pdf_needs_ocr(documents: list[Document]) -> bool:
    if not documents:
        return True
    text = " ".join(doc.page_content or "" for doc in documents).strip()
    useful_text = text.lower().replace("scanned with camscanner", "").strip()
    return len(useful_text) < max(120, len(documents) * 40)


def _ocr_cache_path(file_path: Path) -> Path:
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return OCR_CACHE_DIR / f"{digest}.json"


def _load_cached_ocr(file_path: Path) -> str | None:
    cache_path = _ocr_cache_path(file_path)
    if not cache_path.exists():
        return None
    try:
        return str(json.loads(cache_path.read_text(encoding="utf-8")).get("text") or "").strip() or None
    except (OSError, json.JSONDecodeError):
        return None


def _save_cached_ocr(file_path: Path, text: str) -> None:
    OCR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _ocr_cache_path(file_path).write_text(
        json.dumps({"file_name": file_path.name, "text": text}, ensure_ascii=False),
        encoding="utf-8",
    )


def _ocr_pdf_with_gemini(file_path: Path, page_count: int) -> list[Document]:
    cached_text = _load_cached_ocr(file_path)
    if cached_text:
        print(f"Dung OCR cache: {file_path.name}")
        return [Document(page_content=cached_text, metadata={"page": 0, "ocr": "gemini-cache"})]

    max_pages = int(os.getenv("GEMINI_OCR_MAX_PAGES", "40"))
    if page_count > max_pages:
        raise OcrError(
            f"{file_path.name} co {page_count} trang, vuot GEMINI_OCR_MAX_PAGES={max_pages}"
        )

    api_key = os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'")
    if not api_key:
        raise OcrError(f"Chua co GEMINI_API_KEY de OCR {file_path.name}")

    client = new_client()
    uploaded = None
    try:
        print(f"PDF scan, dang OCR bang Gemini: {file_path.name} ({page_count} trang)")
        uploaded = upload_pdf(client, file_path)
        deadline = time.time() + 120
        while getattr(uploaded, "state", None) and uploaded.state.name == "PROCESSING":
            if time.time() >= deadline:
                raise TimeoutError("Gemini xu ly file OCR qua 120 giay")
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)
        if getattr(uploaded, "state", None) and uploaded.state.name == "FAILED":
            raise RuntimeError("Gemini khong xu ly duoc file PDF")

        response = client.models.generate_content(
            model=os.getenv("GEMINI_OCR_MODEL", "gemini-2.5-flash"),
            contents=[
                "Hay OCR va chep lai day du, chinh xac toan bo noi dung tai lieu PDF nay. "
                "Giu nguyen tieng Viet, so lieu, tieu de, muc va bang bieu; khong tom tat, "
                "khong binh luan. Danh dau moi trang bang [Trang N].",
                uploaded,
            ],
            config={"temperature": 0},
        )
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise EmptyOcrError("Gemini OCR tra ve noi dung trong")
        _save_cached_ocr(file_path, text)
        return [Document(page_content=text, metadata={"page": 0, "ocr": "gemini"})]
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _load_pdf(file_path: Path) -> list[Document]:
    documents = PyPDFLoader(str(file_path)).load()
    if not _pdf_needs_ocr(documents):
        return documents
    if not _truthy_env("ENABLE_GEMINI_PDF_OCR", "true"):
        print(f"PDF co it/noi dung scan nhung OCR dang tat: {file_path.name}")
        return documents
    try:
        ocr_documents = _ocr_pdf_with_gemini(file_path, len(documents))
        return ocr_documents or documents
    except EmptyOcrError:
        print(
            f"Gemini OCR tra ve rong cho {file_path.name}; "
            "bo qua OCR file nay va tiep tuc rebuild."
        )
        return documents
    except Exception as exc:
        if _truthy_env("GEMINI_OCR_STRICT", "true"):
            safe_error = _redact_sensitive_error(exc)
            raise OcrError(f"OCR Gemini that bai cho {file_path.name}: {safe_error}") from exc
        print(
            f"Loi OCR Gemini file {file_path.name}: {_redact_sensitive_error(exc)}. "
            "Dung noi dung PDF goc."
        )
        return documents


def _load_file(file_path: Path) -> list[Document]:
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return _load_pdf(file_path)
    if suffix == ".docx":
        return Docx2txtLoader(str(file_path)).load()
    if suffix == ".txt":
        return TextLoader(str(file_path), encoding="utf-8").load()
    if suffix == ".xlsx":
        return _load_excel(file_path)

    print(f"Bo qua dinh dang chua ho tro: {file_path.name}")
    return []


def _source_category(file_path: Path) -> str | None:
    try:
        relative_parts = file_path.relative_to(DATA_DIR).parts
    except ValueError:
        return None
    if len(relative_parts) <= 1:
        return None
    return relative_parts[0]


def _load_documents() -> list[Document]:
    documents: list[Document] = []
    files = sorted(path for path in DATA_DIR.rglob("*") if path.is_file())
    manifest_items = _load_sharepoint_manifest()

    for file_path in files:
        if file_path.name in IGNORED_FILENAMES:
            continue
        if file_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            print(f"Bo qua dinh dang chua ho tro: {file_path.name}")
            continue

        try:
            loaded_docs = _load_file(file_path)
            relative_path = _normalize_relative_path(str(file_path.relative_to(DATA_DIR)))
            manifest_item = manifest_items.get(relative_path) or {}
            for doc in loaded_docs:
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
                category = _source_category(file_path)
                if category:
                    doc.metadata["source_category"] = category
            documents.extend(loaded_docs)
            print(f"Da doc: {file_path.relative_to(DATA_DIR)} ({len(loaded_docs)} phan)")
        except OcrError as exc:
            print(f"Loi khi doc file {file_path.name}: {exc}")
            raise RuntimeError(
                "Khong rebuild index vi OCR PDF scan that bai; giu nguyen Vector DB cu. "
                f"{exc}"
            ) from exc
        except Exception as exc:
            print(f"Loi khi doc file {file_path.name}: {exc}")

    return documents


def build_index() -> None:
    if not DATA_DIR.exists() or not any(DATA_DIR.iterdir()):
        print(f"Thu muc '{DATA_DIR}' trong. Hay chay sharepoint_loader.py truoc.")
        return

    if CHROMA_DB_TMP_DIR.exists():
        print("Dang xoa Vector DB tam tu lan chay truoc...")
        shutil.rmtree(CHROMA_DB_TMP_DIR, ignore_errors=True)

    print("Dang doc tai lieu tu thu muc data_documents...")
    documents = _load_documents()
    if not documents:
        print("Khong tim thay noi dung hop le nao de index.")
        return

    print(f"Da tai {len(documents)} trang/doan tai lieu. Dang chia nho...")
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1200,
        chunk_overlap=250,
        length_function=len,
    )
    chunks = text_splitter.split_documents(documents)
    print(f"Da chia thanh {len(chunks)} chunks.")

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'")
    embeddings = GoogleGenAIEmbeddings(model="gemini-embedding-001")

    print("Dang tao embeddings va luu vao ChromaDB tam...")
    vector_store = Chroma(
        persist_directory=str(CHROMA_DB_TMP_DIR),
        embedding_function=embeddings,
        client_settings=Settings(anonymized_telemetry=False),
    )

    batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "40"))
    batch_sleep = float(os.getenv("EMBEDDING_BATCH_SLEEP", "65"))
    max_retries = int(os.getenv("EMBEDDING_MAX_RETRIES", "6"))
    total_batches = (len(chunks) - 1) // batch_size + 1

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        batch_no = i // batch_size + 1
        retry_count = 0
        while True:
            try:
                vector_store.add_documents(batch)
                break
            except Exception as exc:
                message = str(exc)
                if "429" not in message and "RESOURCE_EXHAUSTED" not in message:
                    raise
                retry_count += 1
                if retry_count > max_retries:
                    raise RuntimeError(
                        f"Vuot qua so lan retry Gemini cho batch {batch_no}. "
                        "Hay tang quota hoac chay lai sau."
                    ) from exc
                retry_sleep = max(batch_sleep, 70)
                print(
                    f"Gap rate limit Gemini. Cho {retry_sleep:g}s roi thu lai "
                    f"batch {batch_no} ({retry_count}/{max_retries})..."
                )
                time.sleep(retry_sleep)

        print(f"Da luu batch {batch_no}/{total_batches} ({len(batch)} chunks).")
        if batch_no < total_batches and batch_sleep > 0:
            print(f"Tam dung {batch_sleep:g}s de tranh rate limit...")
            time.sleep(batch_sleep)

    vector_store.persist()
    del vector_store
    gc.collect()
    time.sleep(2)
    if CHROMA_DB_DIR.exists():
        print("Dang thay the Vector DB cu bang ban moi...")
        shutil.rmtree(CHROMA_DB_DIR, ignore_errors=True)
    try:
        shutil.move(str(CHROMA_DB_TMP_DIR), str(CHROMA_DB_DIR))
    except PermissionError:
        if (CHROMA_DB_DIR / "chroma.sqlite3").exists():
            print("Vector DB da duoc chep xong; bo qua loi khoa file tam tren Windows.")
            shutil.rmtree(CHROMA_DB_TMP_DIR, ignore_errors=True)
        else:
            raise
    print(f"Hoan tat. Vector DB da duoc rebuild tai '{CHROMA_DB_DIR}'.")


if __name__ == "__main__":
    build_index()
