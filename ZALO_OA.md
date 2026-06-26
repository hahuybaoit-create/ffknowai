# Zalo Official Account Webhook

Module Zalo OA chay nhu mot web service rieng va dung lai `ask_agent()` de tra loi theo Vector DB hien co.

## Bien moi truong

```env
ZALO_OA_ACCESS_TOKEN="..."
ZALO_WEBHOOK_SECRET="chuoi-bi-mat-tu-dat"
ZALO_ALLOWED_EVENTS="user_send_text"
ZALO_MAX_REPLY_CHARS=1800
```

`ZALO_WEBHOOK_SECRET` la chuoi bi mat tu dat. Khi cau hinh webhook tren Zalo OA, them secret vao query string de chan request khong mong muon.

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
ZALO_WEBHOOK_SECRET=...
FF_APP_DATA_ROOT=/var/data/ff-know-ai
```

Webhook service can truy cap cung thu muc `chroma_db` voi app chat. Neu deploy thanh service rieng, hay gan cung persistent disk hoac copy Vector DB sang service webhook.
