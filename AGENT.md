# Quy ước phát triển IK Auto

## Renderer cho Farm và Giám sát

- Không dùng ảnh nhỏ của ô lưới (ví dụ `366×168`) rồi nội suy lên để nhận diện
  ảnh; việc đó không bổ sung chi tiết gốc và không được dùng để quyết định thao
  tác game.
- Farm và Giám sát phải lấy ảnh từ renderer thực `1280×720` qua CDP. Tọa độ
  tương tác luôn là tỉ lệ X/Y theo canvas đang chụp, không phải pixel desktop.
- Một renderer `1280×720` dùng chung cho toàn bộ profile. Phải lấy lease trước
  khi resize và luôn giải phóng lease bằng `finally`, kể cả khi lỗi, dừng Farm
  hoặc đóng Chrome.
- Trước khi nâng renderer, lưu nguyên vị trí và kích thước cửa sổ. Sau lượt
  Giám sát hoặc khi Farm đi vào thời gian chờ dài, khôi phục chính xác ô lưới
  cũ. Không gọi sắp xếp lại toàn bộ cửa sổ chỉ để trả một profile về lưới.
- Không tăng số renderer độ phân giải cao chạy đồng thời nếu chưa đo GPU trên
  máy thực tế. Mặc định giữ một renderer để tránh đỉnh WebGL/DWM khi có nhiều
  profile.

## Kiểm thử bắt buộc khi thay đổi luồng này

```powershell
py -3.13 -m pytest -q
```

Kiểm thử cần giữ các tình huống: canvas nhỏ bị từ chối, renderer thật được nâng
lên `1280×720`, ô lưới được khôi phục, và tọa độ Farm/Giám sát vẫn là tỉ lệ của
canvas hiện tại.
