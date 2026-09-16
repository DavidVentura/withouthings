#!/usr/bin/env python3
"""Write out/flash-relinked.bin: flash.bin with the relinked blob and library.

MBR and SoftDevice come from flash.bin untouched; the blob replaces the app at
0x27000 with its boundary calls rewritten, and the source kernel goes into the
free flash at OVERLAY_FLASH. flash.bin itself is never modified.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
APP_BASE = 0x27000
LIB_BASE = 0xF4000
LIB_SIZE = 0x8000


def main():
    out_dir = os.path.join(SIM, "out", "relink")
    image = bytearray(open(os.path.join(SIM, "flash.bin"), "rb").read())
    blob = open(os.path.join(out_dir, "blob.bin"), "rb").read()
    lib = open(os.path.join(out_dir, "libtext.bin"), "rb").read()

    if len(blob) != len(open(os.path.join(SIM, "appl.bin"), "rb").read()):
        sys.exit("blob is %d bytes, appl.bin is not" % len(blob))
    if len(lib) > LIB_SIZE:
        sys.exit("library is %d bytes, OVERLAY_FLASH holds %d" % (len(lib), LIB_SIZE))
    if any(b != 0xFF for b in image[LIB_BASE:LIB_BASE + len(lib)]):
        sys.exit("OVERLAY_FLASH at 0x%x is not erased in flash.bin" % LIB_BASE)

    image[APP_BASE:APP_BASE + len(blob)] = blob
    image[LIB_BASE:LIB_BASE + len(lib)] = lib

    path = os.path.join(SIM, "out", "flash-relinked.bin")
    with open(path, "wb") as f:
        f.write(image)
    print("%s (library %d bytes at 0x%x)" % (path, len(lib), LIB_BASE))


if __name__ == "__main__":
    main()
