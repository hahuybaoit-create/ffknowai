# Cập nhật service Render đang chạy

Tài liệu này áp dụng cho service `ffknowai-1`, repository
`hahyubaoit-create/ffknowai`, nhánh deploy `main`.

## 1. Giữ lại phiên bản cũ

Trên GitHub, mở repository và tạo branch từ `main` hiện tại:

```text
backup-before-personnel-lookup
```

Không xóa branch này. Render cũng giữ các deploy cũ để có thể dùng nút
**Rollback** trong trang Events.

## 2. Tạo branch cập nhật

Tạo branch mới từ `main`:

```text
feature/personnel-lookup
```

Đưa các file mới/cập nhật trong workspace lên branch này. Các file bắt buộc cho
tính năng tra cứu nhân sự là:

```text
agent.py
personnel_lookup.py
requirements.txt
```

Các file nên cập nhật cùng để cấu hình và khởi tạo Render an toàn:

```text
app.py
render.yaml
README.md
DEPLOY_RENDER.md
```

Không upload `.env`, `chroma_db/`, `chroma_db_tmp/` hoặc `data_documents/` lên
GitHub.

## 3. Kiểm tra Environment trên Render

Giữ nguyên toàn bộ biến môi trường cũ. Thêm hoặc kiểm tra các biến sau:

```text
PERSONNEL_GOOGLE_SHEET_URL=https://docs.google.com/spreadsheets/d/1SLIC1gLgD6z7_LhIkxXA8mHX5RBv-cKn/edit?gid=820290981#gid=820290981
PERSONNEL_SHEET_NAME=Tuần này
```

Khuyến nghị bảo vệ dữ liệu nội bộ bằng:

```text
REQUIRE_APP_PASSWORD=true
APP_ACCESS_PASSWORD=<mật khẩu mạnh>
```

Nếu chưa muốn thay đổi cách người dùng truy cập app cũ, tạm thời không thêm
`REQUIRE_APP_PASSWORD`; mặc định của code vẫn giữ hành vi truy cập cũ.

Không thay đổi hoặc xóa các biến sau:

```text
GEMINI_API_KEY
SHAREPOINT_CLIENT_ID
SHAREPOINT_TENANT_ID
SHAREPOINT_CLIENT_SECRET
FF_APP_DATA_ROOT
```

## 4. Deploy không ảnh hưởng phiên bản đang chạy

1. Trong Render, mở **Settings** và tạm tắt **Auto-Deploy** nếu đang bật.
2. Push/upload code lên branch `feature/personnel-lookup`.
3. Tạo một service Preview/Staging từ branch này nếu tài khoản Render hỗ trợ.
4. Kiểm tra staging bằng các câu:
   - `Bảo là ai?`
   - `Tra cứu nhân viên Bảo`
   - `Bảo làm ở phòng nào?`
   - `Quy định nghỉ phép là gì?`
5. Khi staging đạt, merge `feature/personnel-lookup` vào `main`.
6. Tại service `ffknowai-1`, chọn **Manual Deploy > Deploy latest commit**.
7. Theo dõi Logs đến khi thấy Streamlit chạy và deploy chuyển sang **Live**.

Nếu không có Preview/Staging, vẫn có thể merge vào `main` khi Auto-Deploy đã
tắt, sau đó dùng **Manual Deploy**. Phiên bản cũ tiếp tục phục vụ cho đến khi bản
mới build thành công.

## 5. Kiểm tra sau deploy

Kiểm tra lần lượt:

1. Trang web mở bình thường.
2. Tài liệu cũ vẫn trả lời được.
3. `Bảo là ai?` trả thông tin từ danh sách nhân sự, không còn trả
   `Thông tin này hiện chưa có trong hệ thống`.
4. Câu hỏi không liên quan nhân sự vẫn đi qua luồng chatbot cũ.
5. Không có thông tin nhạy cảm như số điện thoại cá nhân, CCCD, tài khoản ngân
   hàng, địa chỉ hoặc lương trong câu trả lời.

## 6. Rollback nếu có lỗi

Trong Render, mở **Events**, tìm deploy đang chạy ổn trước đó và chọn
**Rollback**. Sau đó kiểm tra Logs và URL production. Không xóa persistent disk;
disk đang giữ tài liệu và Vector DB của phiên bản cũ.

