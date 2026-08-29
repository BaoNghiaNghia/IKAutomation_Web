# IK Auto — Browser Control

Ứng dụng Windows quản lý nhiều Chrome profile riêng biệt cho IK: mở/đóng
profile theo nhóm, sắp xếp cửa sổ, Auto Farm, giám sát thông báo và đồng bộ
chuột - bàn phím. Toàn bộ mã nguồn nằm trực tiếp tại thư mục gốc của
repository này; không còn thư mục dự án lồng nhau.

## Chức năng chính

- Mỗi tài khoản dùng một Chrome profile và dữ liệu cục bộ riêng.
- Mở, đóng và sắp xếp nhiều cửa sổ theo hàng; thao tác được điều tiết để tránh
  tăng tải GPU đột ngột.
- Auto Farm và giám sát thông báo theo nhóm profile, với log/ảnh debug khi một
  bước không được xác minh.
- Đồng bộ chuột - bàn phím giữa profile master và các profile đã chọn.
- Có thể gửi thông báo trạng thái và cảnh báo qua Telegram.

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

The browser-farming architecture and migration plan are documented in
[`docs/farm-automation-browser-spec.md`](docs/farm-automation-browser-spec.md).
The non-negotiable browser-farm workflow and safety invariants are recorded in
[`docs/farm-browser-implementation-contract.md`](docs/farm-browser-implementation-contract.md).
Credential-storage requirements are documented in
[`docs/account-credential-security.md`](docs/account-credential-security.md).

## Safety defaults

- Only `http`/`https` URLs whose hostname matches `capture.allowed_hosts` are
  opened or observed. Matching is hostname-based, so a URL that merely contains
  an allowed domain in its path or query is rejected.
- Network/WebSocket/message capture and response-body capture are disabled by
  default. Turn both on only for a short, supported diagnostic session and
  treat the generated logs as sensitive.
- JSONL logs rotate at 5 MB with three retained backups. Snapshots retain only
  the configured newest files (`capture.snapshot_retention`, default `50`).
- Chrome automation-identifying banners are not hidden by default. The tool
  does not attempt to bypass game controls, access restrictions, or terms.
- Browser profiles contain login cookies. They are excluded from Git; keep the
  `data` folder on an encrypted, access-controlled Windows account.

## Run

Double-click `run.cmd`, or run it from Command Prompt. It performs all setup
only when needed, checks Chrome/configuration, runs the browser-worker test,
then opens the dashboard. On the first run it creates `.venv`, installs missing
packages, and creates `config.json` from `config.example.json`. If a compatible
Python is missing, it installs Python 3.13 through Windows `winget` first.

Run tests from an activated virtual environment:

```powershell
python -m pytest -q
```

## Debug Auto Farm

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
và tạo shortcut **IK Auto** có icon trên Desktop. Build đã loại Qt multimedia,
PDF, QML/WebEngine không dùng và codec video OpenCV; các thành phần bắt buộc như
Chrome CDP/Playwright, OpenCV nhận diện ảnh và Qt UI vẫn được giữ nguyên.

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

This project is intended for authorised automation only. Review the target
service's terms and use separate profiles for separate accounts.
