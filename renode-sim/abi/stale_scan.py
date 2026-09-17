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
    ap.add_argument("--words", default=os.path.join(HERE, "out", "ghidra", "words.json"))
    ap.add_argument("--items", default=os.path.join(HERE, "out", "ghidra", "items.json"))
    ap.add_argument("--list", type=int, default=20, help="survivors to print per bucket")
    args = ap.parse_args()

    with open(args.moves) as fh:
        moves = json.load(fh)
    if not moves:
        sys.exit("%s is empty: nothing moved, so there is nothing to scan for"
                 % args.moves)
    with open(args.words) as fh:
        words = {w["addr"]: w for w in json.load(fh)["words"]}
    with open(args.items) as fh:
        bits = bytes.fromhex(json.load(fh)["instruction_bytes"]["bits"])

    def is_instruction(addr):
        """A word the disassembly covers is not a slot anything could relocate.

        Two Thumb instructions in a row hold an in-range value often enough --
        `movs r1,r1` before a `push.w` reads as 0x0009e92d, which is inside this
        image -- that without this the scan is mostly noise.
        """
        i = addr - 0x27000
        return any(bits[(i + k) >> 3] >> ((i + k) & 7) & 1 for k in range(4))

    elf = Elf(args.elf)
    written = elf.relocation_sites()
    moves.sort(key=lambda m: m["old"])
    olds = [m["old"] for m in moves]
    # The scan reads the linked image, so a word's address there is its new one;
    # the classification is keyed by where the word was. Only moved sections
    # need the translation, and they do not overlap.
    news = sorted((m["new"], m["new"] + m["end"] - m["old"], m["old"]) for m in moves)

    def original(at):
        lo, hi = 0, len(news)
        while lo < hi:
            mid = (lo + hi) // 2
            if news[mid][0] <= at:
                lo = mid + 1
            else:
                hi = mid
        if lo and news[lo - 1][1] > at:
            return news[lo - 1][2] + at - news[lo - 1][0]
        return at

    def moved(value):
        lo, hi = 0, len(olds)
        while lo < hi:
            mid = (lo + hi) // 2
            if olds[mid] <= value:
                lo = mid + 1
            else:
                hi = mid
        return moves[lo - 1] if lo and moves[lo - 1]["end"] > value else None

    survivors = []
    for section in elf.allocated():
        blob = elf.bytes_of(section)
        for off in range(0, len(blob) - 3, 2):
            at = section["addr"] + off
            # An R_ARM_ABS32 slot is word-aligned in the image it came from, so
            # a misaligned window is two halves of two different data fields;
            # a moved section can change the parity, hence the old address.
            if at in written or original(at) % 4:
                continue
            value, = struct.unpack_from("<I", blob, off)
            m = moved(value & ~1)
            if m is not None:
                survivors.append((at, value, m))

    print("moved %d sections, %d bytes" % (len(moves),
                                           sum(m["end"] - m["old"] for m in moves)))
    print("%d words still hold an address inside a moved section" % len(survivors))
    buckets, unexplained, in_instructions = {}, [], 0
    for at, value, m in survivors:
        if is_instruction(original(at)):
            in_instructions += 1
            continue
        word = words.get(original(at))
        if word is None:
            # The scan reads the linked image, which is laid out differently
            # from the one the classification ran on, so a survivor at an
            # address the candidate set does not know is a word the partition
            # never offered -- the only kind that is a real miss.
            unexplained.append((at, value, m, "not a candidate word"))
            continue
        if word["class"] == "pointer":
            unexplained.append((at, value, m, "classified a pointer but not relocated"))
            continue
        key = "%s:%s:%s" % (word["kind"], word["class"], word["signal"])
        buckets.setdefault(key, []).append((at, value, m))
    for key in sorted(buckets):
        rows = buckets[key]
        print("  %-48s %d" % (key, len(rows)))
        for at, value, m in rows[:args.list]:
            print("      0x%08x (was 0x%08x) holds 0x%08x, %s + 0x%x"
                  % (at, original(at), value, m["section"], (value & ~1) - m["old"]))
    print("  %-48s %d" % ("(bytes of an instruction, not a slot)", in_instructions))
    print("%d survivors are not explained" % len(unexplained))
    for at, value, m, why in unexplained[:args.list * 4]:
        print("  0x%08x (was 0x%08x) holds 0x%08x, %s + 0x%x: %s"
              % (at, original(at), value, m["section"], (value & ~1) - m["old"], why))
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main())
