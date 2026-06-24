import os
import shutil
import sys
from pathlib import Path

from paths import BASE_DIR, CHROMA_DB_DIR, DATA_DIR


def _copy_tree_if_missing(source: Path, destination: Path) -> bool:
    if destination.exists() and any(destination.iterdir()):
        return False
    if not source.exists() or not any(source.iterdir()):
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return True


def _has_sharepoint_credentials() -> bool:
    required = (
        "SHAREPOINT_CLIENT_ID",
        "SHAREPOINT_TENANT_ID",
        "SHAREPOINT_CLIENT_SECRET",
    )
    return all(os.getenv(name) for name in required)


def _ensure_runtime_data() -> None:
    bundled_data_dir = BASE_DIR / "data_documents"
    bundled_chroma_dir = BASE_DIR / "chroma_db"

    copied_data = _copy_tree_if_missing(bundled_data_dir, DATA_DIR)
    copied_chroma = _copy_tree_if_missing(bundled_chroma_dir, CHROMA_DB_DIR)
    if copied_data:
        print(f"Copied bundled data_documents to {DATA_DIR}", flush=True)
    if copied_chroma:
        print(f"Copied bundled chroma_db to {CHROMA_DB_DIR}", flush=True)

    if CHROMA_DB_DIR.exists() and any(CHROMA_DB_DIR.iterdir()):
        return

    if _has_sharepoint_credentials():
        print("No Vector DB found. Syncing SharePoint and building index...", flush=True)
        from sync_documents import sync_documents

        try:
            sync_documents(force_index=True)
        except Exception as exc:
            print(f"Initial SharePoint sync failed: {exc}", flush=True)
            print("Starting the app without Vector DB so the issue is visible in the UI.", flush=True)
        return

    if DATA_DIR.exists() and any(DATA_DIR.iterdir()):
        print("No Vector DB found. Building index from bundled documents...", flush=True)
        from indexer import build_index

        build_index()
        return

    raise RuntimeError(
        "No runtime data found. Add data_documents/chroma_db to the deploy "
        "or configure SharePoint credentials on Render."
    )


def main() -> None:
    _ensure_runtime_data()

    port = os.getenv("PORT", "8501")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.address",
        "0.0.0.0",
        "--server.port",
        port,
        "--server.headless",
        "true",
    ]
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
