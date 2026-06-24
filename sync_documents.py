import argparse
import json
from pathlib import Path

from indexer import CHROMA_DB_DIR, build_index
from sharepoint_loader import DEFAULT_DOWNLOAD_DIR, MANIFEST_NAME, download_documents

INDEX_STATE_NAME = "_source_manifest.json"


def _index_state_path() -> Path:
    return Path(CHROMA_DB_DIR) / INDEX_STATE_NAME


def _load_index_state() -> dict:
    state_path = _index_state_path()
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_index_state(manifest: dict) -> None:
    state_path = _index_state_path()
    state_path.write_text(
        json.dumps(
            {
                "sharepoint_updated_at": manifest.get("updated_at"),
                "file_count": manifest.get("file_count"),
                "files": manifest.get("files", []),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _load_download_manifest() -> dict:
    manifest_path = Path(DEFAULT_DOWNLOAD_DIR) / MANIFEST_NAME
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Chưa có manifest tại '{manifest_path}'. Hãy chạy đồng bộ SharePoint trước."
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def sync_documents(
    force_index: bool = False,
    skip_download: bool = False,
    skip_index: bool = False,
) -> None:
    manifest = _load_download_manifest() if skip_download else download_documents(DEFAULT_DOWNLOAD_DIR)
    if skip_index:
        print("Đã tải tài liệu SharePoint. Bỏ qua bước rebuild index theo yêu cầu skip-index.")
        return

    changed = bool(manifest.get("changed_since_previous_run"))
    index_state = _load_index_state()
    index_matches_sources = index_state.get("files") == manifest.get("files", [])
    needs_index = force_index or changed or not Path(CHROMA_DB_DIR).exists() or not index_matches_sources

    if not needs_index:
        print("Vector DB đã cập nhật. Bỏ qua bước rebuild index.")
        return

    if force_index:
        print("Đang rebuild index theo yêu cầu force-index...")
    elif changed:
        print("Có tài liệu mới/thay đổi. Đang rebuild index...")
    else:
        print("Chưa có Vector DB. Đang tạo index lần đầu...")
    build_index()
    _write_index_state(manifest)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tải tài liệu SharePoint mới nhất và tự rebuild ChromaDB khi có thay đổi."
    )
    parser.add_argument(
        "--force-index",
        action="store_true",
        help="Rebuild ChromaDB kể cả khi manifest không phát hiện thay đổi.",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Không tải lại SharePoint, chỉ dùng tài liệu và manifest hiện có trong data_documents.",
    )
    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="Chỉ tải tài liệu SharePoint, không rebuild ChromaDB.",
    )
    args = parser.parse_args()
    sync_documents(
        force_index=args.force_index,
        skip_download=args.skip_download,
        skip_index=args.skip_index,
    )


if __name__ == "__main__":
    main()
