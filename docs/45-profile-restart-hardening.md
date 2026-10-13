# IK Auto — kế hoạch vận hành an toàn 45 Chrome profiles

**Ngày rà soát:** 2026-10-12
**Workspace:** `D:\Bot_Tool_Auto_Game\IKAutomation_Web`
**Mục tiêu:** Người dùng có thể đóng và mở lại IK Auto bất kỳ lúc nào, trong khi tối đa 45 cửa sổ Chrome vẫn giữ nguyên profile, tài khoản, tab và nội dung game. Khi IK Auto mở lại, người dùng có thể bật Sync 1 master → 44 followers, Auto Farm và Monitor trên các profile đã xác minh Ready.

## Các bất biến an toàn (P0)

1. **Không tự kill / restart Chrome trong khởi động lại IK Auto.** Startup chỉ sử dụng `ATTACH`. `repair_existing_browser_without_cdp` mặc định không cho phép restart; hành vi kết thúc Chrome cần một hành động cho phép riêng.
2. **Không gắn CDP theo cổng đơn thuần.** Một endpoint chỉ được dùng khi listener PID/Chrome ancestor xác minh được `--user-data-dir` chính xác của profile. Thiếu quyền truy vấn process ⇒ `unknown` ⇒ không được điều khiển.
3. **Không mở trùng profile cũ.** Khi process Chrome cũ đang sử dụng cùng `user-data-dir` nhưng không có CDP hợp lệ, lệnh OPEN bị chặn; yêu cầu Retry hoặc xử lý thủ công.
4. **Không chạy automation trên kết nối chưa Ready.** Sync xác minh master + toàn bộ followers; không gửi một thao tác đến tập con thiếu profile. Farm/Monitor chỉ dùng những session attached.
5. **Giữ dữ liệu hiện có:** không reset/stash thay đổi Git, không ghi đè `data/profiles`, không tự bật Farm/Sync sau restart, không ghi mật khẩu/token vào log.
6. **Một dashboard điều khiển:** Release giữ cùng Windows mutex `Local\IKAutoBrowserDashboard` như Development CMD.

## Giai đoạn 1 — P0: Bảo vệ Chrome, xác minh danh tính

**Đã triển khai trong local Live Project:**
- Chuyển startup `REPAIR_ATTACH` → `ATTACH`.
- Đổi `repair_existing_browser_without_cdp(allow_restart=False)`; chỉ khi người dùng chủ động cho phép xử lý thủ công mới có khả năng gọi code restart.
- Validate identity từ PID listener hoặc ancestry của process Chrome. `identity=unknown` không được coi là match.
- Chặn mở Chrome mới khi Chrome cũ của cùng profile vẫn tồn tại nhưng không có CDP.
- Thêm test cổng của profile lạ, PID ancestry, endpoint unknown, bảo vệ chống khởi chạy trùng và kiểm thử quy mô 45 profile.

**Gate đạt:** không có `taskkill` tự động trong quá trình mở lại tool; không có sự kiện Sync/Farm gửi đến sai profile.

## Giai đoạn 2 — P1: Scheduler reconnect và UI

**Đã triển khai:**
- Reconnect tối đa 4 profile song song; mỗi profile có deadline `max(20s, min(120s, startup_timeout + 10s))`, mặc định ~100s.
- Một profile timeout chỉ được đánh dấu unresolved, không đóng Chrome và không ngăn scheduler tiếp tục.
- UI hiển thị `CDP attached/total`, `pending` và có nút `Retry CDP` khi còn profile chưa kết nối.
- Sync bị khóa trong lúc reconnect đang chạy; sau khi xong, mỗi session vẫn phải được kiểm tra attached trước khi vào danh sách điều khiển.

**Việc cần kiểm thử trên máy chạy 45 profile:**
- Kiểm tra 45/45 configured, process Chrome, PID, port CDP, window và attached; chứng minh không có hai profile trỏ cùng PID/port hoặc sai `user-data-dir`.
- Cho các profile timeout/mất CDP thử lại theo sự cho phép của người dùng; không tự đóng chúng.
- Ghi timestamp từ start → attached cho từng profile; đo P50/P95 reconnect thực tế rồi điều chỉnh concurrency từ 4 xuống 3 nếu RAM/GPU bị tải cao.

## Giai đoạn 3 — P1/P2: vòng đời ứng dụng và updater

**Đã triển khai:**
- Dev CMD đã có mutex; Release `.exe` dùng lại cùng tên mutex để ngăn dashboard cạnh tranh.
- Trước shutdown, worker bỏ các command chưa xử lý, ưu tiên SHUTDOWN, nhả renderer về grid compact rồi detach Playwright. Chrome process không bị kết thúc.
- Grace period shutdown chung được tăng lên 2.5 giây và phân bổ công bằng cho nhiều worker. Có `runner_shutdown_result` để nhận biết profile còn dọn dẹp khi process thoát.
- Startup updater có `-SkipProfileSync`, không cập nhật trong phiên đang chạy, không ghi đè working tree Git đang dirty.

**Còn cần tích hợp/kiểm chứng:** kiểm tra launcher Release mutex trên Windows thật; kiểm thử updater trên một clean test checkout với Chrome vẫn mở, xác nhận dữ liệu Chrome không đổi; kiểm tra fallback nếu build GitHub thất bại.

## Giai đoạn 4 — P2: xác nhận vận hành end-to-end

1. Kiểm tra trạng thái hệ thống/memory trước khi chạy 45 (không tự mở 45 Chrome trên máy chưa chuẩn bị).
2. Chạy 5 chu kỳ: 45 Chrome đang mở → đóng riêng tool → xác nhận Chrome còn mở → mở lại tool → check 45/45 attached.
3. Test Sync với 1 master và 44 followers: ít nhất 100 thao tác click/drag/key/wheel kiểm tra ack và kết quả trên follower; 0 identity mismatch, 0 gửi nhầm.
4. Tắt Sync, chạy Farm từ Ready, kiểm chứng GUI/state trên từng profile, renderer lease và việc trở về grid compact.
5. Tắt Farm và chạy Monitor nhiều chu kỳ: không starvation, không gửi cảnh báo trùng, không lỗi reader/CDP.
6. Giả lập một Chrome không có CDP/port thay đổi: phải tiếp tục kết nối các Chrome còn lại, để profile lỗi ở trạng thái Retry, không taskkill.
7. Mở tool lần hai: chỉ một dashboard; auto-update test qua restart: không mất browser, config hoặc session.
8. Kiểm tra log tại `data/logs/events.jsonl`, `data/logs/lifecycle.jsonl`, `data/logs/updater.jsonl` và theo từng profile Farm.

**Điều kiện nghiệm thu:** trong 5 chu kỳ, không có Chrome bị đóng bởi tool; mỗi lần reconnect thành công đều đúng account/PID/CDP; 45/45 khi các cổng CDP khả dụng; Sync/Farm/Monitor chạy sau reconnect mà không cần mở Chrome mới.

## Lệnh kiểm tra source

```bat
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
git diff --check
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-release.ps1 -NoDesktopShortcut -SkipProfileSync
```

**Giới hạn hiện tại:** Cấu hình đang được nhìn thấy trên máy `BaoNghia` có 16 profile; kết quả 45 profile ở tầng kiểm thử mô phỏng chưa thay thế bằng chứng 45 Chrome đang vận hành thật. Việc kiểm tra thực tế không nên tự đóng/mở các profile của người dùng đang làm việc.

## Bổ sung độ ổn định — 2026-10-12

- Retry CDP chỉ gửi lại các profile thất bại và vẫn chưa Attached; không động đến session Ready.
- Ready/Configured đếm trực tiếp từ `runner.has_open_session`, tránh hiển thị tiến độ ảo khi một worker timeout/đến muộn.
- Sau timeout, snapshot `READY` tới muộn được đối soát và xóa trạng thái Failed.
- PID dùng để tìm HWND luôn ưu tiên Chrome ancestor có `--user-data-dir` đã xác minh, không phải PID của renderer chỉ sở hữu listener.
- Nếu Windows process discovery không đáng tin, OPEN fail-closed và không tạo Chrome mới vào cùng user-data-dir.
- Detached Git updater đợi mutex `Local\\IKAutoBrowserDashboard` và giữ mutex trong lúc fetch/build, nhả mutex trước khi relaunch.
- Regression tests cho retry có chọn lọc, late attach, live Ready count và thất bại process discovery được bổ sung.

**Giới hạn còn tồn tại:** quá trình kiểm thử 45 profile mới là mock/simulation, không phải kiểm thử 45 Chrome trực tiếp. Nếu process Windows chặn quyền đọc command line, tool sẽ không attach để tránh điều khiển nhầm tài khoản. Updater chỉ hỗ trợ release đặt trong Git checkout và working tree sạch; chưa hỗ trợ standalone distribution ngoài repository.

## Chẩn đoán kết nối không tương tác (bổ sung 2026-10-12)

Chạy trên máy Windows đã cài tool, trong thư mục project:

```bat
.venv\Scripts\python.exe -m ik_chrome_auto --config config.json connection-health
```

Hoặc dùng tham số `--config` trỏ đến cấu hình đang dùng trong bản Release, không giả định Development và Release dùng chung cấu hình.

- Quét tối đa 4 profile đồng thời; chỉ đọc trạng thái process, TCP listener và danh tính CDP, **không mở, đóng, resize, đăng nhập, điều khiển hoặc attach Playwright**.
- Trả về số lượng `configured_enabled`, `verified_cdp` cùng các trạng thái `chrome_without_verified_cdp`, `chrome_not_detected`, `process_discovery_unavailable`, `probe_error` theo từng ID profile.
- Lưu log có xoay vòng tại `data/logs/connection-health.jsonl`; không lưu mật khẩu hay command line của Chrome.
- Cấu hình giờ từ chối hai profile sử dụng chung `user-data-dir` hoặc cùng endpoint CDP. Những cấu hình trùng từ các lần chạy cũ cần tách riêng, không được cho hai tài khoản chia sẻ cùng Chrome.
- Kết quả trên máy BaoNghia lúc kiểm tra: 16 configured, 0 verified CDP, 16 Chrome not detected; thời gian ~8.7s sau khi dùng 4 luồng, so với ~34.8s trước tối ưu. Chưa có phiên 45 Chrome thực tế để nghiệm thu.

Lệnh `connection-health` là phép đo trạng thái tại một thời điểm, **không** chứng minh thực thi thành công Sync, Auto Farm hoặc Monitor trên game.

## Reliability and fleet optimizations — 2026-10-12

The following changes have been implemented in this checkout and are covered by unit tests:

- **Bounded Monitor retries:** each profile gets at most three consecutive attempts for `SCAN_ERROR`. After the third failure it is quarantined for the current monitoring session, and the other profiles are allowed to continue. Quarantined profiles produce a scheduler diagnostic and Telegram warning. Starting a new Monitor session clears that quarantine.
- **Hard Monitor watchdog:** a single mailbox flow exceeding 180 seconds requests cooperative cancellation and stops further Monitor dispatch. It **never** releases the 1280×720 renderer from the dashboard while the worker might still be using it. If Playwright itself never returns, tool restart may still be necessary: Python threads cannot safely interrupt an in-flight native browser call.
- **Actual CDP attach limit:** dashboard's four-slot advisory queue now has a shared `BoundedSemaphore(4)` in the profile workers. An expired UI deadline cannot cause more than four live attach operations.
- **Sync ACK:** critical `pointerdown/pointerup/keydown/keyup` commands are tracked per delivery ID. Follower success/failure is aggregated to one log row per gesture (`sync_input_ack_complete` or `sync_input_ack_incomplete`), with missing/failed follower IDs and maximum CDP call latency. These are CDP-call ACKs, **not** confirmation of in-game effects.
- **Qt pulse bound:** UI event queues are drained in finite batches; the coordinate inspector retains the newest sample from a bounded batch so bursts cannot monopolize the Qt event loop.
- **Working-set trim:** automatic trim is avoided below 85% memory load, during Sync activation/active input, and while any Farm or Monitor worker may own the WebGL renderer.
- **Release staging:** application binaries are first copied into `release/.ik-auto-app-staging`, then moved into `release/IK Auto`. The prior app binaries are retained in `release/.ik-auto-app-previous`; handled errors roll back moved binaries. This operation **never mirrors** the application tree over `data/profiles` or `config.json`. Abrupt power loss in the middle of a two-directory swap is still not fully atomic; a future stable versioned launcher is recommended.

### Remaining gates (not falsely claimed as done)

1. Real 45-profile live test with all Chrome windows open: identity/reattach, 1-to-44 Sync event effects, Mail Monitor, Farm and five restart cycles.
2. Simulated power-loss interruption of an update and startup recovery from backup binaries; app versions currently share an installation path.
3. Standalone manifest-backed update source with hash/signature verification so portable users without a clean Git checkout can update by reopening the app.
4. Before tuning monitor group size or farm renderer parallelism, collect real P50/P95 lease and CDP latency metrics. Leave the one-renderer safety lock in place until measured.
5. Optional dedicated asynchronous log writer and diagnostic bundle, followed by an isolated refactor of oversized dashboard/browser/runner modules.

This work deliberately does not push a dirty worktree or terminate any existing Chrome profile.