#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoFileSync 图标生成器（纯标准库，不依赖 Pillow）。

图形语义：圆角方形蓝色底 + 两条反向箭头 = 文件夹镜像同步。
做法：每个尺寸按自身倍数超采样渲染，再按面积平均降采样，得到平滑边缘；
最后用 zlib + CRC 手写 PNG（RGBA）。
"""
import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "src", "ui", "images"))
SIZES = (16, 24, 32, 48, 64, 72, 256)

TOP = (0x3B, 0x82, 0xFF)
BOT = (0x1B, 0x46, 0xC8)
INK = (0xFF, 0xFF, 0xFF)


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _dist_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else _clamp(((px - x1) * dx + (py - y1) * dy) / l2, 0.0, 1.0)
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _in_tri(px, py, a, b, c):
    def s(p1, p2):
        return (px - p2[0]) * (p1[1] - p2[1]) - (p1[0] - p2[0]) * (py - p2[1])
    d1, d2, d3 = s(a, b), s(b, c), s(c, a)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def _in_round_rect(x, y, w, h, r):
    if x < 0 or y < 0 or x >= w or y >= h:
        return False
    cx = _clamp(x, r, w - r)
    cy = _clamp(y, r, h - r)
    return math.hypot(x - cx, y - cy) <= r


def render(size, ss):
    """返回 size*ss 的 RGBA 行列表。"""
    W = size * ss
    r = 0.235 * W
    bar = 0.095 * W
    half = bar / 2.0

    # 箭头几何（归一化坐标 × W）
    r_y1, r_x1, r_x2 = 0.395 * W, 0.255 * W, 0.625 * W
    r_tip, r_hy = 0.815 * W, 0.115 * W
    l_y1, l_x1, l_x2 = 0.615 * W, 0.745 * W, 0.375 * W
    l_tip, l_hy = 0.185 * W, 0.115 * W

    rows = []
    for y in range(W):
        py = y + 0.5
        t = py / W
        bg = (
            int(TOP[0] + (BOT[0] - TOP[0]) * t),
            int(TOP[1] + (BOT[1] - TOP[1]) * t),
            int(TOP[2] + (BOT[2] - TOP[2]) * t),
        )
        row = []
        for x in range(W):
            px = x + 0.5
            if not _in_round_rect(px, py, W, W, r):
                row.append((0, 0, 0, 0))
                continue
            ink = (
                _dist_seg(px, py, r_x1, r_y1, r_x2, r_y1) <= half
                or _in_tri(px, py, (r_x2, r_y1 - r_hy), (r_tip, r_y1), (r_x2, r_y1 + r_hy))
                or _dist_seg(px, py, l_x1, l_y1, l_x2, l_y1) <= half
                or _in_tri(px, py, (l_x2, l_y1 - l_hy), (l_tip, l_y1), (l_x2, l_y1 + l_hy))
            )
            if ink:
                row.append((INK[0], INK[1], INK[2], 255))
            else:
                row.append((bg[0], bg[1], bg[2], 255))
        rows.append(row)
    return rows


def downsample(rows, ss):
    """把 (size*ss)² 按 ss×ss 块面积平均成 size²。"""
    W = len(rows)
    n = W // ss
    out = []
    for y in range(n):
        row = []
        for x in range(n):
            r = g = b = a = 0
            for dy in range(ss):
                src = rows[y * ss + dy]
                for dx in range(ss):
                    p = src[x * ss + dx]
                    r += p[0]; g += p[1]; b += p[2]; a += p[3]
            cnt = ss * ss
            row.append((r // cnt, g // cnt, b // cnt, a // cnt))
        out.append(row)
    return out


def write_png(path, rows):
    h = len(rows)
    w = len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter: none
        for p in row:
            raw.extend(p)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def main():
    os.makedirs(OUT, exist_ok=True)
    made = {}
    for size in SIZES:
        ss = max(2, min(8, 512 // size))
        rows = downsample(render(size, ss), ss)
        path = os.path.join(OUT, "icon_%d.png" % size)
        write_png(path, rows)
        made[size] = path
        print("  icon_%d.png (%d×%d, %dx supersample)" % (size, size, size, ss))

    root = os.path.normpath(os.path.join(HERE, ".."))
    write_png(os.path.join(root, "PACKAGE_ICON.PNG"), downsample(render(72, 7), 7))
    write_png(os.path.join(root, "PACKAGE_ICON_256.PNG"), downsample(render(256, 2), 2))
    print("  PACKAGE_ICON.PNG / PACKAGE_ICON_256.PNG")


if __name__ == "__main__":
    main()
