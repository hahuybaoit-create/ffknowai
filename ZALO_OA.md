# Zalo Official Account Webhook

Module Zalo OA chay nhu mot web service rieng va dung lai `ask_agent()` de tra loi theo Vector DB hien co.

## Bien moi truong

```env
ZALO_OA_ACCESS_TOKEN="..."
ZALO_OA_REFRESH_TOKEN="..."
ZALO_APP_ID="..."
ZALO_APP_SECRET="..."
ZALO_WEBHOOK_SECRET="chuoi-bi-mat-tu-dat"
ZALO_ALLOWED_EVENTS="user_send_text"
ZALO_MAX_REPLY_CHARS=1800
ZALO_VERIFY_META_CONTENT="noi-dung-content-zalo-cap"
ZALO_ALLOWED_USER_IDS="user_id_1,user_id_2"
FILE_DOWNLOAD_SECRET="chuoi-bi-mat-de-tai-file"
PUBLIC_BASE_URL="https://ffknowai-1.onrender.com"
APP_ACCESS_PASSWORD="mat-khau-vao-giao-dien-web"
```

`ZALO_WEBHOOK_SECRET` la chuoi bi mat tu dat. Khi cau hinh webhook tren Zalo OA, them secret vao query string de chan request khong mong muon.

`ZALO_OA_ACCESS_TOKEN` se het han theo vong doi token cua Zalo. Nen cau hinh them
`ZALO_OA_REFRESH_TOKEN`, `ZALO_APP_ID`, va `ZALO_APP_SECRET` de service tu refresh
token roi retry mot lan khi Zalo tra loi `Access token has expired`.

## Chay local

```bash
uvicorn zalo_webhook_app:app --host 0.0.0.0 --port 8000
```

Kiem tra health check:

```text
http://127.0.0.1:8000/healthz
```

## Cau hinh webhook tren Zalo OA

Webhook URL:

```text
https://<domain-cua-ban>/zalo/webhook?secret=<ZALO_WEBHOOK_SECRET>
```

Neu muon chi cho mot so nguoi dung Zalo duoc hoi chatbot, them cac `user_id` vao `ZALO_ALLOWED_USER_IDS`, cach nhau bang dau phay. De trong bien nay thi moi nguoi nhan OA deu co the hoi.

Chatbot se gui kem muc `File lien quan`. Tren Zalo, neu co `PUBLIC_BASE_URL` va `FILE_DOWNLOAD_SECRET`, moi file se co link tai xuong dang `/files/...`.

## Xac thuc domain Zalo

Neu dung domain Render dang co, vi du:

```text
ffknowai-1.onrender.com
```

nen chon cach xac thuc bang the meta trong Zalo Developers.

Zalo se hien mot the tuong tu:

```html
<meta name="zalo-platform-site-verification" content="..." />
```

Hay copy rieng gia tri trong `content="..."` va dat vao bien moi truong:

```env
ZALO_VERIFY_META_CONTENT="..."
```

Sau khi deploy lai, mo trang goc:

```text
https://ffknowai-1.onrender.com/
```

Trang HTML se tu chen meta:

```html
<meta name="zalo-platform-site-verification" content="..." />
```

Sau do quay lai Zalo Developers va bam `Xac thuc`.

Khi nguoi dung nhan tin text cho OA, webhook se:

1. Nhan payload tu Zalo OA.
2. Lay `sender.id` lam `user_id`.
3. Lay `message.text` lam cau hoi.
4. Goi `ask_agent(cau_hoi)`.
5. Gui cau tra loi ve user qua Zalo OA API.

## Trien khai tren Render

Nen tao mot Render Web Service rieng cho webhook Zalo OA.

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn zalo_webhook_app:app --host 0.0.0.0 --port $PORT
```

Can cau hinh cac bien moi truong giong service Streamlit, toi thieu:

```env
GEMINI_API_KEY=...
ZALO_OA_ACCESS_TOKEN=...
ZALO_OA_REFRESH_TOKEN=...
ZALO_APP_ID=...
ZALO_APP_SECRET=...
ZALO_WEBHOOK_SECRET=...
FF_APP_DATA_ROOT=/var/data/ff-know-ai
```

Neu them Disk tren Render, nhap **Mount Path** la:

```text
/var/data
```

Code hien tai dung `FF_APP_DATA_ROOT=/var/data/ff-know-ai`, nen cac thu muc
`data_documents`, `chroma_db`, va `chroma_db_tmp` se nam ben trong disk do.
Webhook service can truy cap cung thu muc `chroma_db` voi app chat. Neu deploy
thanh service rieng, hay gan cung persistent disk hoac copy Vector DB sang
service webhook.

## Tao du lieu Vector DB tren Render

Sau khi deploy webhook service va cau hinh Environment, URL status chi de kiem
tra trang thai:

```text
https://<domain-cua-ban>/admin/sync/status?secret=<ADMIN_SYNC_SECRET>
```

Neu JSON tra ve `state: "idle"`, `data_dir_exists: false` va
`chroma_db_exists: false`, nghia la chua co job sync nao chay. Mo URL nay de
bat dau tai SharePoint va build Vector DB:

```text
https://<domain-cua-ban>/admin/sync?secret=<ADMIN_SYNC_SECRET>
```

Sau do quay lai URL `/admin/sync/status?...` de theo doi. Khi sync thanh cong,
`sync.state` se la `done`, `data_dir_exists` va `chroma_db_exists` se la `true`.

Neu secret da tung bi chup man hinh hoac chia se, hay doi lai
`ADMIN_SYNC_SECRET`/`ZALO_WEBHOOK_SECRET` trong Render Environment roi redeploy.
