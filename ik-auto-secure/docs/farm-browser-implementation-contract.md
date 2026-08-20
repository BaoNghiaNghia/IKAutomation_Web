# Farm browser — implementation contract

Tài liệu này khóa các invariant triển khai cho browser. Nguồn yêu cầu là
[`farm-automation-web-spec.md`](farm-automation-web-spec.md), đặc biệt các
mục 1.2, 1.3, 5, 6, 7 và 10; `farm-automation-browser-spec.md` là tài liệu
chuyển đổi adapter từ ADB sang browser/CDP.

## Luồng không được thay đổi

`Preflight → World Map → quét roster → chọn resource + level → mở/search panel
→ tìm resource → xác minh popup → chọn team → dispatch → xác minh march →
waiting`.

Mỗi cycle chỉ hoàn tất tối đa một dispatch đã xác minh. Fallback phải đổi loại
tài nguyên sau từng lần không có kết quả; chỉ sau khi đã thử đủ bốn loại tại
cùng cấp mỏ mới được chuyển khu vực theo pool điểm đã xác minh.

## Quét roster World Map

- Mỗi cycle phải quét từng hàng đội đã mở, với số đội và trạng thái từng đội
  được ghi log: `ready` hoặc `busy`.
- Chỉ nhãn `Sẵn sàng` là bằng chứng `ready` cho đúng hàng đó. Bốn hàng được
  đánh số cố định từ trên xuống: `1 → 2 → 3 → 4`; không được remap theo số
  lượng nhãn tìm thấy. Nếu một hàng cao hơn được xác nhận thì các hàng trước
  đó được coi là đã mở; hàng không có nhãn `Sẵn sàng` được phân loại `busy`,
  không được chọn lại.
- Scheduler chỉ chọn đội `ready` đầu tiên theo policy. Không được suy ra team
  từ số lượng nhãn sẵn sàng.

## Kế hoạch tài nguyên

- Đầu mỗi cycle, xáo trộn một lần thứ tự bốn tài nguyên: `food`, `wood`,
  `stone`, `iron`; ghi thứ tự đó vào log của cycle.
- Cấp mỏ hợp lệ của fallback là `6 → 7 → 8`; cấp 5 không có quy tắc khu vực
  nên không được đưa vào plan.
- Với một cấp mỏ, thử lần lượt bốn tài nguyên trong thứ tự đã xáo trộn. Mỗi
  `no-result` chỉ đổi sang tài nguyên kế tiếp cùng cấp. Sau tài nguyên thứ tư
  không có kết quả, mới gọi luồng đổi khu vực; không random lại thứ tự tài
  nguyên giữa các lần retry/fallback.
- Trước mỗi tap `Tìm kiếm`, bắt buộc bật checkbox lọc mục tiêu trong panel.
  Matcher chỉ nhận diện glyph **chưa chọn**; worker rematch, tap chính glyph đó
  và chỉ tiếp tục khi glyph chưa chọn biến mất trong panel vẫn được xác minh.
  Nếu không xác minh được trong thời gian giới hạn thì dừng an toàn và lưu ảnh
  diagnostic, không được bấm `Tìm kiếm`.
- Ngay sau mỗi tap `Tìm kiếm`, quan sát toast trong một cửa sổ ngắn. Toast
  `không tìm thấy` hoặc `cấp mục tiêu quá thấp` là kết quả nghiệp vụ: chờ toast
  biến mất rồi đổi resource theo plan; chỉ sau đủ bốn resource mới đổi khu vực.
  Không dừng workflow và không tap lặp mù quáng.
- Nếu hết cửa sổ quan sát mà popup không xuất hiện nhưng nút `Tìm kiếm` vẫn
  còn dùng được, coi đây là một lần `no-result` không có toast và đổi sang
  resource tiếp theo; không lặp lại vô hạn cùng một nút.
- Khi dialog có thông điệp bắt đầu bằng `Điểm Tài nguyên mục tiêu sẽ biến mất`
  xuất hiện trong lúc điều quân, worker rematch cả phần thông điệp cố định và
  nút `Xác nhận` trên frame mới rồi mới bấm. Sau đó phải xác minh panel đã
  đóng và đoàn quân xuất phát; không được bấm các dialog đỏ khác.
- Quy tắc vùng khi ADB/browser nhận được kết quả phải đổi khu vực (`Resource
  Area Lv2 Redirect`) được giữ nguyên từ `ResourceAreaLv2PointSelector`:

  | Cấp mỏ | Khu vực thành được phép |
  | --- | --- |
  | 6 | 7, 8 |
  | 7 | 7, 8, 9, 10 |
  | 8 | 8, 9, 10 |

  Browser chỉ được nhập một tọa độ của các khu vực này sau khi đã match được
  UI tọa độ/pin tương ứng trên frame mới; không dùng tọa độ màn hình cố định.
  Luồng mở bản đồ trên website là: World Map (hoặc panel tìm kiếm trên nền
  World Map) → bấm nút quay lại đã được match để về City → match và bấm icon
  bản đồ City → xác minh Continent Map → đọc/ghi X,Y → Pin. Không được dùng
  shortcut minimap của ADB trên website.
  Sau khi thử cả bốn resource không có kết quả, selector được phép thử tối đa
  ba điểm chưa dùng. Mỗi lần đổi điểm phải quay lại World Map sạch popup, mở
  lại panel và áp dụng lại cấu hình tìm kiếm; nếu không xác minh được một bước
  thì không input/click tiếp và giữ nguyên trạng thái an toàn. Khi hết ba điểm,
  mới chuyển sang cấp mỏ kế tiếp trong `6 → 7 → 8`.
  Danh sách đang được dùng có 55 điểm Lv8 (đúng theo list nguồn và source
  ADB), nên tổng thực tế là Lv6: 71, Lv7: 123, Lv8: 107; không tự bịa thêm
  một điểm để khớp các số tổng được ghi trước đó.

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
