import gc
import json
import os
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, TextLoader
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from paths import CHROMA_DB_DIR, CHROMA_DB_TMP_DIR, DATA_DIR

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".xlsx"}
IGNORED_FILENAMES = {"_sharepoint_manifest.json"}
MANIFEST_PATH = DATA_DIR / "_sharepoint_manifest.json"


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


def _load_file(file_path: Path) -> list[Document]:
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return PyPDFLoader(str(file_path)).load()
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
    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=gemini_key,
    )

    print("Dang tao embeddings va luu vao ChromaDB tam...")
    vector_store = Chroma(persist_directory=str(CHROMA_DB_TMP_DIR), embedding_function=embeddings)

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
