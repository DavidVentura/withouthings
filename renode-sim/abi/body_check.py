#!/usr/bin/env python3
"""Compare function bodies in a relocatable ELF against the image, byte for byte.

    python3 abi/body_check.py --elf ~/ref-build/build/n2_0_0_Os/ref.elf \
        --pair saadc_busy_check=0x7db08 --pair nrfx_saadc_uninit=0x7e138
    python3 abi/body_check.py --elf <elf> --pairs abi/saadc_bodies.yaml --dis

abi/libc_check.py asks this question of an archive; this asks it of one object
or partial link, which is what a driver built from source is. A byte the build
and the image disagree on is a divergence unless a relocation owns it, because
a relocated field is written by the link and cannot agree before one.

--dis prints the disassembly of both sides side by side from the first
divergence, which is how a source patch gets written.
"""

import argparse
import os
import re
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
APP_BASE = 0x27000
OBJDUMP = ["llvm-objdump", "-d", "--triple=thumbv7em-none-eabi", "--mattr=+vfp4"]
INSN = re.compile(r"^\s*([0-9a-f]+):\s+((?:[0-9a-f]{2,4} )+)\s*\t(\S+)\s*(.*)$")


class Object(object):
    """Function bodies of one relocatable ELF, by symbol name."""

    def __init__(self, path, tools=""):
        self.path, self.tools = path, tools
        self.bodies = {}
        listing = self.run(["objdump", "-t", path])
        found, offsets, self.sizes = [], {}, self.section_sizes(path)
        for line in listing.splitlines():
            row = line.split()
            sections = [w for w in row if w.startswith(".text")]
            if len(row) < 6 or "F" not in row[2:5] or not sections:
                continue
            section = sections[0]
            off = int(row[0], 16)
            found.append((section, off, int(row[row.index(section) + 1], 16),
                          row[-1], row[1]))
            offsets.setdefault(section, set()).add(off)
        for section, off, size, name, bind in found:
            if not size:
                after = sorted(o for o in offsets[section] if o > off)
                size = (after[0] if after else self.sizes.get(section, off)) - off
            if size and name not in self.bodies:
                self.bodies[name] = (section, off, size, bind)
        self.contents, self.relocs = {}, {}

    def run(self, argv):
        argv = [self.tools + argv[0]] + argv[1:]
        return subprocess.run(argv, capture_output=True, text=True, check=True).stdout

    def section_sizes(self, path):
        sizes = {}
        for line in self.run(["objdump", "-h", path]).splitlines():
            row = line.split()
            if len(row) > 2 and row[1].startswith(".text"):
                sizes[row[1]] = int(row[2], 16)
        return sizes

    def section_bytes(self, section):
        if section not in self.contents:
            out = "/tmp/body_check_section.bin"
            subprocess.run([self.tools + "objcopy", "-O", "binary",
                            "--only-section", section, self.path, out], check=True)
            with open(out, "rb") as fh:
                self.contents[section] = fh.read()
        return self.contents[section]

    def relocated(self, section):
        """Byte offsets in a section that a relocation owns."""
        if section in self.relocs:
            return self.relocs[section]
        owned, current = set(), None
        for line in self.run(["objdump", "-r", self.path]).splitlines():
            if line.startswith("RELOCATION RECORDS FOR ["):
                current = line.split("[")[1].split("]")[0]
                continue
            row = line.split()
            if current != section or len(row) < 2:
                continue
            try:
                at = int(row[0], 16)
            except ValueError:
                continue
            owned.update(range(at, at + 4))
        self.relocs[section] = owned
        return owned

    def body(self, name):
        section, off, size, _ = self.bodies[name]
        blob = self.section_bytes(section)[off:off + size]
        owned = set(i - off for i in self.relocated(section)
                    if off <= i < off + size)
        return blob, owned


def disassemble(data, base):
    """(address, mnemonic, operands) for a blob of Thumb code."""
    raw = "/tmp/body_check_dis.bin"
    with open(raw, "wb") as fh:
        fh.write(data)
    elf = raw + ".elf"
    subprocess.run(["llvm-objcopy", "-I", "binary", "-O", "elf32-littlearm",
                    "--rename-section", ".data=.text,alloc,load,readonly,code",
                    raw, elf], check=True)
    out = subprocess.run(OBJDUMP + ["--adjust-vma=0x%x" % base, elf],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        m = INSN.match(line)
        if m:
            rows.append((int(m.group(1), 16), m.group(3), m.group(4).split("@")[0].strip()))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--elf", required=True)
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--base", type=lambda s: int(s, 0), default=APP_BASE)
    ap.add_argument("--pair", action="append", default=[],
                    help="symbol=0xaddress, repeatable")
    ap.add_argument("--pairs", help="YAML list of {symbol, address}")
    ap.add_argument("--tools", default="arm-none-eabi-")
    ap.add_argument("--dis", action="store_true",
                    help="print both disassemblies for every body that differs")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    pairs = []
    for p in args.pair:
        name, _, addr = p.partition("=")
        pairs.append((name, int(addr, 0)))
    if args.pairs:
        for row in yaml.safe_load(open(args.pairs)):
            pairs.append((row["symbol"], int(str(row["address"]), 0)))

    obj = Object(args.elf, args.tools)
    with open(args.image, "rb") as fh:
        image = fh.read()

    verdicts, worst = [], 0
    for name, addr in pairs:
        if name not in obj.bodies:
            verdicts.append((name, addr, "absent", 0, 0))
            continue
        built, owned = obj.body(name)
        there = image[addr - args.base:addr - args.base + len(built)]
        bad = [i for i in range(len(built))
               if built[i] != there[i] and i not in owned]
        verdict = "exact" if built == there else ("masked" if not bad else "differs")
        verdicts.append((name, addr, verdict, len(bad), len(built)))
        if verdict == "differs":
            worst += 1
        if args.dis and verdict == "differs":
            print("=== %s @0x%x: %d of %d bytes differ outside relocated fields"
                  % (name, addr, len(bad), len(built)))
            ref = disassemble(built, addr)
            img = disassemble(there, addr)
            for i in range(max(len(ref), len(img))):
                r = "%-32s" % ("%s %s" % ref[i][1:] if i < len(ref) else "")
                g = "%s %s" % img[i][1:] if i < len(img) else ""
                flag = " " if r.strip() == g.strip() else "|"
                print("  0x%05x %s %s %s"
                      % (ref[i][0] if i < len(ref) else 0, r, flag, g))

    if not args.quiet:
        for name, addr, verdict, bad, size in verdicts:
            print("%-34s 0x%05x  %-8s %s"
                  % (name, addr, verdict,
                     "%d/%d bytes differ" % (bad, size) if bad else "%d bytes" % size))
    return 1 if worst else 0


if __name__ == "__main__":
    sys.exit(main())
