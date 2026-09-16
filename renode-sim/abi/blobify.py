#!/usr/bin/env python3
"""Emit appl.bin as an ELF32 ARM relocatable object with the boundary relocated.

    python3 abi/blobify.py [-o out/appl-blob.o]

Reads abi/boundary.yaml. Every app->library `bl` whose target is named and not
marked `keep` becomes an R_ARM_THM_CALL against that symbol, with the branch
displacement zeroed so the REL addend is 0. Every vector-table word marked
`relocate` becomes an R_ARM_ABS32 against the handler symbol, zeroed likewise.
Library->app targets and the app's entry points become defined symbols so the
source library can be linked against them.

The blob keeps its own copy of the library bytes: the relocations move the calls
away from them, and leaving them in place keeps an A/B comparison possible.

The ELF structures are written by hand (no pyelftools/lief dependency); the
output is a plain REL object that arm-none-eabi-ld consumes directly.
"""

import argparse
import os
import struct
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
APP_BASE = 0x27000

R_ARM_ABS32 = 2
R_ARM_THM_CALL = 10

STT_FUNC, STT_NOTYPE = 2, 0
STB_GLOBAL = 1


def decode_bl(data, off):
    """-> branch target, or None if these four bytes are not a Thumb BL."""
    hi, lo = struct.unpack_from("<HH", data, off)
    if (hi & 0xF800) != 0xF000 or (lo & 0xD000) != 0xD000:
        return None
    s = (hi >> 10) & 1
    j1, j2 = (lo >> 13) & 1, (lo >> 11) & 1
    imm = (s << 24) | ((1 - (j1 ^ s)) << 23) | ((1 - (j2 ^ s)) << 22) \
        | ((hi & 0x3FF) << 12) | ((lo & 0x7FF) << 1)
    if s:
        imm -= 1 << 25
    return APP_BASE + off + 4 + imm


def find_sites(data, wanted, spans):
    """Every BL in the image whose target is one of `wanted`, by scan and check.

    The scan is over raw halfword-aligned bytes rather than a disassembly,
    because a linear disassembly desynchronises on literal pools. It agrees
    exactly with abi/match.py's disassembly on this image (647 sites), which is
    what makes a data word accidentally decoding as a boundary BL implausible.
    """
    sites = []
    for off in range(0, len(data) - 3, 2):
        addr = APP_BASE + off
        if any(lo <= addr <= hi for lo, hi in spans):
            continue            # library calling itself, not the boundary
        tgt = decode_bl(data, off)
        if tgt is not None and tgt in wanted:
            sites.append((addr, tgt))
    return sites


class Elf:
    def __init__(self):
        self.shstr = bytearray(b"\0")
        self.str = bytearray(b"\0")
        self.syms = [b"\0" * 16]      # the mandatory null symbol
        self.symidx = {}

    def name(self, table, s):
        off = len(table)
        table += s.encode() + b"\0"
        return off

    def symbol(self, sym, value, shndx, styp):
        if sym in self.symidx:
            return self.symidx[sym]
        idx = len(self.syms)
        self.syms.append(struct.pack("<IIIBBH", self.name(self.str, sym), value, 0,
                                     (STB_GLOBAL << 4) | styp, 0, shndx))
        self.symidx[sym] = idx
        return idx


def build(blob, relocs, defined, path):
    e = Elf()
    for sym, (value, styp) in sorted(defined.items()):
        # ARM ELF carries Thumb-ness in bit 0 of a function symbol's value.
        e.symbol(sym, value - APP_BASE + (1 if styp == STT_FUNC else 0), 1, styp)
    rel = b"".join(struct.pack("<II", off, (e.symbol(sym, 0, 0, STT_NOTYPE) << 8) | rtype)
                   for off, sym, rtype in relocs)
    symtab = b"".join(e.syms)
    names = {n: e.name(e.shstr, n) for n in
             (".blob", ".rel.blob", ".symtab", ".strtab", ".shstrtab")}

    body = [blob, rel, symtab, bytes(e.str), bytes(e.shstr)]
    off = 52
    offs = []
    for b in body:
        off = (off + 3) & ~3
        offs.append(off)
        off += len(b)
    shoff = (off + 3) & ~3

    # sh_info of .symtab is the index of the first non-local symbol; every
    # symbol here is global, so it is 1 (past the null symbol).
    SHT_PROGBITS, SHT_REL, SHT_SYMTAB, SHT_STRTAB = 1, 9, 2, 3
    SHF_WRITE, SHF_ALLOC, SHF_EXEC = 1, 2, 4
    sh = [b"\0" * 40]
    sh.append(struct.pack("<10I", names[".blob"], SHT_PROGBITS,
                          SHF_ALLOC | SHF_EXEC | SHF_WRITE, APP_BASE, offs[0],
                          len(blob), 0, 0, 4, 0))
    sh.append(struct.pack("<10I", names[".rel.blob"], SHT_REL, 0, 0, offs[1],
                          len(rel), 3, 1, 4, 8))
    sh.append(struct.pack("<10I", names[".symtab"], SHT_SYMTAB, 0, 0, offs[2],
                          len(symtab), 4, 1, 4, 16))
    sh.append(struct.pack("<10I", names[".strtab"], SHT_STRTAB, 0, 0, offs[3],
                          len(e.str), 0, 0, 1, 0))
    sh.append(struct.pack("<10I", names[".shstrtab"], SHT_STRTAB, 0, 0, offs[4],
                          len(e.shstr), 0, 0, 1, 0))

    # EF_ARM_EABI_VER5 | EF_ARM_ABI_FLOAT_HARD
    hdr = struct.pack("<16sHHIIIIIHHHHHH",
                      b"\x7fELF\x01\x01\x01\x00" + b"\0" * 8, 1, 40, 1, 0, 0,
                      shoff, 0x05000400, 52, 0, 0, 40, len(sh), 5)
    with open(path, "wb") as f:
        f.write(hdr)
        for o, b in zip(offs, body):
            f.write(b"\0" * (o - f.tell()))
            f.write(b)
        f.write(b"\0" * (shoff - f.tell()))
        f.write(b"".join(sh))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", default=os.path.join(SIM, "out", "appl-blob.o"))
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    args = ap.parse_args()

    b = yaml.safe_load(open(os.path.join(HERE, "boundary.yaml")))
    blob = bytearray(open(args.image, "rb").read())

    by_addr = {int(e["addr"]): e for e in b["app_to_lib"]}
    relocate = {a: e["symbol"] for a, e in by_addr.items()
                if not e.get("keep") and e["symbol"]}

    sites = find_sites(blob, set(by_addr),
                       [(int(lo), int(hi)) for lo, hi in b["library_ranges"]])
    if len(sites) != b["meta"]["app_to_lib"]["sites"]:
        sys.exit("found %d boundary sites, boundary.yaml declares %d"
                 % (len(sites), b["meta"]["app_to_lib"]["sites"]))

    relocs = []
    for addr, tgt in sites:
        if tgt not in relocate:
            continue            # explicitly kept: the blob calls its own copy
        off = addr - APP_BASE
        struct.pack_into("<HH", blob, off, 0xF7FF, 0xFFFE)   # bl .-4: the REL addend is 0
        relocs.append((off, relocate[tgt], R_ARM_THM_CALL))

    for v in b["data_references"]["vector_table"]:
        if not v.get("relocate"):
            continue
        off = v["index"] * 4
        if struct.unpack_from("<I", blob, off)[0] != v["word"]:
            sys.exit("vector %d holds 0x%x, boundary.yaml expects 0x%x"
                     % (v["index"], struct.unpack_from("<I", blob, off)[0], v["word"]))
        struct.pack_into("<I", blob, off, 0)
        relocs.append((off, v["symbol"], R_ARM_ABS32))

    defined = {}
    for e in b["lib_to_app"]:
        if e["symbol"] and not e.get("library_not_app"):
            defined[e["symbol"]] = (int(e["addr"]), STT_FUNC)
    for v in b["data_references"]["vector_table"]:
        if not v.get("relocate"):
            defined.setdefault(v["symbol"], (v["word"] & ~1, STT_FUNC))
    defined["appl_vector_table"] = (APP_BASE, STT_NOTYPE)
    defined["appl_blob_start"] = (APP_BASE, STT_NOTYPE)
    defined["appl_blob_end"] = (APP_BASE + len(blob), STT_NOTYPE)

    os.makedirs(os.path.dirname(args.o), exist_ok=True)
    build(bytes(blob), relocs, defined, args.o)
    thm = sum(1 for _, _, t in relocs if t == R_ARM_THM_CALL)
    print("%s: %d bytes, %d R_ARM_THM_CALL, %d R_ARM_ABS32, %d defined symbols"
          % (args.o, len(blob), thm, len(relocs) - thm, len(defined)))


if __name__ == "__main__":
    main()
