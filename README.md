# Hướng Dẫn Cài Đặt & Chạy AI Agent Tra Cứu Nội Bộ FF

Dự án này là AI Agent nội bộ dùng LangChain, ChromaDB và Streamlit để tải, lập chỉ mục và trả lời câu hỏi dựa trên tài liệu SharePoint của Flexfit.

## 1. Cài đặt môi trường

Yêu cầu Python 3.10 trở lên.

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Cấu hình `.env`

Kiểm tra các biến sau trong file `.env`:

```env
GEMINI_API_KEY="..."
SHAREPOINT_CLIENT_ID="..."
SHAREPOINT_TENANT_ID="..."
SHAREPOINT_CLIENT_SECRET="..."
SHAREPOINT_HOST="flexfitcom.sharepoint.com"
SHAREPOINT_SITE_PATH="/sites/Intranet"
SHAREPOINT_SITE_NAME="Intranet"
```

Nếu muốn app tự kiểm tra SharePoint khi khởi động, thêm:

```env
AUTO_SYNC_ON_START=true
AUTO_SYNC_INTERVAL_MINUTES=60
```

## 3. Nguồn tài liệu đang đồng bộ

`sharepoint_loader.py` đang tải tài liệu từ 4 nhóm nguồn:

- Cơ chế lương: `Quy chế, chính sách bộ phận/Mô hình BU`
- Quy định chung: `Quy chế và chính sách chung công ty`
- Biểu mẫu: `Các biểu mẫu thường dùng`
- Bộ kit nhân sự mới: `Bộ kit nhân viên mới`

Các định dạng được tải về để người dùng có thể tra cứu/tải file: `.pdf`, `.doc`, `.docx`, `.txt`, `.xls`, `.xlsx`, `.ppt`, `.pptx`, `.pptm`.

Các định dạng được lập chỉ mục nội dung trong Vector DB: `.pdf`, `.docx`, `.txt`, `.xlsx`.

## 4. Cập nhật tài liệu và Vector DB

Chạy một lệnh duy nhất:

```bash
python sync_documents.py
```

Lệnh này sẽ:

1. Tải tài liệu mới nhất từ 4 nguồn SharePoint.
2. Ghi manifest tại `data_documents/_sharepoint_manifest.json`.
3. Tự rebuild `chroma_db` nếu phát hiện file mới hoặc file thay đổi.

Nếu cần ép rebuild index:

```bash
python sync_documents.py --force-index
```

Nếu chỉ cần tải đủ file SharePoint nhưng chưa rebuild index, ví dụ khi Gemini embedding đang hết quota:

```bash
python sync_documents.py --skip-index
```

## 5. Chạy giao diện chat

```bash
streamlit run app.py
```

Giao diện chạy tại `http://localhost:8501`. Trong sidebar có nút **Cập nhật tài liệu từ SharePoint** để tải và index lại ngay từ app.

## 6. Tự động cập nhật định kỳ

Có thể dùng Windows Task Scheduler để chạy:

```bash
python "D:\FF KNOW AI\sync_documents.py"
```

Khi có tài liệu mới trên các thư mục nguồn SharePoint, script sẽ tải lại và rebuild Vector DB để câu trả lời dùng nội dung mới nhất.
