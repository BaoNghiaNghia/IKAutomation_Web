# IK Auto Secure

Windows desktop tool for operating separate Chrome profiles used with the IK
portal. It retains the profile manager, window controls, input synchronisation
and 2048 helper while making data collection explicit and bounded.

The browser-farming architecture and migration plan are documented in
[`docs/farm-automation-browser-spec.md`](docs/farm-automation-browser-spec.md).
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
và tạo shortcut **IK Auto** có icon trên Desktop. Icon xuất hiện ở file `.exe`,
shortcut, tiêu đề cửa sổ và taskbar.

This project is intended for authorised automation only. Review the target
service's terms and use separate profiles for separate accounts.
