#!/usr/bin/env python3
"""The chip's register map: what an address in the peripheral ranges is called.

    python3 abi/peripherals.py                       # the map, as counts
    python3 abi/peripherals.py --at 0x40007004       # what one address is
    python3 abi/peripherals.py --words               # the app's words, resolved

The app holds about a hundred of these addresses in its literal pools and the
classifier had nothing to say about any of them: a value outside the image's
flash and RAM is `out_of_range`, which is true and useless. The names are
public -- Nordic ships NRF52840.svd -- so this parses that file into peripheral,
register and field records and answers the one question the rest of the repo
asks: what is at this address.

Two things the SVD does not settle on its own.

`derivedFrom` peripherals carry no registers of their own (SPIM3 is SPIM0 at
another base), so a derived peripheral takes the base one's registers.

Several peripherals share a base, because the hardware block is one and the
programming models are alternatives: SPI0, SPIM0, SPIS0, TWI0, TWIM0 and TWIS0
are all at 0x40003000. Where the register offset picks exactly one of them the
address is unambiguous; where it does not, the alternative this firmware drives
is a fact about the firmware and not about the chip, so it is declared in
abi/words.yaml's `peripherals` block beside the argument for it, and an
ambiguity that block does not resolve is refused rather than guessed at.

The Cortex-M core's own registers are not in the SVD at all -- NVIC, SCB and
the rest are ARM's, not Nordic's -- and the app reads 0xE000Exxx from its pools
like any other peripheral, so the System Control Space is carried here from the
ARMv7-M layout. That layout is architectural: it is the same on every Cortex-M4
and cannot go stale against this image.
"""

import argparse
import collections
import json
import os
import struct
import sys
import xml.etree.ElementTree as ET

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
SVD = os.path.join(SIM, "NRF52840.svd")

# The address ranges a peripheral can live in on this part: the factory and user
# information pages, the APB/AHB peripherals, the GPIO ports, CryptoCell, and
# the Private Peripheral Bus the core's own registers are on. A word outside
# them is not a register whatever it looks like.
REGIONS = ((0x10000000, 0x10002000), (0x40000000, 0x40100000),
           (0x50000000, 0x50001000), (0x5002A000, 0x5002B000),
           (0xE0000000, 0xE0100000))


def in_peripheral_space(value):
    return any(lo <= value < hi for lo, hi in REGIONS)


class Field(object):
    """One named bit range of a register."""

    __slots__ = ("name", "lsb", "msb", "description")

    def __init__(self, name, lsb, msb, description):
        self.name = name
        self.lsb = lsb
        self.msb = msb
        self.description = description

    @property
    def mask(self):
        return ((1 << (self.msb - self.lsb + 1)) - 1) << self.lsb


class Register(object):
    """One register of one peripheral, at its offset from that peripheral's base."""

    __slots__ = ("name", "offset", "width", "access", "description", "fields")

    def __init__(self, name, offset, width, access, description, fields):
        self.name = name
        self.offset = offset
        self.width = width
        # "read-only", "write-only", "read-write"; the SVD states it for every
        # register of this part and the access index reads it as what the
        # hardware permits, which is the half a data-flow summary cannot see.
        self.access = access
        self.description = description
        self.fields = fields

    @property
    def is_task(self):
        return self.name.startswith("TASKS_")

    @property
    def is_event(self):
        return self.name.startswith("EVENTS_")

    def __repr__(self):
        return "<reg %s +0x%x>" % (self.name, self.offset)


class Peripheral(object):
    """One peripheral instance: a name, a base address and its registers."""

    __slots__ = ("name", "base", "size", "group", "description", "registers",
                 "by_offset")

    def __init__(self, name, base, size, group, description, registers):
        self.name = name
        self.base = base
        self.size = size
        self.group = group
        self.description = description
        self.registers = registers
        self.by_offset = {}
        for reg in registers:
            self.by_offset.setdefault(reg.offset, reg)

    @property
    def end(self):
        return self.base + self.size

    def __repr__(self):
        return "<periph %s @0x%x, %d regs>" % (self.name, self.base,
                                               len(self.registers))


class Location(object):
    """An address resolved against the register map."""

    __slots__ = ("address", "peripheral", "register")

    def __init__(self, address, peripheral, register):
        self.address = address
        self.peripheral = peripheral
        # None where the address is the peripheral's base and no register sits
        # at offset zero, which is how the code names the block itself.
        self.register = register

    @property
    def name(self):
        if self.register is None:
            return "NRF_%s" % self.peripheral.name
        return "NRF_%s->%s" % (self.peripheral.name, self.register.name)

    @property
    def access(self):
        return self.register.access if self.register else "read-write"

    def __repr__(self):
        return "<%s @0x%08x>" % (self.name, self.address)


class Ambiguous(Exception):
    """Two peripherals claim the same address and nothing says which is meant."""


# The System Control Space, from the ARMv7-M architecture reference manual. The
# blocks are the ones a firmware addresses by base plus offset; CMSIS's own
# reserved gaps are left out, so an address in one of them resolves to nothing
# rather than to the register on the far side of the hole.
CORE_BLOCKS = [
    ("ITM", 0xE0000000, 0x1000, "Instrumentation Trace Macrocell",
     [("STIM", 0x000, 0x100, "read-write"), ("TER", 0xE00, 0x20, "read-write"),
      ("TPR", 0xE40, 4, "read-write"), ("TCR", 0xE80, 4, "read-write")]),
    ("DWT", 0xE0001000, 0x1000, "Data Watchpoint and Trace",
     [("CTRL", 0x000, 4, "read-write"), ("CYCCNT", 0x004, 4, "read-write"),
      ("CPICNT", 0x008, 4, "read-write"), ("EXCCNT", 0x00C, 4, "read-write"),
      ("SLEEPCNT", 0x010, 4, "read-write"), ("LSUCNT", 0x014, 4, "read-write"),
      ("FOLDCNT", 0x018, 4, "read-write"), ("PCSR", 0x01C, 4, "read-only")]),
    ("SCnSCB", 0xE000E000, 0x10, "System control block, auxiliary",
     [("ICTR", 0x004, 4, "read-only"), ("ACTLR", 0x008, 4, "read-write")]),
    ("SysTick", 0xE000E010, 0x10, "System timer",
     [("CTRL", 0x000, 4, "read-write"), ("LOAD", 0x004, 4, "read-write"),
      ("VAL", 0x008, 4, "read-write"), ("CALIB", 0x00C, 4, "read-only")]),
    ("NVIC", 0xE000E100, 0x400, "Nested vectored interrupt controller",
     [("ISER", 0x000, 8 * 4, "read-write"), ("ICER", 0x080, 8 * 4, "read-write"),
      ("ISPR", 0x100, 8 * 4, "read-write"), ("ICPR", 0x180, 8 * 4, "read-write"),
      ("IABR", 0x200, 8 * 4, "read-only"), ("IPR", 0x300, 60 * 4, "read-write")]),
    ("SCB", 0xE000ED00, 0x90, "System control block",
     [("CPUID", 0x00, 4, "read-only"), ("ICSR", 0x04, 4, "read-write"),
      ("VTOR", 0x08, 4, "read-write"), ("AIRCR", 0x0C, 4, "read-write"),
      ("SCR", 0x10, 4, "read-write"), ("CCR", 0x14, 4, "read-write"),
      ("SHPR1", 0x18, 4, "read-write"), ("SHPR2", 0x1C, 4, "read-write"),
      ("SHPR3", 0x20, 4, "read-write"), ("SHCSR", 0x24, 4, "read-write"),
      ("CFSR", 0x28, 4, "read-write"), ("HFSR", 0x2C, 4, "read-write"),
      ("DFSR", 0x30, 4, "read-write"), ("MMFAR", 0x34, 4, "read-write"),
      ("BFAR", 0x38, 4, "read-write"), ("AFSR", 0x3C, 4, "read-write"),
      ("CPACR", 0x88, 4, "read-write")]),
    ("MPU", 0xE000ED90, 0x40, "Memory protection unit",
     [("TYPE", 0x00, 4, "read-only"), ("CTRL", 0x04, 4, "read-write"),
      ("RNR", 0x08, 4, "read-write"), ("RBAR", 0x0C, 4, "read-write"),
      ("RASR", 0x10, 4, "read-write")]),
    ("FPU", 0xE000EF30, 0x10, "Floating point unit",
     [("FPCCR", 0x04, 4, "read-write"), ("FPCAR", 0x08, 4, "read-write"),
      ("FPDSCR", 0x0C, 4, "read-write")]),
    ("DBG", 0xE000EDF0, 0x10, "Debug control",
     [("DHCSR", 0x00, 4, "read-write"), ("DCRSR", 0x04, 4, "write-only"),
      ("DCRDR", 0x08, 4, "read-write"), ("DEMCR", 0x0C, 4, "read-write")]),
]


def core_peripherals():
    """The System Control Space as Peripheral records.

    A block declared wider than four bytes is an array in the architecture --
    NVIC's eight ISER words, its sixty priority words -- so it is expanded into
    one indexed register per word, which is what makes 0xE000E104 come out as
    `NVIC->ISER[1]` rather than as the base plus a number.
    """
    out = []
    for name, base, size, description, regs in CORE_BLOCKS:
        records = []
        for reg, offset, width, access in regs:
            if width == 4:
                records.append(Register(reg, offset, 4, access, description, ()))
                continue
            for i in range(width // 4):
                records.append(Register("%s[%d]" % (reg, i), offset + 4 * i, 4,
                                        access, description, ()))
        out.append(Peripheral(name, base, size, name, description,
                              tuple(records)))
    return out


def as_int(text):
    return None if text is None else int(text, 0)


def parse_fields(node):
    out = []
    holder = node.find("fields")
    if holder is None:
        return ()
    for f in holder.findall("field"):
        lsb, msb = as_int(f.findtext("lsb")), as_int(f.findtext("msb"))
        if lsb is None:
            offset = as_int(f.findtext("bitOffset"))
            width = as_int(f.findtext("bitWidth"))
            if offset is None or width is None:
                continue
            lsb, msb = offset, offset + width - 1
        out.append(Field(f.findtext("name"), lsb, msb, f.findtext("description")))
    return tuple(out)


def expand(name, count):
    """The names a `dim` register or cluster stands for.

    The SVD spells an array either as `CH[%s]` or as `EVENTS_END_%s`; both mean
    the same thing and both are written here the way C would index them.
    """
    if name.endswith("[%s]"):
        stem = name[:-len("[%s]")]
        return ["%s[%d]" % (stem, i) for i in range(count)]
    if "%s" in name:
        return [name.replace("%s", str(i)) for i in range(count)]
    return ["%s[%d]" % (name, i) for i in range(count)]


def parse_registers(holder, base_offset, default_width, prefix=""):
    """Every register under one `<registers>` or `<cluster>` node, flattened."""
    out = []
    for node in holder:
        if node.tag == "cluster":
            offset = as_int(node.findtext("addressOffset")) + base_offset
            dim = as_int(node.findtext("dim"))
            step = as_int(node.findtext("dimIncrement")) or 0
            names = (expand(node.findtext("name"), dim) if dim
                     else [node.findtext("name")])
            for i, name in enumerate(names):
                out.extend(parse_registers(node, offset + i * step,
                                           default_width,
                                           "%s%s." % (prefix, name)))
        elif node.tag == "register":
            offset = as_int(node.findtext("addressOffset")) + base_offset
            width = as_int(node.findtext("size")) or default_width
            access = node.findtext("access") or "read-write"
            description = node.findtext("description")
            dim = as_int(node.findtext("dim"))
            step = as_int(node.findtext("dimIncrement")) or 0
            names = (expand(node.findtext("name"), dim) if dim
                     else [node.findtext("name")])
            fields = parse_fields(node)
            for i, name in enumerate(names):
                out.append(Register(prefix + name, offset + i * step,
                                    width // 8, access, description, fields))
    return out


def parse_svd(path=SVD):
    """NRF52840.svd -> the Peripheral records, `derivedFrom` resolved."""
    root = ET.parse(path).getroot()
    nodes = collections.OrderedDict(
        (p.findtext("name"), p) for p in root.find("peripherals"))
    built = collections.OrderedDict()
    for name, node in nodes.items():
        source = node
        derived = node.get("derivedFrom")
        if derived is not None:
            if derived not in nodes:
                sys.exit("%s: %s derives from %s, which the file does not"
                         " declare" % (path, name, derived))
            source = nodes[derived]
        holder = source.find("registers")
        if holder is None:
            sys.exit("%s: %s has no registers and derives from nothing"
                     % (path, name))
        width = as_int(source.findtext("size")) or 32
        block = source.find("addressBlock")
        size = as_int(block.findtext("size")) if block is not None else 0x1000
        built[name] = Peripheral(
            name, as_int(node.findtext("baseAddress")), size,
            node.findtext("groupName") or name,
            node.findtext("description") or source.findtext("description"),
            tuple(parse_registers(holder, 0, width)))
    return list(built.values())


class Chip(object):
    """The whole register map, and the lookup the rest of the repo uses."""

    def __init__(self, peripherals, resolutions):
        self.peripherals = list(peripherals)
        self.by_name = {p.name: p for p in self.peripherals}
        # Which alternative owns a shared base, where the offset cannot say.
        self.resolutions = dict(resolutions)
        for base, name in sorted(self.resolutions.items()):
            chosen = self.by_name.get(name)
            if chosen is None or chosen.base != base:
                sys.exit("abi/words.yaml gives 0x%08x to %s, which the SVD does"
                         " not put there" % (base, name))
        self.bases = collections.defaultdict(list)
        for p in self.peripherals:
            self.bases[p.base].append(p)

    def candidates(self, address):
        return [p for p in self.peripherals if p.base <= address < p.end]

    def at(self, address):
        """The Location `address` names, or None.

        A candidate that has a register exactly at the offset beats one that
        does not, because a peripheral covering the address without declaring
        anything there is the block's span and not a name for the word.
        """
        if not in_peripheral_space(address):
            return None
        holders = self.candidates(address)
        if not holders:
            return None
        exact = [(p, p.by_offset[address - p.base]) for p in holders
                 if address - p.base in p.by_offset]
        if exact:
            chosen = self._one(address, [p for p, _ in exact])
            return Location(address, chosen, dict(exact)[chosen])
        bases = [p for p in holders if p.base == address]
        if not bases:
            return None
        return Location(address, self._one(address, bases), None)

    def _one(self, address, holders):
        if len(holders) == 1:
            return holders[0]
        declared = [p for p in holders if self.resolutions.get(p.base) == p.name]
        if len(declared) == 1:
            return declared[0]
        raise Ambiguous(
            "0x%08x is %s and nothing says which; declare the base in"
            " abi/words.yaml's peripherals block"
            % (address, " and ".join(sorted(p.name for p in holders))))

    def register_after(self, base_address, displacement):
        """The register `base + displacement` selects, for a word holding a base.

        This is the other half of the shape: the pool word holds the peripheral
        and the reading instruction carries the offset as an immediate, so the
        word alone names the block and the pair names the register.
        """
        return self.at((base_address + displacement) & 0xFFFFFFFF)


def resolutions(path=None):
    """abi/words.yaml's `peripherals` block: base -> the alternative driven."""
    path = path or os.path.join(HERE, "words.yaml")
    spec = yaml.safe_load(open(path))
    out = {}
    for entry in spec.get("peripherals") or ():
        for key in ("base", "peripheral", "why"):
            if key not in entry:
                sys.exit("abi/words.yaml: a peripherals entry without %s" % key)
        if entry["base"] in out:
            sys.exit("abi/words.yaml declares the base 0x%x twice"
                     % entry["base"])
        out[entry["base"]] = entry["peripheral"]
    return out


def load(svd=SVD, words=None):
    return Chip(parse_svd(svd) + core_peripherals(), resolutions(words))


# What a word in the peripheral ranges is when it is not a register: this image
# holds its float constants in the same pools, and a single-precision float
# with a small exponent lands in 0x3f000000..0x4f000000 the same way a
# peripheral base does. 0x42c80000 is 100.0.
FLOAT_MIN, FLOAT_MAX = 1e-4, 1e9
FLOAT_DIGITS = 6


def plausible_float(value):
    """The float `value` decodes to if that reading is a plausible constant.

    Plausible means three things at once: the bits are a normal number of an
    everyday magnitude, and the decimal it prints as reproduces the bits
    exactly. A number a person wrote in the source round-trips through seven
    significant digits; an address that happens to decode as a float does not,
    because its low bits are an offset rather than a mantissa.
    """
    number, = struct.unpack("<f", struct.pack("<I", value))
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if not FLOAT_MIN <= abs(number) <= FLOAT_MAX:
        return None
    short = float("%.*g" % (FLOAT_DIGITS, number))
    if struct.unpack("<I", struct.pack("<f", short))[0] != value:
        return None
    return short


def report_words(chip, export):
    """Which words abi/classify_words.py decided by the register map, per block."""
    with open(os.path.join(export, "words.json")) as fh:
        words = json.load(fh)["words"]
    with open(os.path.join(export, "references.json")) as fh:
        refs = json.load(fh)
    with open(os.path.join(export, "items.json")) as fh:
        items = json.load(fh)
    named = {f["start"]: f["name"] for f in items["functions"]}
    sites = collections.defaultdict(list)
    for read in refs["pool_reads"]:
        sites[read["target"]].append(read)
    rows = [w for w in words if w["signal"] == "peripheral"]
    per = collections.defaultdict(list)
    for word in rows:
        found = chip.at(word["value"])
        per[found.peripheral.name].append(word)
    total = sum(len(sites[w["addr"]]) for w in rows)
    print("%d words name a register, over %d peripherals, read from %d sites"
          % (len(rows), len(per), total))
    for name in sorted(per, key=lambda n: (-len(per[n]), n)):
        held = per[name]
        readers = sorted(set(named.get(r["function"], "0x%x" % r["function"])
                             for w in held for r in sites[w["addr"]]))
        print("  %-10s %3d words, %3d sites: %s"
              % (name, len(held), sum(len(sites[w["addr"]]) for w in held),
                 ", ".join(readers)))
        shapes = collections.Counter()
        for word in held:
            shapes[(word["value"], word["note"])] += len(sites[word["addr"]])
        for (value, note), count in sorted(shapes.items()):
            print("      0x%08x  %-3d sites  %s" % (value, count, note))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--svd", default=SVD)
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--at", help="resolve one address")
    ap.add_argument("--words", action="store_true",
                    help="resolve every candidate word of the app image")
    args = ap.parse_args()
    chip = load(args.svd)
    if args.at:
        at = int(args.at, 0)
        found = chip.at(at)
        print("0x%08x: %s" % (at, found.name if found else "no register"))
        if found and found.register:
            print("  %s, %s" % (found.register.access,
                                found.register.description or ""))
            for f in found.register.fields:
                print("  [%d:%d] %s" % (f.msb, f.lsb, f.name))
        return 0
    if args.words:
        return report_words(chip, args.export)
    print("%d peripherals, %d registers"
          % (len(chip.peripherals), sum(len(p.registers) for p in chip.peripherals)))
    for p in sorted(chip.peripherals, key=lambda p: p.base):
        print("  0x%08x %-14s %4d registers" % (p.base, p.name, len(p.registers)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
