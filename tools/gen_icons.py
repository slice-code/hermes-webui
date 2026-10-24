#!/usr/bin/env python3
"""Hasilkan ikon PNG PWA Hermes Web Chat — stdlib saja (zlib + struct).

Dipakai untuk `static/icon-192.png`, `static/icon-512.png`, dan
`static/icon-maskable-512.png`. Tidak ada Pillow / cairo / dependency lain:
piksel ditulis sebagai RGBA mentah (filter 0) lalu dikompresi zlib.

Kenapa digenerate, bukan disimpan sebagai berkas biner tangan: hasilnya bisa
diulang (`python3 tools/gen_icons.py`) dan ikut review di git sebagai kode.

Desain: latar gelap (#0f1115) + lingkaran aksen (#7aa2f7) dengan lubang di
tengah — sama dengan favicon SVG di keempat halaman.

Varian *maskable* memakai safe zone Android (lingkaran konten ≤ 40% lebar,
bukan 30%) supaya tidak terpotong kalau peluncur memasang mask bulat/persegi.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

BG = (15, 17, 21, 255)          # --bg gelap
ACCENT = (122, 162, 247, 255)   # --acc
STATIC = Path(__file__).resolve().parent.parent / "static"


def _png(rows: list[bytes], width: int, height: int) -> bytes:
    """Encode baris RGBA (tanpa filter) menjadi PNG 8-bit truecolor+alpha."""
    raw = b"".join(b"\x00" + row for row in rows)  # filter byte 0 per scanline

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _blend(base: tuple, top: tuple, a: float) -> tuple:
    """Alpha-composite `top` di atas `base` (a = 0..1)."""
    return tuple(round(base[i] * (1 - a) + top[i] * a) for i in range(3)) + (255,)


def render(size: int, ring_ratio: float, hole_ratio: float) -> bytes:
    """Ikon persegi: latar gelap, cincin aksen, lubang gelap di tengah.

    `ring_ratio` = jari-jari luar / setengah sisi, `hole_ratio` = jari-jari
    lubang / jari-jari luar. Tepi dihaluskan dengan coverage 1 px.
    """
    cx = cy = (size - 1) / 2.0
    r_out = size * 0.5 * ring_ratio
    r_in = r_out * hole_ratio
    rows: list[bytes] = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            # Coverage linear 1 px di kedua tepi cincin (antialias sederhana).
            outer = min(1.0, max(0.0, r_out - d + 0.5))
            inner = min(1.0, max(0.0, d - r_in + 0.5))
            cov = outer * (inner if r_in > 0 else 1.0)
            px = BG if cov <= 0 else _blend(BG, ACCENT, cov)
            row += bytes(px)
        rows.append(bytes(row))
    return _png(rows, size, size)


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    jobs = [
        # (nama berkas, ukuran, rasio jari-jari luar, rasio lubang)
        ("icon-192.png", 192, 0.62, 0.42),
        ("icon-512.png", 512, 0.62, 0.42),
        # maskable: konten di dalam 80% area aman
        ("icon-maskable-512.png", 512, 0.50, 0.42),
    ]
    for name, size, ring, hole in jobs:
        data = render(size, ring, hole)
        path = STATIC / name
        path.write_bytes(data)
        print(f"{path}  {len(data)} bytes  {size}x{size}")


if __name__ == "__main__":
    main()
