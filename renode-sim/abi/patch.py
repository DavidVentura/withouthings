#!/usr/bin/env python3
"""Write out/flash-patched.bin: flash.bin + the overlay + patches.txt."""

import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
LINE = re.compile(r"^(0x[0-9a-fA-F]+)\s+((?:[0-9a-fA-F]{2}\s+)+?)\s((?:[0-9a-fA-F]{2}\s*)+)$")


def read_patches(path):
    patches = []
    for lineno, raw in enumerate(open(path), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = LINE.match(line)
        if not m:
            sys.exit("patches.txt:%d: not '<addr> <orig bytes>  <new bytes>'" % lineno)
        old = bytes.fromhex(m.group(2).replace(" ", ""))
        new = bytes.fromhex(m.group(3).replace(" ", ""))
        if len(old) != len(new):
            sys.exit("patches.txt:%d: %d original bytes but %d new" % (lineno, len(old), len(new)))
        patches.append((int(m.group(1), 16), old, new))
    return patches


def main():
    with open(os.path.join(HERE, "hwa10.yaml")) as f:
        manifest = yaml.safe_load(f)
    flash_region = next(r for r in manifest["regions"] if r["name"] == "OVERLAY_FLASH")

    image = bytearray(open(os.path.join(HERE, "..", "flash.bin"), "rb").read())
    overlay = open(os.path.join(HERE, "out", "overlay.bin"), "rb").read()
    if len(overlay) > flash_region["size"]:
        sys.exit("overlay is %d bytes, OVERLAY_FLASH holds %d" % (len(overlay), flash_region["size"]))
    start = flash_region["start"]
    if any(b != 0xFF for b in image[start:start + len(overlay)]):
        sys.exit("OVERLAY_FLASH at 0x%x is not erased in flash.bin" % start)
    image[start:start + len(overlay)] = overlay

    for address, old, new in read_patches(os.path.join(HERE, "patches.txt")):
        found = bytes(image[address:address + len(old)])
        if found != old:
            sys.exit("0x%x: image has %s, patch expects %s" % (address, found.hex(" "), old.hex(" ")))
        image[address:address + len(new)] = new

    out = os.path.join(HERE, "out", "flash-patched.bin")
    with open(out, "wb") as f:
        f.write(image)
    print("%s (overlay %d bytes at 0x%x)" % (out, len(overlay), start))


if __name__ == "__main__":
    main()
