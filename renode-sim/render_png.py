#!/usr/bin/env python3
# Reconstruct a framebuffer PNG from the raw pixel bytes dumped by
# OledSpimCapture.DumpPixels. Format is not known a priori, so this supports the
# layouts a small OLED controller typically uses; pick with --fmt.
import argparse, struct, zlib, sys

def png(path, w, h, rgb):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            raw += bytes(rgb[y*w + x])
    out = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
           chunk(b"IEND", b""))
    open(path, "wb").write(out)

def gray(v): return (v, v, v)

def fmt_page1bpp(data, w, h):
    # SSD1306-style: 1bpp, one byte = 8 vertical pixels, pages of h/8 rows.
    px = [(0,0,0)] * (w*h)
    pages = h // 8
    i = 0
    for pg in range(pages):
        for x in range(w):
            if i >= len(data): break
            b = data[i]; i += 1
            for bit in range(8):
                y = pg*8 + bit
                if (b >> bit) & 1:
                    px[y*w + x] = (255,255,255)
    return px

def fmt_gray4(data, w, h):
    # 4bpp grayscale, two pixels per byte (hi nibble first).
    px = [(0,0,0)] * (w*h)
    for idx in range(min(w*h, len(data)*2)):
        b = data[idx//2]
        nib = (b >> 4) if (idx % 2 == 0) else (b & 0xf)
        px[idx] = gray(nib * 17)
    return px

def fmt_gray8(data, w, h):
    px = [(0,0,0)] * (w*h)
    for idx in range(min(w*h, len(data))):
        px[idx] = gray(data[idx])
    return px

def fmt_rgb565(data, w, h):
    px = [(0,0,0)] * (w*h)
    for idx in range(min(w*h, len(data)//2)):
        hi, lo = data[2*idx], data[2*idx+1]
        v = (hi << 8) | lo
        r = ((v >> 11) & 0x1f) << 3
        g = ((v >> 5) & 0x3f) << 2
        b = (v & 0x1f) << 3
        px[idx] = (r, g, b)
    return px

FMTS = {"page1bpp": fmt_page1bpp, "gray4": fmt_gray4, "gray8": fmt_gray8, "rgb565": fmt_rgb565}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("outfile")
    ap.add_argument("--w", type=int, required=True)
    ap.add_argument("--h", type=int, required=True)
    ap.add_argument("--fmt", choices=list(FMTS), default="gray4")
    ap.add_argument("--skip", type=int, default=0, help="skip N leading bytes")
    a = ap.parse_args()
    data = open(a.infile, "rb").read()[a.skip:]
    print("input %d bytes, %dx%d, fmt=%s" % (len(data), a.w, a.h, a.fmt))
    px = FMTS[a.fmt](data, a.w, a.h)
    png(a.outfile, a.w, a.h, px)
    print("wrote", a.outfile)

if __name__ == "__main__":
    main()
