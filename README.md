# IK Auto — Browser Control

Ứng dụng Windows quản lý nhiều Chrome profile riêng biệt cho IK: mở/đóng
profile theo nhóm, sắp xếp cửa sổ, Auto Farm, giám sát thông báo và đồng bộ
chuột - bàn phím. Toàn bộ mã nguồn nằm trực tiếp tại thư mục gốc của kho mã
này; không còn thư mục dự án lồng nhau.

## Chức năng chính

- Mỗi tài khoản dùng một Chrome profile và dữ liệu cục bộ riêng.
- Mở, đóng và sắp xếp nhiều cửa sổ theo hàng; thao tác được điều tiết để tránh
  tăng tải GPU đột ngột.
- Auto Farm và giám sát thông báo theo nhóm profile, với log/ảnh debug khi một
  bước không được xác minh.
- Đồng bộ chuột - bàn phím giữa profile master và các profile đã chọn.
- Có thể gửi thông báo trạng thái và cảnh báo qua Telegram.

## Độ phân giải khi chạy Automation

Khi xếp 5 profile trên một hàng, canvas hiển thị của mỗi cửa sổ có thể chỉ còn
khoảng `366×168`. Phóng to ảnh chụp từ kích thước này không tạo ra thêm chi
tiết, nên không đủ an toàn để nhận diện đội, nút Farm hoặc thư Chiến đấu.

Vì vậy Farm và Giám sát dùng chung một **renderer tạm 1280×720**:

1. Khi một profile đến lượt, tool lưu vị trí/kích thước ô lưới hiện tại.
2. Chỉ profile đó được nâng renderer thực lên `1280×720`, sau đó ảnh CDP và
   thao tác X/Y tỷ lệ đều dùng đúng canvas độ phân giải cao này.
3. Hoàn tất lượt Giám sát hoặc khi Farm chuyển sang thời gian chờ dài, profile
   được trả về chính xác ô lưới cũ.
4. Tại một thời điểm chỉ có **một** renderer 1280×720. Các profile còn lại chờ
   lượt, tránh việc một nhóm 5 profile tạo đồng thời năm bề mặt WebGL 720p và
   làm GPU tăng đột ngột.

Nhóm profile vẫn dùng chính sách điều phối hiện có; riêng việc dựng ảnh độ phân
giải cao được tuần tự hóa để ưu tiên độ chính xác và độ ổn định của máy.

## Cấu trúc thư mục

```text
src/        Mã nguồn ứng dụng
tests/      Bộ kiểm thử
scripts/    Script cài Python và tạo bản phát hành
docs/       Thiết kế, giới hạn và hướng dẫn kỹ thuật
data/       Dữ liệu cục bộ, log và ảnh debug (không commit)
release/    Bản build Windows (không commit)
```

## Yêu cầu

- Windows 10/11 và Google Chrome.
- Python 3.11, 3.12 hoặc 3.13. `run.cmd` sẽ thử cài Python 3.13 khi máy chưa
  có bản phù hợp.

Kiến trúc và kế hoạch chuyển đổi Auto Farm được mô tả trong
[`docs/farm-automation-browser-spec.md`](docs/farm-automation-browser-spec.md).
Quy trình bắt buộc và các nguyên tắc an toàn của Farm nằm trong
[`docs/farm-browser-implementation-contract.md`](docs/farm-browser-implementation-contract.md).
Yêu cầu lưu trữ thông tin đăng nhập nằm trong
[`docs/account-credential-security.md`](docs/account-credential-security.md).

## Mặc định an toàn

- Chỉ mở hoặc quan sát URL `http`/`https` có hostname khớp với
  `capture.allowed_hosts`. Việc so khớp dựa trên hostname; URL chỉ chứa domain
  được phép trong path hoặc query sẽ bị từ chối.
- Việc ghi nhận mạng/WebSocket/tin nhắn và nội dung phản hồi mặc định bị tắt.
  Chỉ bật trong phiên chẩn đoán ngắn và xem log sinh ra là dữ liệu nhạy cảm.
- Log JSONL tự xoay vòng ở 5 MB và giữ ba bản sao lưu. Ảnh chụp chỉ giữ số file
  mới nhất theo `capture.snapshot_retention` (mặc định `50`).
- Công cụ không ẩn banner nhận diện tự động hóa của Chrome và không cố vượt qua
  cơ chế kiểm soát, hạn chế truy cập hoặc điều khoản của game.
- Profile trình duyệt có thể chứa cookie đăng nhập. Chúng bị loại khỏi Git; hãy
  giữ thư mục `data` trong tài khoản Windows được mã hóa và kiểm soát truy cập.

## Chạy ứng dụng

Double-click `run.cmd` hoặc chạy từ Command Prompt. Script chỉ thiết lập khi
cần, kiểm tra Chrome/cấu hình, chạy kiểm thử browser worker rồi mở dashboard.
Ở lần chạy đầu, script tạo `.venv`, cài dependency còn thiếu và tạo
`config.json` từ `config.example.json`. Nếu chưa có Python tương thích, script
sẽ thử cài Python 3.13 qua Windows `winget` trước.

Chạy toàn bộ kiểm thử từ môi trường ảo đã kích hoạt:

```powershell
python -m pytest -q
```

## Chẩn đoán Auto Farm

Khi bấm **Farm**, tool ghi log riêng theo từng profile tại
`data\logs\farm-<profile-id>.jsonl`. Mỗi dòng có thời gian, trạng thái game,
kích thước canvas, điểm khớp City/World Map và thao tác đã được xác minh.

Nếu chưa nhận diện được game hoặc có lỗi an toàn, ảnh canvas CDP tương ứng được
lưu tại `data\screenshots\<profile-id>\farm-debug`. Tool chỉ giữ 10 ảnh mới
nhất/profile; log Auto Farm tự xoay vòng ở 2 MB với 2 bản sao lưu. Khi cần hỗ
trợ, gửi file log cùng ảnh debug mới nhất; không gửi thư mục profile Chrome hay
file credential.

## Build ứng dụng Windows

Sau khi chạy `run.cmd` một lần, double-click [`build.cmd`](build.cmd). Script sẽ
kiểm thử, tạo ứng dụng không có cửa sổ Terminal tại `release\IK Auto\IK Auto.exe`
và tạo shortcut **IK Auto** có icon trên Desktop. Bản build đã loại Qt
multimedia, PDF, QML/WebEngine không dùng và codec video OpenCV; các thành phần
bắt buộc như Chrome CDP/Playwright, OpenCV nhận diện ảnh và giao diện Qt vẫn
được giữ nguyên.

Với bản build mới, cài đặt lần đầu lưu `config.json`, profile Chrome, ảnh và log
tại `%LOCALAPPDATA%\IK Auto`, không nằm trong thư mục `release`. Vì vậy có thể
thay thư mục release khi nâng cấp mà không đóng gói cookie/profile vào bản phát
hành. Bản portable cũ có `config.json` đặt cạnh `.exe` vẫn tiếp tục dùng dữ liệu
cũ để không mất profile.

Để tạo thêm một file nén thuận tiện gửi cho máy khác, chạy:

```powershell
.\build.cmd -Archive
```

File phát hành sẽ là `release\IK-Auto-portable.zip`; người nhận giải nén toàn bộ
thư mục trước khi chạy `IK Auto.exe`.

Dự án chỉ dành cho hoạt động tự động hóa được cho phép. Hãy xem điều khoản của
dịch vụ mục tiêu và sử dụng profile tách biệt cho từng tài khoản.
