#!/usr/bin/env python3
"""Resolve abi/sim.yaml against an image and write the rig's Renode fragments.

    python3 abi/rig.py --image flash.bin --symbols partition --out out/rig
    python3 abi/rig.py --image out/flash-relinked.bin \
                       --symbols out/relink/relinked.elf --out out/rig

The sim reaches into the running image at a handful of addresses: instructions
it rewrites before the run, and instructions it hooks. Written as numbers those
addresses are only right for the layout they were read off, so sim.yaml names
each one as its original address plus an offset and this resolves them against
whatever is about to run: the partition export when the image is the stock
flash.bin, which has no ELF and where every original address is still itself,
and the linked ELF for a relink or a moved layout, where abi/blobify.py's
`a_<original address>` alias says where the body went.

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

import blobify

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
    """The address-keyed table for a run with no ELF, plus the names for the report.

    The stock image is what the partition was taken from, so every original
    address is still itself; what the export adds is the check that the address
    is the start of something the partition cut, and the name to show when an
    entry carries one.
    """
    with open(os.path.join(export, "items.json")) as fh:
        items = json.load(fh)
    starts = {}
    for f in items["functions"]:
        starts.setdefault(f["start"], f["name"])
    for d in items["data"]:
        starts.setdefault(d["start"], d["name"])
    return {blobify.address_alias(addr): {addr} for addr in starts}, starts


def check_name(what, address, given, names):
    """A `symbol:` is the partition's name for the address, and only informational.

    The address is the key because it is the one thing no renaming moves; an
    entry may still carry the name, and then it has to be the name the export
    gives that address, so a file written against a different cut is refused
    rather than applied to whatever the address now holds.
    """
    if given is None:
        return
    found = names.get(address)
    if found != given:
        sys.exit("%s names 0x%x as %s, which the partition calls %s"
                 % (what, address, given, found or "nothing"))


def resolve(entry, symbols, names, image, meta):
    """The address one sim.yaml entry resolves to, or exit with what is wrong."""
    width = entry["width"]
    if width not in WRITE:
        sys.exit("%s asks for %d bytes; the bus writes 1, 2 or 4"
                 % (entry["name"], width))
    if "absolute" in entry:
        if "address" in entry:
            sys.exit("%s is both absolute and address-keyed" % entry["name"])
        address = entry["absolute"]
        if meta["app_base"] <= address < meta["app_end"]:
            sys.exit("%s is absolute but 0x%x is inside the app, where a layout"
                     " moves it" % (entry["name"], address))
    else:
        original = int(str(entry["address"]), 0)
        check_name(entry["name"], original, entry.get("symbol"), names)
        alias = blobify.address_alias(original)
        found_at = symbols.get(alias, set())
        if len(found_at) != 1:
            sys.exit("%s names 0x%x, which the symbol table defines %s as %s"
                     % (entry["name"], original,
                        "not at all" if not found_at else
                        "at " + ", ".join("0x%x" % v for v in sorted(found_at)),
                        alias))
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


def resolve_variable(entry, symbols):
    """Where one sim.yaml RAM cell is in the run about to start.

    A RAM cell is not in the image, so there are no bytes to check and no
    partition item to key on; the two answers are which kernel the run carries.
    A relink against the source kernel brings the cell's own definition with it
    and the linked ELF is the only thing that knows where the link put it. A
    stock run, and a relink that replaces something else and keeps the blob's
    kernel, has the image's cell where the analysis found it, which is `stock`.
    """
    at = symbols.get(entry["symbol"], set())
    if len(at) > 1:
        sys.exit("%s: the symbol table defines %s at %s"
                 % (entry["name"], entry["symbol"],
                    ", ".join("0x%x" % v for v in sorted(at))))
    if at:
        address, how = next(iter(at)), "the source kernel's own, from the linked ELF"
    else:
        address, how = (int(str(entry["stock"]), 0),
                        "the image's own; the link brought no %s" % entry["symbol"])
    if not 0x20000000 <= address < 0x20040000:
        sys.exit("%s resolves to 0x%x, which is not SRAM"
                 % (entry["name"], address))
    return address, how


def resolve_ram(entry, symbols, linked):
    """Where one sim.yaml RAM item is in the run about to start.

    The address the entry carries is the one the item has in the stock image,
    which a run with no ELF is; a link answers through abi/blobify.py's alias
    for that address, which is where the placement put the item's section.
    """
    address = int(str(entry["address"]), 0)
    if not 0x20000000 <= address < 0x20040000:
        sys.exit("%s names 0x%x, which is not SRAM" % (entry["name"], address))
    if linked:
        alias = blobify.address_alias(address)
        placed = symbols.get(alias, set())
        if len(placed) != 1:
            sys.exit("%s names 0x%x, which the symbol table defines %s as %s"
                     % (entry["name"], address,
                        "not at all" if not placed else
                        "at " + ", ".join("0x%x" % v for v in sorted(placed)),
                        alias))
        address = next(iter(placed))
    if not 0x20000000 <= address < 0x20040000:
        sys.exit("%s resolves to 0x%x, which is not SRAM"
                 % (entry["name"], address))
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
    symbols, names = partition_symbols(args.export)
    if args.symbols != "partition":
        symbols = elf_symbols(args.symbols)
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
                address = resolve(entry, symbols, names, image, meta)
                resolved += 1
                fh.write(commented(entry["why"], "    "))
                fh.write("    sysbus %s 0x%x 0x%X\n"
                         % (WRITE[entry["width"]], address, entry["new"]))
            fh.write("\"\"\"\n")

    hooks = os.path.join(args.out, "hooks.resc")
    with open(hooks, "w") as fh:
        fh.write(head)
        for entry in spec["hooks"]:
            address = resolve(entry, symbols, names, image, meta)
            resolved += 1
            fh.write("\n" + commented(entry["why"], ""))
            fh.write("$%s=0x%x\n" % (entry["name"], address))
        for entry in spec["ram"]:
            address = resolve_ram(entry, symbols, args.symbols != "partition")
            resolved += 1
            fh.write("\n" + commented(entry["why"], ""))
            fh.write("$%s=0x%x\n" % (entry["name"], address))
        for entry in spec["variables"]:
            address, how = resolve_variable(entry, symbols)
            resolved += 1
            fh.write("\n" + commented(entry["why"], ""))
            fh.write("# %s\n$%s=0x%x\n" % (how, entry["name"], address))

    print("%s: patches.resc and hooks.resc, %d addresses resolved against %s"
          % (args.out, resolved, args.symbols))
    return 0


if __name__ == "__main__":
    sys.exit(main())
