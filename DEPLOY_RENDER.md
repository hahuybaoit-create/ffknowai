# Deploy FF Know AI on Render

This app is a Streamlit web service with local ChromaDB data. Deploy it as a
Render Web Service, not as a static site.

## 1. Push the project to a private Git repository

Use a private GitHub/GitLab repository because this project contains internal
document names and may include internal documents.

Do not commit `.env`.

## 2. Create a Render Blueprint

1. Open Render Dashboard.
2. Choose **New +** -> **Blueprint**.
3. Connect the private repository.
4. Render will detect `render.yaml`.
5. Create the `ff-know-ai` service.

The service uses:

- Runtime: Python
- Python version: 3.11.9
- Region: Singapore
- Build command: `pip install -r requirements.txt`
- Start command: `python render_start.py`
- Persistent disk mount path: `/var/data`
- Persistent disk size: `5 GB`

When adding the Render disk manually, enter `/var/data` in **Mount Path**. The
app stores its runtime data under `FF_APP_DATA_ROOT=/var/data/ff-know-ai`, so
`data_documents`, `chroma_db`, and `chroma_db_tmp` will live on the persistent
disk.

Persistent disks require a paid Render web service. If the service is still on
the Free plan, Vector DB files created at runtime can disappear after a restart
or redeploy.

Render's default Python version can change for newly created services. This repo
pins Python with both `PYTHON_VERSION=3.11.9` in `render.yaml` and a
`.python-version` file so dependencies such as `pandas` install from compatible
prebuilt wheels instead of compiling from source.

## 3. Add secret environment variables

In the Render service environment settings, fill these values:

```text
GEMINI_API_KEY=...
APP_ACCESS_PASSWORD=...
SHAREPOINT_CLIENT_ID=...
SHAREPOINT_TENANT_ID=...
SHAREPOINT_CLIENT_SECRET=...
```

These are marked `sync: false` in `render.yaml`, so Render will ask for them and
will not store them in the repo.

The non-secret SharePoint defaults are already in `render.yaml`:

```text
SHAREPOINT_HOST=flexfitcom.sharepoint.com
SHAREPOINT_SITE_PATH=/sites/Intranet
SHAREPOINT_SITE_NAME=Intranet
PERSONNEL_GOOGLE_SHEET_URL=https://docs.google.com/spreadsheets/d/.../edit
PERSONNEL_SHEET_NAME=Tuần này
```

## 4. First deploy behavior

On startup, `render_start.py` checks the persistent disk:

1. If `/var/data/ff-know-ai/chroma_db` exists, it starts Streamlit immediately.
2. If bundled `data_documents` or `chroma_db` exist in the repo, it copies them
   to the persistent disk.
3. It starts Streamlit without rebuilding the Vector DB during service startup.
   This is important on Render because the web service must bind to `$PORT`
   quickly or the deploy is marked as failed.
4. If no Vector DB exists yet, open the app and use the SharePoint sync/index
   button in the sidebar.

The first Vector DB creation can be slow because it downloads SharePoint
documents and rebuilds embeddings, but it should happen after the web service is
already online.

If the app opens but shows **Chưa tìm thấy dữ liệu Vector DB**, confirm that the
Render service is using `python render_start.py` as its start command and that
all secret environment variables are set. The app also shows a
**Tạo Vector DB từ SharePoint** button when SharePoint credentials are present,
so you can trigger the initial sync directly from the web UI.

If Render logs show **Không tìm thấy thư mục SharePoint**, the app can
authenticate to Microsoft Graph but the configured folder path does not exist
from the app's permissions. Check the folder names in `SOURCE_FOLDERS` inside
`sharepoint_loader.py`, especially the first source:
`Quy chế, chính sách bộ phận/Mô hình BU`. The sync skips missing folders by
default with `SHAREPOINT_SKIP_MISSING_FOLDERS=true` and prints the top-level
folders it can see in each SharePoint drive.

## 5. Updating documents after deploy

Use the Streamlit sidebar button **Cap nhat tai lieu tu SharePoint** to sync
documents and rebuild the Vector DB on the persistent disk.

If Gemini quota is limited, use the download-only button first, then rebuild
later when quota is available.
