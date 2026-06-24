import os
import shutil
import subprocess
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


def _prepare_runtime_data() -> None:
    bundled_data_dir = BASE_DIR / "data_documents"
    bundled_chroma_dir = BASE_DIR / "chroma_db"

    copied_data = _copy_tree_if_missing(bundled_data_dir, DATA_DIR)
    copied_chroma = _copy_tree_if_missing(bundled_chroma_dir, CHROMA_DB_DIR)
    if copied_data:
        print(f"Copied bundled data_documents to {DATA_DIR}", flush=True)
    if copied_chroma:
        print(f"Copied bundled chroma_db to {CHROMA_DB_DIR}", flush=True)

    if CHROMA_DB_DIR.exists() and any(CHROMA_DB_DIR.iterdir()):
        print("Vector DB found. Starting Streamlit.", flush=True)
        return

    if DATA_DIR.exists() and any(DATA_DIR.iterdir()):
        print(
            "Documents found but no Vector DB exists yet. Starting Streamlit now; "
            "use the app sidebar to create or rebuild the Vector DB.",
            flush=True,
        )
        return

    if _has_sharepoint_credentials():
        print(
            "No runtime data found yet. Starting Streamlit now; use the app "
            "button to sync SharePoint and create the Vector DB.",
            flush=True,
        )
        return

    print(
        "No runtime data found. Starting Streamlit so the app can show setup "
        "instructions. Add bundled data/chroma_db or configure SharePoint "
        "credentials on Render.",
        flush=True,
    )


def main() -> None:
    _prepare_runtime_data()

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
    raise SystemExit(subprocess.run(command, env=os.environ, check=False).returncode)


if __name__ == "__main__":
    main()
