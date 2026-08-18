from __future__ import annotations

import argparse
from pathlib import Path

from ik_chrome_auto.game2048 import decode_png, detect_grid
from ik_chrome_auto.windows import encode_rgb_png


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract one exact 2048 grid cell as a live vision prototype."
    )
    parser.add_argument("image", type=Path)
    parser.add_argument("row", type=int, choices=range(1, 5))
    parser.add_argument("column", type=int, choices=range(1, 5))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    image = decode_png(args.image.read_bytes())
    grid = detect_grid(image)
    left = grid.x_lines[args.column - 1]
    right = grid.x_lines[args.column]
    top = grid.y_lines[args.row - 1]
    bottom = grid.y_lines[args.row]
    pixels = bytearray()
    for y in range(top, bottom):
        for x in range(left, right):
            pixels.extend(image.pixel(x, y))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encode_rgb_png(right - left, bottom - top, bytes(pixels)))
    print(
        f"Extracted r{args.row}c{args.column} "
        f"({right - left}x{bottom - top}) -> {args.output}"
    )


if __name__ == "__main__":
    main()
