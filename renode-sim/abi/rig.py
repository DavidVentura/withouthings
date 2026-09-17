#!/usr/bin/env python3
"""Resolve abi/sim.yaml against an image and write the rig's Renode fragments.

    python3 abi/rig.py --image flash.bin --symbols partition --out out/rig
    python3 abi/rig.py --image out/flash-relinked.bin \
                       --symbols out/relink/relinked.elf --out out/rig

The sim reaches into the running image at a handful of addresses: instructions
it rewrites before the run, and instructions it hooks. Written as numbers those
addresses are only right for the layout they were read off, so sim.yaml names
each one as a symbol plus an offset and this resolves them against whatever is
about to run: the partition export when the image is the stock flash.bin, which
has no ELF, and the linked ELF for a relink or a moved layout.

Every entry declares the bytes it expects to find. They are checked against the
image itself, so a symbol that resolved to the wrong place, a patch site that
moved inside its function or an image built from a different source is refused
here rather than silently applied to whatever landed at the address.
"""

import argparse
import json
import os
import struct
import sys
import textwrap

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)

STT_FUNC = 2
WRITE = {1: "WriteByte", 2: "WriteWord", 4: "WriteDoubleWord"}


def elf_symbols(path):
    """name -> the addresses an ELF32-LE symbol table gives it, Thumb bit off."""
    data = open(path, "rb").read()
    shoff, = struct.unpack_from("<I", data, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x2E)
    headers = [struct.unpack_from("<10I", data, shoff + i * shentsize)
               for i in range(shnum)]
    table = {}
    for _, styp, _, _, off, size, link, _, _, entsize in headers:
        if styp != 2 or not entsize:            # SHT_SYMTAB
            continue
        stroff = headers[link][4]
        for at in range(entsize, size, entsize):
            name, value, _, info, _, shndx = struct.unpack_from("<IIIBBH", data,
                                                                off + at)
            if not name or not shndx:
                continue
            end = data.index(b"\0", stroff + name)
            sym = data[stroff + name:end].decode()
            # ARM ELF carries Thumb-ness in bit 0 of a function symbol's value.
            if info & 0xF == STT_FUNC:
                value &= ~1
            table.setdefault(sym, set()).add(value)
    # A name two objects define is only a problem for an entry that asks for
    # it, and the source kernel brings names of its own into this table.
    return table


def partition_symbols(export):
    """name -> address for a run with no ELF: the partition's own function names.

    The stock image is what the partition was taken from, so its names are at
    their original addresses; a name the export hands to two functions is
    refused rather than picked between.
    """
    with open(os.path.join(export, "items.json")) as fh:
        items = json.load(fh)
    table, repeated = {}, set()
    for f in items["functions"]:
        if table.setdefault(f["name"], f["start"]) != f["start"]:
            repeated.add(f["name"])
    return {name: {start} for name, start in table.items()
            if name not in repeated}


def resolve(entry, symbols, image, meta):
    """The address one sim.yaml entry resolves to, or exit with what is wrong."""
    width = entry["width"]
    if width not in WRITE:
        sys.exit("%s asks for %d bytes; the bus writes 1, 2 or 4"
                 % (entry["name"], width))
    if "absolute" in entry:
        if "symbol" in entry:
            sys.exit("%s is both absolute and symbolic" % entry["name"])
        address = entry["absolute"]
        if meta["app_base"] <= address < meta["app_end"]:
            sys.exit("%s is absolute but 0x%x is inside the app, where a layout"
                     " moves it" % (entry["name"], address))
    else:
        found_at = symbols.get(entry["symbol"], set())
        if len(found_at) != 1:
            sys.exit("%s names %s, which the symbol table defines %s"
                     % (entry["name"], entry["symbol"],
                        "not at all" if not found_at else
                        "at " + ", ".join("0x%x" % v for v in sorted(found_at))))
        address = next(iter(found_at)) + entry["offset"]
    if address + width > len(image):
        sys.exit("%s resolves to 0x%x, past the end of the image"
                 % (entry["name"], address))
    found = int.from_bytes(image[address:address + width], "little")
    if found != entry["original"]:
        sys.exit("%s resolves to 0x%x, which holds 0x%0*x, not the 0x%0*x it"
                 " expects" % (entry["name"], address, width * 2, found,
                               width * 2, entry["original"]))
    return address


def commented(why, indent):
    return "".join("%s# %s\n" % (indent, line) for line in
                   textwrap.wrap(" ".join(str(why).split()), 78 - len(indent)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.path.join(SIM, "flash.bin"),
                    help="the internal-flash image the run loads at 0")
    ap.add_argument("--symbols", default="partition",
                    help="an ELF to read the symbol table from, or `partition`"
                         " for the export, which is the stock image's table")
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--out", default=os.path.join(SIM, "out", "rig"))
    args = ap.parse_args()

    spec = yaml.safe_load(open(os.path.join(HERE, "sim.yaml")))
    meta = spec["meta"]
    symbols = (partition_symbols(args.export) if args.symbols == "partition"
               else elf_symbols(args.symbols))
    image = open(args.image, "rb").read()

    os.makedirs(args.out, exist_ok=True)
    head = ("# Generated by abi/rig.py from abi/sim.yaml against %s, with the"
            " %s symbol table; do not edit.\n"
            % (os.path.basename(args.image),
               "partition" if args.symbols == "partition"
               else os.path.basename(args.symbols)))
    resolved = 0

    patches = os.path.join(args.out, "patches.resc")
    with open(patches, "w") as fh:
        fh.write(head)
        for macro, entries in spec["macros"].items():
            fh.write("\nmacro %s\n\"\"\"\n" % macro)
            for entry in entries:
                address = resolve(entry, symbols, image, meta)
                resolved += 1
                fh.write(commented(entry["why"], "    "))
                fh.write("    sysbus %s 0x%x 0x%X\n"
                         % (WRITE[entry["width"]], address, entry["new"]))
            fh.write("\"\"\"\n")

    hooks = os.path.join(args.out, "hooks.resc")
    with open(hooks, "w") as fh:
        fh.write(head)
        for entry in spec["hooks"]:
            address = resolve(entry, symbols, image, meta)
            resolved += 1
            fh.write("\n" + commented(entry["why"], ""))
            fh.write("$%s=0x%x\n" % (entry["name"], address))

    print("%s: patches.resc and hooks.resc, %d addresses resolved against %s"
          % (args.out, resolved, args.symbols))
    return 0


if __name__ == "__main__":
    sys.exit(main())
