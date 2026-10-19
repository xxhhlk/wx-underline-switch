"""纯标准库生成 app.ico（48x48，32bpp）：白底圆角 + 三条灰色文本行 + 微信绿虚线。"""

import struct
import sys

SIZE = 48
BG = (255, 255, 255)
BORDER = (216, 216, 216)
TEXT = (150, 150, 150)
GREEN = (7, 193, 96)


def rounded_rect(x, y, w, h, radius):
    """返回判定函数：点是否在圆角矩形内。"""
    def inside(px, py):
        if px < x or px >= x + w or py < y or py >= y + h:
            return False
        for cx, cy in ((x + radius, y + radius), (x + w - radius - 1, y + radius),
                       (x + radius, y + h - radius - 1),
                       (x + w - radius - 1, y + h - radius - 1)):
            if ((px < x + radius and py < y + radius) or
                    (px > x + w - radius - 1 and py < y + radius) or
                    (px < x + radius and py > y + h - radius - 1) or
                    (px > x + w - radius - 1 and py > y + h - radius - 1)):
                if (px - cx) ** 2 + (py - cy) ** 2 > radius * radius:
                    return False
        return True
    return inside


def build_pixels():
    """返回 SIZE*SIZE 的 BGRA 像素（行优先，自上而下）。"""
    px = [[(0, 0, 0, 0)] * SIZE for _ in range(SIZE)]
    card = rounded_rect(2, 2, SIZE - 4, SIZE - 4, 9)
    inner = rounded_rect(3, 3, SIZE - 6, SIZE - 6, 8)
    for y in range(SIZE):
        for x in range(SIZE):
            if card(x, y):
                r, g, b = BORDER if not inner(x, y) else BG
                px[y][x] = (b, g, r, 255)
    # 三条文本行
    for row_y in (13, 21, 29):
        for y in range(row_y, row_y + 3):
            for x in range(10, 38):
                if inner(x, y):
                    px[y][x] = (TEXT[2], TEXT[1], TEXT[0], 255)
    # 最后一行下方的绿色虚线
    for y in range(35, 37):
        for x in range(10, 38):
            if not inner(x, y):
                continue
            if (x - 10) % 7 < 4:
                px[y][x] = (GREEN[2], GREEN[1], GREEN[0], 255)
    return px


def write_ico(path):
    px = build_pixels()
    # XOR 位图：BGRA，自下而上
    xor = bytearray()
    for y in range(SIZE - 1, -1, -1):
        for x in range(SIZE):
            b, g, r, a = px[y][x]
            xor += bytes((b, g, r, a))
    # AND 掩码：全 0（全不透明），每行按 4 字节对齐
    row_bytes = ((SIZE + 31) // 32) * 4
    and_mask = bytes(row_bytes * SIZE)

    header = struct.pack("<IiiHHIIiiII", 40, SIZE, SIZE * 2, 1, 32, 0,
                         len(xor) + len(and_mask), 0, 0, 0, 0)
    image = header + bytes(xor) + and_mask

    icondir = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", SIZE, SIZE, 0, 0, 1, 32,
                        len(image), 6 + 16)
    with open(path, "wb") as fh:
        fh.write(icondir + entry + image)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out = sys.argv[1] if len(sys.argv) > 1 else "app.ico"
    write_ico(out)
    print("written %s" % out)
