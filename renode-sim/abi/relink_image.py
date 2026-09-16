#!/usr/bin/env python3
"""Write out/flash-relinked.bin and out/appl-relinked.bin.

MBR and SoftDevice come from flash.bin untouched; the blob replaces the app at
0x27000 with its boundary calls rewritten, and the source kernel goes into the
free flash at LIBRARY_FLASH, which starts at the app image end so that app and
library are one contiguous block. That block is also written out on its own as
out/appl-relinked.bin, the `appl` part tools/mkpkg.py --appl packages.
flash.bin itself is never modified.
"""

import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
APP_BASE = 0x27000
APP_END = 0xF117C
LIB_LIMIT = 0xFC000


def section_address(path, name):
    """The link address of one section of an ELF32 little-endian file.

    The library is placed at the app image end, but .libtext carries its own
    16-byte alignment (relink_glue.c's naked vPortEnableVFP ends in `.align 4`),
    so the linker may push it past the MEMORY origin; writing the bytes at the
    origin anyway shifts the whole library and the image executes garbage.
    """
    data = open(path, "rb").read()
    shoff, = struct.unpack_from("<I", data, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x2E)
    stroff, = struct.unpack_from("<I", data, shoff + shstrndx * shentsize + 0x10)
    for i in range(shnum):
        head = shoff + i * shentsize
        nameoff, = struct.unpack_from("<I", data, head)
        end = data.index(b"\0", stroff + nameoff)
        if data[stroff + nameoff:end].decode() == name:
            return struct.unpack_from("<I", data, head + 0x0C)[0]
    sys.exit("%s has no section %s" % (path, name))


def main():
    out_dir = os.path.join(SIM, "out", "relink")
    image = bytearray(open(os.path.join(SIM, "flash.bin"), "rb").read())
    blob = open(os.path.join(out_dir, "blob.bin"), "rb").read()
    lib = open(os.path.join(out_dir, "libtext.bin"), "rb").read()

    lib_base = section_address(os.path.join(out_dir, "relinked.elf"), ".libtext")
    if APP_BASE + len(blob) != APP_END:
        sys.exit("blob ends at 0x%x, not the app image end 0x%x"
                 % (APP_BASE + len(blob), APP_END))
    if not APP_END <= lib_base < APP_END + 0x100:
        sys.exit("the library is linked at 0x%x, not just past the app end 0x%x"
                 % (lib_base, APP_END))
    if lib_base + len(lib) > LIB_LIMIT:
        sys.exit("library runs to 0x%x, the bootloader starts at 0x%x"
                 % (lib_base + len(lib), LIB_LIMIT))
    if any(b != 0xFF for b in image[lib_base:lib_base + len(lib)]):
        sys.exit("LIBRARY_FLASH at 0x%x is not erased in flash.bin" % lib_base)

    image[APP_BASE:APP_BASE + len(blob)] = blob
    image[lib_base:lib_base + len(lib)] = lib

    path = os.path.join(SIM, "out", "flash-relinked.bin")
    with open(path, "wb") as f:
        f.write(image)
    part = os.path.join(SIM, "out", "appl-relinked.bin")
    with open(part, "wb") as f:
        f.write(image[APP_BASE:lib_base + len(lib)])
    print("%s (library %d bytes at 0x%x)" % (path, len(lib), lib_base))
    print("%s (%d bytes, 0x%x..0x%x)"
          % (part, lib_base + len(lib) - APP_BASE, APP_BASE, lib_base + len(lib)))


if __name__ == "__main__":
    main()
