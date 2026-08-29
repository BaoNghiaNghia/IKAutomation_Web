"""Small PNG decoding helpers shared by browser capture and diagnostics."""
from __future__ import annotations

import struct
import zlib


class RGBImage:
    def __init__(self, width: int, height: int, pixels: bytes) -> None:
        if len(pixels) != width * height * 3:
            raise ValueError("Dữ liệu RGB không khớp kích thước ảnh")
        self.width = width
        self.height = height
        self.pixels = pixels

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        offset = (y * self.width + x) * 3
        return tuple(self.pixels[offset : offset + 3])  # type: ignore[return-value]

    def feature(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
        *,
        size: int = 12,
    ) -> tuple[float, ...]:
        left = max(0, min(self.width - 1, left))
        top = max(0, min(self.height - 1, top))
        right = max(left + 1, min(self.width, right))
        bottom = max(top + 1, min(self.height, bottom))
        values: list[float] = []
        for target_y in range(size):
            source_y = min(bottom - 1, top + int((target_y + 0.5) * (bottom - top) / size))
            for target_x in range(size):
                source_x = min(right - 1, left + int((target_x + 0.5) * (right - left) / size))
                red, green, blue = self.pixel(source_x, source_y)
                values.extend((red / 255.0, green / 255.0, blue / 255.0))
        return tuple(values)


def decode_png(data: bytes) -> RGBImage:
    """Decode a non-interlaced RGB/RGBA PNG into opaque RGB pixels."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Ảnh quét không phải PNG")
    position = 8
    idat: list[bytes] = []
    width = height = bit_depth = color_type = interlace = -1
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        chunk_type = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        position += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if compression != 0 or filtering != 0:
                raise ValueError("PNG dùng kiểu nén/lọc không được hỗ trợ")
        elif chunk_type == b"IDAT":
            idat.append(payload)
        elif chunk_type == b"IEND":
            break
    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
        raise ValueError("Chỉ hỗ trợ PNG RGB/RGBA 8-bit không interlace")
    bytes_per_pixel = 3 if color_type == 2 else 4
    stride = width * bytes_per_pixel
    raw = zlib.decompress(b"".join(idat))
    previous = bytearray(stride)
    offset = 0
    rgb = bytearray(width * height * 3)
    for y in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset : offset + stride])
        offset += stride
        for index in range(stride):
            left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + above) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                row[index] = (row[index] + _paeth(left, above, upper_left)) & 0xFF
            elif filter_type != 0:
                raise ValueError(f"PNG filter không được hỗ trợ: {filter_type}")
        for x in range(width):
            source = x * bytes_per_pixel
            target = (y * width + x) * 3
            if color_type == 6:
                alpha = row[source + 3] / 255.0
                rgb[target] = round(row[source] * alpha)
                rgb[target + 1] = round(row[source + 1] * alpha)
                rgb[target + 2] = round(row[source + 2] * alpha)
            else:
                rgb[target : target + 3] = row[source : source + 3]
        previous = row
    return RGBImage(width, height, bytes(rgb))


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distance_left = abs(estimate - left)
    distance_above = abs(estimate - above)
    distance_upper_left = abs(estimate - upper_left)
    if distance_left <= distance_above and distance_left <= distance_upper_left:
        return left
    return above if distance_above <= distance_upper_left else upper_left
