# Farm automation browser — thiết kế Phase 0

Đặc tả triển khai hiện hành là
[`farm-automation-browser-spec.md`](farm-automation-browser-spec.md). Bản
LDPlayer/ADB được giữ nguyên để tham chiếu lịch sử, không phải roadmap hiện tại.

## Ranh giới triển khai

```text
Web control plane -> validated command -> Browser Session Worker
Browser Session Worker -> Chrome profile/CDP -> Playwright/CDP -> game web
Browser Session Worker -> sequenced event/checkpoint -> dashboard read model
```

Web dashboard không có API click, touch, CDP endpoint hoặc cookie access. Chỉ
Browser Session Worker có profile lease và quyết định browser input từ page/frame
mới đã xác minh.

## Bất biến

- Một `browserProfileId` có tối đa một profile lease active.
- Mỗi command có idempotency key, expected version và deadline.
- Snapshot mang `workerId`, `runId`, `profileId`, `sequence`; state cũ không
  được ghi đè state mới.
- Restart chỉ restore metadata; browser runtime luôn bắt đầu lại `preflight`.
- Một cycle tối đa một verified dispatch và luôn release/yield gameplay gate.

## Browser input guard

```text
guardedInput(profileLease, expectedState, templateId, postCondition):
  cancellation.throwIfCancelled()
  frame = captureFreshPageOrCanvas(maxAge=500ms)
  assert detectState(frame) == expectedState
  match = vision.match(frame, templateId)
  assert match.confidence >= threshold && match.bounds.isValid
  sendPlaywrightOrCDPInput(match.bounds.center)
  after = captureFreshPageOrCanvas(maxAge=500ms)
  assert postCondition(after)
```

Unknown state, stale frame, weak match, cancelled operation, timeout hoặc
post-condition failure đều dừng bước hiện tại mà không thêm input mù.

## Các bước tiếp theo

1. Đổi các contract `Device`/`Agent` hiện tại thành `BrowserProfile`/
   `BrowserSessionWorker` và loại bỏ configuration LDPlayer/DPI/ADB.
2. Dùng fake browser adapter để test profile lease, cancellation, checkpoint,
   idempotency và event sequence.
3. Kết nối adapter Playwright/CDP hiện có để discover/health/capture browser.
4. Sau khi worker ổn định, xây API/RBAC, broker và dashboard.
