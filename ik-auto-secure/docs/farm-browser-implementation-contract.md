# Farm browser — implementation contract

Tài liệu này khóa các invariant triển khai cho browser. Nguồn yêu cầu là
[`farm-automation-web-spec.md`](farm-automation-web-spec.md), đặc biệt các
mục 1.2, 1.3, 5, 6, 7 và 10; `farm-automation-browser-spec.md` là tài liệu
chuyển đổi adapter từ ADB sang browser/CDP.

## Luồng không được thay đổi

`Preflight → World Map → quét roster → chọn resource + level → mở/search panel
→ tìm resource → xác minh popup → chọn team → dispatch → xác minh march →
waiting`.

Mỗi cycle chỉ hoàn tất tối đa một dispatch đã xác minh. Fallback phải thử hết
level của resource hiện tại trước khi chuyển resource kế tiếp.

## Quy tắc input và vision

- Không input ở trạng thái `Unknown`.
- Mỗi tap: capture mới → rematch template → kiểm tra bounds → tap tâm bounds.
- Sau tap phải xác minh state/post-condition; tap thành công không đồng nghĩa
  game đã thành công.
- Chỉ dùng crop UI ổn định, ROI hẹp và bounds từ matcher; không click bằng tọa
  độ cứng, terrain, timer, power hay text động.
- Dialog/overlay có ưu tiên cao hơn state nền.
- Thiếu template cho một bước thì workflow chờ an toàn và lưu diagnostic; không
  suy đoán hoặc click tiếp.

## Kết quả và diagnostics

- Business outcome (không có đội/resource, kho đầy, hết resource/level): chờ
  hoặc fallback theo policy, không coi là lỗi hạ tầng.
- Technical failure (capture/matcher/timeout): retry bounded, backoff/recovery
  theo policy; mọi đường dừng phải lưu canvas gần nhất trước khi publish lỗi.
- Log phải chứa state, confidence/bounds, target resource/level/team và đường
  dẫn screenshot diagnostic. Screenshot có retention/quota cục bộ.

## Quy tắc cho thay đổi tiếp theo

Mọi bước Auto Farm mới phải bổ sung theo thứ tự trên, có template dương, điều
kiện trước/sau click, log và test regression. Thay đổi UI/dashboard, viewport
hoặc adapter Playwright/CDP không được thay đổi state machine hay nới lỏng các
quy tắc an toàn này.
