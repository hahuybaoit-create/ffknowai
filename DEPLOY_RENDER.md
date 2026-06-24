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
- Persistent disk: `/var/data`

Render's default Python version can change for newly created services. This repo
pins Python with both `PYTHON_VERSION=3.11.9` in `render.yaml` and a
`.python-version` file so dependencies such as `pandas` install from compatible
prebuilt wheels instead of compiling from source.

## 3. Add secret environment variables

In the Render service environment settings, fill these values:

```text
GEMINI_API_KEY=...
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
```

## 4. First deploy behavior

On startup, `render_start.py` checks the persistent disk:

1. If `/var/data/ff-know-ai/chroma_db` exists, it starts Streamlit immediately.
2. If bundled `data_documents` or `chroma_db` exist in the repo, it copies them
   to the persistent disk.
3. If no Vector DB exists but SharePoint credentials are configured, it syncs
   SharePoint and builds the index.
4. If no documents and no SharePoint credentials exist, startup fails with a
   clear error.

The first deploy can be slow if it needs to download SharePoint documents and
rebuild embeddings.

## 5. Updating documents after deploy

Use the Streamlit sidebar button **Cap nhat tai lieu tu SharePoint** to sync
documents and rebuild the Vector DB on the persistent disk.

If Gemini quota is limited, use the download-only button first, then rebuild
later when quota is available.
