# Browser control-plane design

## Phạm vi

Control plane quản lý ý định, cấu hình, audit, event và read model cho các
Chrome profile. Nó không giữ CDP session, cookie, Playwright `Page` hoặc quyền
gửi browser input.

## Domain contracts đã có

`ik_chrome_auto.browser_control` hiện cung cấp:

- Versioned farm-profile validation.
- Command role, idempotency key, deadline và optimistic version.
- State machine profile run và profile lease value object.
- Snapshot projector chống event duplicate/stale.

`SqliteBrowserControlStore` cung cấp implementation local có transaction cho command,
audit, ownership và event sequence. Production sẽ thay bằng PostgreSQL cùng
contract repository; browser worker không được có database credential.

## Điều chỉnh còn thực hiện

`browser_worker.py` quản lý `BrowserProfile`, profile lease, cancellation và
checkpoint. Bước kế tiếp nối adapter Playwright/CDP hiện có vào worker.

## API target

API chỉ tạo command đã validate và stream state read-only:

```text
POST /api/v1/farm-runs
POST /api/v1/farm-runs/{runId}/stop
GET  /api/v1/browser-profiles
POST /api/v1/browser-profiles/{profileId}/pause
POST /api/v1/browser-profiles/{profileId}/resume
GET  /api/v1/farm-runs/{runId}/events?afterSequence=...
WS   /api/v1/stream
```

OIDC/RBAC, worker identity, broker and PostgreSQL schema remain prerequisites
for exposing these endpoints outside local development.
