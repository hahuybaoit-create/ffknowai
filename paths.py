import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
APP_DATA_ROOT = Path(os.getenv("FF_APP_DATA_ROOT", str(BASE_DIR))).resolve()

DATA_DIR = Path(os.getenv("FF_DATA_DIR", str(APP_DATA_ROOT / "data_documents"))).resolve()
CHROMA_DB_DIR = Path(os.getenv("FF_CHROMA_DB_DIR", str(APP_DATA_ROOT / "chroma_db"))).resolve()
CHROMA_DB_TMP_DIR = Path(
    os.getenv("FF_CHROMA_DB_TMP_DIR", str(APP_DATA_ROOT / "chroma_db_tmp"))
).resolve()
