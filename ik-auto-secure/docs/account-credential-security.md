# Lưu trữ an toàn tài khoản game trên Windows

## Quyết định kiến trúc

Không lưu user/password trong `config.json`, source code, log, checkpoint,
screenshot, database control plane hoặc Git. Với tool chạy cục bộ dưới một tài
khoản Windows tương tác, kho lưu trữ mặc định là **Windows Credential Manager**
với một *Generic Credential* cho mỗi tài khoản game.

Credential Manager là Windows Vault dành cho username/password của ứng dụng.
Nếu cần fallback kỹ thuật, dùng **DPAPI theo user scope** (`CryptProtectData`)
để mã hóa một blob riêng trên đúng máy và đúng Windows user. Không dùng
`CRYPTPROTECT_LOCAL_MACHINE`: tùy chọn đó cho phép mọi user trên máy giải mã.

## Mô hình dữ liệu tối thiểu

`config.json` chỉ chứa metadata không bí mật:

```json
{
  "accounts": [
    {
      "accountId": "a4e92038-4e72-4fcb-a1c3-3d6583170d7e",
      "displayName": "Farm 01",
      "credentialTarget": "IKAutoSecure/account/a4e92038-4e72-4fcb-a1c3-3d6583170d7e",
      "browserProfileId": "farm-01",
      "enabled": true
    }
  ]
}
```

`credentialTarget` là định danh, không phải password, username, URL login hay
cookie. Username và password cùng nằm trong credential entry của Windows Vault.
`displayName` phải là alias, không dùng email/số điện thoại nếu không cần.

## Luồng thêm và dùng tài khoản

1. Dashboard mở form nhập user/password cục bộ; password input che ký tự và
   không autocomplete vào browser.
2. Client gửi secret thẳng tới local Browser Session Worker qua IPC cục bộ đã
   xác thực; không gửi qua WebSocket control plane hoặc ghi xuống event store.
3. Worker ghi Generic Credential vào Credential Manager với target theo format
   trên, rồi chỉ trả về `accountId`/metadata đã redaction.
4. Khi cần đăng nhập, worker đọc credential ngay trước bước login, dùng trong
   bộ nhớ ngắn hạn, điền vào page qua Playwright và xóa các object/buffer tham
   chiếu khi workflow kết thúc.
5. Log chỉ ghi `accountId`, `browserProfileId`, success/failure và error class.
   Không ghi input value, request body, URL có token, cookie hoặc stack trace
   chứa secret.

Ưu tiên giữ Chrome persistent profile đã đăng nhập. Chỉ dùng password để login
lại khi session hết hạn; không đọc hay xuất cookie ra khỏi profile.

## Phân quyền và ranh giới

- Chỉ `admin` được thêm, thay password hoặc xóa credential.
- `operator` chỉ được start/stop profile; API không trả username/password.
- `viewer` không thấy account mapping chi tiết.
- Browser Session Worker phải chạy dưới **cùng Windows user** đã tạo credential.
  Không chạy Windows Service bằng `LocalSystem` rồi cố đọc Vault của user.
- Web control plane không có database column chứa secret và không có endpoint
  `GET credential`.
- Nếu worker chạy trên máy khác, người vận hành nhập lại secret trên máy đó;
  không đồng bộ blob DPAPI hay Vault entry qua network.

## Quy tắc DPAPI fallback

Chỉ dùng DPAPI nếu Credential Manager API không đáp ứng use case. Blob phải có
version/schema, account id làm optional entropy, checksum/version rotation và
ACL chỉ cho Windows user chạy worker. Không tự tạo/master key trong source,
không dùng password làm khóa mã hóa, không dùng AES key đặt trong `.env` hay
`config.json`.

DPAPI theo user scope thường chỉ giải mã được bởi cùng user và cùng máy. Đây là
lý do backup không thể là copy file blob đơn giản; hướng khôi phục an toàn là
người quản trị nhập lại password trên máy mới.

## Hardening máy chạy tool

- Dùng Windows account riêng, password mạnh và khóa màn hình tự động.
- Bật BitLocker cho volume chứa Chrome profile, `.venv`, data và diagnostic.
- Không chia sẻ thư mục profile qua network; không chạy tool bằng tài khoản
  Windows dùng chung.
- Giữ Windows/Chrome cập nhật; hạn chế extension Chrome và quyền local admin.
- Bật Defender/EDR, rà malware trước khi nhập credential.
- Cấp NTFS ACL tối thiểu cho thư mục data; không lưu secret ở Desktop/Downloads.
- Diagnostic mặc định không capture network body, cookie hoặc form password.

## Rotation, xóa và incident response

- Khi đổi password: cập nhật Vault entry, invalidate browser session nếu game
  yêu cầu, audit actor/time nhưng không audit secret.
- Khi xóa tài khoản: stop profile lease, xóa Generic Credential, xóa browser
  profile theo quy trình xác nhận riêng và xóa diagnostic liên quan theo
  retention policy.
- Nếu nghi lộ: đổi password trên dịch vụ game trước, thu hồi session nếu có,
  xóa credential/profile local, kiểm tra log đã redaction và điều tra máy chạy
  tool. Không tin rằng mã hóa local thay thế cho việc đổi password.

## Checklist trước khi triển khai

- [ ] Test chứng minh `config.json`, log, event, snapshot và exception không
  chứa username/password/cookie/token.
- [ ] Test quyền: operator/viewer không thể read, overwrite hoặc delete secret.
- [ ] Test worker khác Windows user không thể dùng credential đã tạo.
- [ ] Test rotation và delete không để reference cũ còn dùng được.
- [ ] Test crash/restart không serialize secret vào checkpoint.
- [ ] Code review không có `CRYPTPROTECT_LOCAL_MACHINE`, static AES key hoặc
  password hard-code.

## Nguồn chính thức

- [Microsoft: Windows Credential Manager and Vault](https://learn.microsoft.com/en-us/windows-server/security/windows-authentication/credentials-processes-in-windows-authentication)
- [Microsoft: CryptProtectData / DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)
- [Microsoft: DPAPI scope and recovery limitations](https://learn.microsoft.com/en-us/windows/win32/seccrypto/example-c-program-using-cryptprotectdata)
