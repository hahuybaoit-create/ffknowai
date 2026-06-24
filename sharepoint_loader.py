import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import msal
import requests
from dotenv import load_dotenv
from paths import DATA_DIR

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DOWNLOAD_DIR = DATA_DIR
MANIFEST_NAME = "_sharepoint_manifest.json"
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

CLIENT_ID = os.getenv("SHAREPOINT_CLIENT_ID")
TENANT_ID = os.getenv("SHAREPOINT_TENANT_ID")
CLIENT_SECRET = os.getenv("SHAREPOINT_CLIENT_SECRET")
SHAREPOINT_HOST = os.getenv("SHAREPOINT_HOST", "flexfitcom.sharepoint.com")
SHAREPOINT_SITE_PATH = os.getenv("SHAREPOINT_SITE_PATH", "/sites/Intranet")
SHAREPOINT_SITE_NAME = os.getenv("SHAREPOINT_SITE_NAME", "Intranet")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

SOURCE_FOLDERS = [
    {
        "name": "co_che_luong",
        "label": "Cơ chế lương",
        "sharepoint_url": "https://flexfitcom-my.sharepoint.com/shared?id=%2Fsites%2FIntranet%2FShared%20Documents%2FQuy%20ch%E1%BA%BF%2C%20ch%C3%ADnh%20s%C3%A1ch%20b%E1%BB%99%20ph%E1%BA%ADn%2FM%C3%B4%20h%C3%ACnh%20BU&listurl=https%3A%2F%2Fflexfitcom%2Esharepoint%2Ecom%2Fsites%2FIntranet%2FShared%20Documents&viewid=17f7279f%2Dc3d4%2D4b19%2D8e2b%2D7a728ce1fdd6",
    },
    {
        "name": "quy_dinh_chung",
        "label": "Quy định chung",
        "sharepoint_url": "https://flexfitcom-my.sharepoint.com/shared?id=%2Fsites%2FIntranet%2FShared%20Documents%2FQuy%20ch%E1%BA%BF%20v%C3%A0%20ch%C3%ADnh%20s%C3%A1ch%20chung%20c%C3%B4ng%20ty&listurl=https%3A%2F%2Fflexfitcom%2Esharepoint%2Ecom%2Fsites%2FIntranet%2FShared%20Documents&viewid=17f7279f%2Dc3d4%2D4b19%2D8e2b%2D7a728ce1fdd6",
    },
    {
        "name": "bieu_mau",
        "label": "Biểu mẫu",
        "sharepoint_url": "https://flexfitcom-my.sharepoint.com/shared?id=%2Fsites%2FIntranet%2FShared%20Documents%2FC%C3%A1c%20bi%E1%BB%83u%20m%E1%BA%ABu%20th%C6%B0%E1%BB%9Dng%20d%C3%B9ng&listurl=https%3A%2F%2Fflexfitcom%2Esharepoint%2Ecom%2Fsites%2FIntranet%2FShared%20Documents&viewid=17f7279f%2Dc3d4%2D4b19%2D8e2b%2D7a728ce1fdd6",
    },
    {
        "name": "bo_kit_nhan_su_moi",
        "label": "Bộ kit nhân sự mới",
        "sharepoint_url": "https://flexfitcom-my.sharepoint.com/shared?id=%2Fsites%2FIntranet%2FShared%20Documents%2FB%E1%BB%99%20kit%20nh%C3%A2n%20vi%C3%AAn%20m%E1%BB%9Bi&listurl=https%3A%2F%2Fflexfitcom%2Esharepoint%2Ecom%2Fsites%2FIntranet%2FShared%20Documents&viewid=17f7279f%2Dc3d4%2D4b19%2D8e2b%2D7a728ce1fdd6",
    },
]


def _sharepoint_path_from_url(shared_url: str) -> str:
    params = parse_qs(urlparse(shared_url).query)
    item_id = params.get("id", [""])[0]
    if not item_id:
        raise ValueError(f"Link SharePoint không có tham số id: {shared_url}")
    return unquote(item_id)


def _graph_get(url: str, access_token: str) -> dict:
    response = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=60)
    response.raise_for_status()
    return response.json()


def _safe_name(name: str) -> str:
    keep = []
    for char in name:
        keep.append(char if char.isalnum() or char in (" ", ".", "-", "_") else "_")
    return "".join(keep).strip().strip(".") or "unnamed"


def _load_manifest(download_dir: Path) -> dict:
    manifest_path = download_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_manifest(download_dir: Path, manifest: dict) -> None:
    manifest_path = download_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_access_token() -> str:
    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )
    result = app.acquire_token_silent(SCOPES, account=None)
    if not result:
        print("Không tìm thấy token trong cache. Đang yêu cầu token mới từ Azure AD...")
        result = app.acquire_token_for_client(scopes=SCOPES)

    if "access_token" not in result:
        raise RuntimeError(
            "Không thể lấy token. "
            f"Lỗi: {result.get('error')}. Mô tả: {result.get('error_description')}"
        )
    return result["access_token"]


def get_site_id(access_token: str) -> str:
    site_url = f"{GRAPH_ROOT}/sites/{SHAREPOINT_HOST}:{SHAREPOINT_SITE_PATH}"
    try:
        return _graph_get(site_url, access_token)["id"]
    except requests.HTTPError:
        search_url = f"{GRAPH_ROOT}/sites?search={quote(SHAREPOINT_SITE_NAME)}"
        sites = _graph_get(search_url, access_token).get("value", [])
        if not sites:
            raise RuntimeError(f"Không tìm thấy SharePoint site: {SHAREPOINT_SITE_NAME}")
        return sites[0]["id"]


def get_drives(access_token: str, site_id: str) -> list[dict]:
    url = f"{GRAPH_ROOT}/sites/{site_id}/drives"
    return _graph_get(url, access_token).get("value", [])


def _relative_drive_path(sharepoint_path: str) -> str:
    marker = "/Shared Documents/"
    if marker not in sharepoint_path:
        return sharepoint_path.strip("/")
    return sharepoint_path.split(marker, 1)[1].strip("/")


def _get_folder_item(access_token: str, site_id: str, drives: list[dict], relative_path: str) -> tuple[str, dict]:
    encoded_path = "/".join(quote(part) for part in relative_path.split("/") if part)
    errors = []
    for drive in drives:
        url = f"{GRAPH_ROOT}/sites/{site_id}/drives/{drive['id']}/root:/{encoded_path}"
        try:
            return drive["id"], _graph_get(url, access_token)
        except requests.HTTPError as exc:
            errors.append(f"{drive.get('name')}: {exc.response.status_code}")
    raise RuntimeError(f"Không tìm thấy thư mục SharePoint '{relative_path}'. Drives đã thử: {', '.join(errors)}")


def _iter_children(access_token: str, site_id: str, drive_id: str, folder_id: str) -> list[dict]:
    items: list[dict] = []
    url = f"{GRAPH_ROOT}/sites/{site_id}/drives/{drive_id}/items/{folder_id}/children"
    while url:
        payload = _graph_get(url, access_token)
        items.extend(payload.get("value", []))
        url = payload.get("@odata.nextLink")
    return items


def _download_file(download_url: str, destination: Path) -> None:
    response = requests.get(download_url, timeout=180)
    response.raise_for_status()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.content)


def _download_folder(
    access_token: str,
    site_id: str,
    drive_id: str,
    folder_id: str,
    destination_dir: Path,
    source_label: str,
    manifest_files: list[dict],
    relative_prefix: str = "",
) -> int:
    downloaded = 0
    for item in _iter_children(access_token, site_id, drive_id, folder_id):
        item_name = item["name"]
        item_relative_path = f"{relative_prefix}/{item_name}".strip("/")
        if "folder" in item:
            downloaded += _download_folder(
                access_token,
                site_id,
                drive_id,
                item["id"],
                destination_dir,
                source_label,
                manifest_files,
                item_relative_path,
            )
            continue

        if "file" not in item:
            continue

        suffix = Path(item_name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            print(f"Bỏ qua định dạng chưa hỗ trợ: {source_label}/{item_relative_path}")
            continue

        download_url = item.get("@microsoft.graph.downloadUrl")
        if not download_url:
            print(f"Bỏ qua file không có downloadUrl: {source_label}/{item_relative_path}")
            continue

        local_path = destination_dir / Path(item_relative_path)
        print(f"Đang tải: {source_label}/{item_relative_path}")
        _download_file(download_url, local_path)
        manifest_files.append(
            {
                "source": source_label,
                "name": item_name,
                "relative_path": f"{source_label}/{item_relative_path}",
                "local_path": f"{source_label}/{item_relative_path}",
                "sharepoint_path": item.get("parentReference", {}).get("path", ""),
                "web_url": item.get("webUrl"),
                "etag": item.get("eTag"),
                "last_modified": item.get("lastModifiedDateTime"),
                "size": item.get("size"),
            }
        )
        downloaded += 1
    return downloaded


def download_documents(download_dir: str | Path = DEFAULT_DOWNLOAD_DIR, clean: bool = True) -> dict:
    download_dir = Path(download_dir)
    previous_manifest = _load_manifest(download_dir)
    work_dir = download_dir.with_name(f"{download_dir.name}_tmp") if clean else download_dir

    if clean and work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    print("Đang xác thực với Microsoft Graph API...")
    token = get_access_token()
    print("Xác thực thành công. Đang tìm SharePoint site...")
    site_id = get_site_id(token)
    drives = get_drives(token, site_id)
    if not drives:
        raise RuntimeError("Không tìm thấy thư viện tài liệu nào trong SharePoint site.")

    manifest = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sources": SOURCE_FOLDERS,
        "files": [],
    }

    total_files = 0
    for source in SOURCE_FOLDERS:
        sharepoint_path = source.get("sharepoint_path") or _sharepoint_path_from_url(source["sharepoint_url"])
        source["sharepoint_path"] = sharepoint_path
        relative_path = _relative_drive_path(sharepoint_path)
        source_dir = work_dir / _safe_name(source["label"])
        print(f"Đang xử lý nguồn: {source['label']} ({relative_path})")
        drive_id, folder_item = _get_folder_item(token, site_id, drives, relative_path)
        total_files += _download_folder(
            token,
            site_id,
            drive_id,
            folder_item["id"],
            source_dir,
            source["label"],
            manifest["files"],
        )

    manifest["file_count"] = total_files
    changed = previous_manifest.get("files") != manifest["files"]
    manifest["changed_since_previous_run"] = changed
    _write_manifest(work_dir, manifest)

    if clean:
        if download_dir.exists():
            print(f"Đang thay thế thư mục tài liệu cũ {download_dir}...")
            shutil.rmtree(download_dir, ignore_errors=True)
        shutil.move(str(work_dir), str(download_dir))

    print(f"Hoàn thành tải {total_files} file về '{download_dir}'.")
    if changed:
        print("Có thay đổi so với lần tải trước.")
    else:
        print("Không phát hiện thay đổi so với lần tải trước.")
    return manifest


if __name__ == "__main__":
    download_documents()
