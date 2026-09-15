#!/usr/bin/env python3
# Reconstruct a framebuffer PNG from the raw pixel bytes dumped by
# OledSpimCapture.DumpPixels. Format is not known a priori, so this supports the
# layouts a small OLED controller typically uses; pick with --fmt. The HWA10
# panel is an SSD1320 driven at 144x98 in 4bpp (--w 144 --h 98 --fmt gray4).
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
    # SSD1320: 4bpp grayscale, two pixels per byte, low nibble is the left pixel.
    px = [(0,0,0)] * (w*h)
    for idx in range(min(w*h, len(data)*2)):
        b = data[idx//2]
        nib = (b & 0xf) if (idx % 2 == 0) else (b >> 4)
        px[idx] = gray(nib * 17)
    return px

def fmt_gray4hi(data, w, h):
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

FMTS = {"page1bpp": fmt_page1bpp, "gray4": fmt_gray4, "gray4hi": fmt_gray4hi, "gray8": fmt_gray8, "rgb565": fmt_rgb565}

def upscale(px, w, h, s):
    out = [(0,0,0)] * (w*s*h*s)
    for y in range(h*s):
        row = (y//s)*w
        for x in range(w*s):
            out[y*w*s + x] = px[row + x//s]
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("outfile")
    ap.add_argument("--w", type=int, required=True)
    ap.add_argument("--h", type=int, required=True)
    ap.add_argument("--fmt", choices=list(FMTS), default="gray4")
    ap.add_argument("--skip", type=int, default=0, help="skip N leading bytes")
    ap.add_argument("--scale", type=int, default=1, help="nearest-neighbour upscale")
    a = ap.parse_args()
    data = open(a.infile, "rb").read()[a.skip:]
    print("input %d bytes, %dx%d, fmt=%s" % (len(data), a.w, a.h, a.fmt))
    px = FMTS[a.fmt](data, a.w, a.h)
    if a.scale > 1:
        px = upscale(px, a.w, a.h, a.scale)
    png(a.outfile, a.w * a.scale, a.h * a.scale, px)
    print("wrote", a.outfile)

if __name__ == "__main__":
    main()
