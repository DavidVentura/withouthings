#!/usr/bin/env python3
"""Find references to a moved section that no relocation rewrote.

    python3 abi/stale_scan.py [--elf out/relink/identity.elf]

Validation for the objectified app, as DEVELOPMENT.md's plan describes it: link
with a placement in which some function does not keep its old address, then scan
the image for a 32-bit word that still holds an old address. Every reference the
object declares is rewritten by the linker, so a word that still points at the
old place is a reference the object does not declare -- a pool word or a table
word, which is step 3's work. A word the linker did write is not a survivor even
if it happens to hold an old address, so the relocations the link emits
(`ld --emit-relocs`) are subtracted first.

The moved sections come from out/relink-place-moves.json, which abi/blobify.py
writes from its --move arguments.
"""

import argparse
import json
import os
import struct
import sys

R_ARM_ABS32 = 2

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)


class Elf(object):
    """Just enough ELF32-LE to read section bytes and the emitted relocations."""

    def __init__(self, path):
        self.data = open(path, "rb").read()
        shoff, = struct.unpack_from("<I", self.data, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", self.data, 0x2E)
        raw = [struct.unpack_from("<10I", self.data, shoff + i * shentsize)
               for i in range(shnum)]
        stroff = raw[shstrndx][4]
        self.sections = []
        for name, styp, flags, addr, off, size, link, info, align, entsize in raw:
            end = self.data.index(b"\0", stroff + name)
            self.sections.append({
                "name": self.data[stroff + name:end].decode(), "type": styp,
                "flags": flags, "addr": addr, "off": off, "size": size,
                "info": info, "entsize": entsize})

    def bytes_of(self, section):
        return self.data[section["off"]:section["off"] + section["size"]]

    def allocated(self):
        return [s for s in self.sections if s["flags"] & 2 and s["type"] == 1]

    def relocation_sites(self):
        """The addresses the linker itself wrote an absolute word at."""
        sites = set()
        for s in self.sections:
            if s["type"] not in (4, 9):         # RELA, REL
                continue
            for off in range(0, s["size"], s["entsize"]):
                r_offset, r_info = struct.unpack_from("<II", self.data, s["off"] + off)
                if r_info & 0xFF == R_ARM_ABS32:
                    sites.add(r_offset)
        return sites


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--elf", default=os.path.join(SIM, "out", "relink", "identity.elf"))
    ap.add_argument("--moves", default=os.path.join(SIM, "out", "relink-place-moves.json"))
    args = ap.parse_args()

    with open(args.moves) as fh:
        moves = json.load(fh)
    if not moves:
        sys.exit("%s is empty: nothing moved, so there is nothing to scan for"
                 % args.moves)

    elf = Elf(args.elf)
    written = elf.relocation_sites()
    survivors = []
    for section in elf.allocated():
        blob = elf.bytes_of(section)
        for off in range(0, len(blob) - 3, 4):
            at = section["addr"] + off
            if at in written:
                continue
            value, = struct.unpack_from("<I", blob, off)
            for m in moves:
                if m["old"] <= value & ~1 < m["end"]:
                    survivors.append((at, value, m))
                    break

    for m in moves:
        print("moved %s: 0x%x..0x%x -> 0x%x (%d bytes)"
              % (m["section"], m["old"], m["end"], m["new"], m["end"] - m["old"]))
    print("%d words still hold an address inside a moved section" % len(survivors))
    for at, value, m in survivors[:40]:
        print("  0x%08x holds 0x%08x, %s + 0x%x"
              % (at, value, m["section"], (value & ~1) - m["old"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
