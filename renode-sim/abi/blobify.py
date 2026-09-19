#!/usr/bin/env python3
"""Emit appl.bin as an ELF32 ARM relocatable object, one section per item.

    python3 abi/blobify.py [-o out/relink/appl-blob.o]

The partition (abi/ghidra/analyze.sh -> abi/out/ghidra/items.json) cuts the app
image into functions, data items, literal pools and gaps that tile it exactly;
abi/objectify.py turns that cover into one `.text.<name>` per function and one
`.rodata.<name>` per data item, and every control transfer that leaves a section
becomes a relocation, so the linker is free to place them. Alongside the object
this writes the SECTIONS fragment that pins each section at its original
address (out/relink-place.ld) and the definitions that make a link without the
source library possible (out/relink/stock-defs.o); together those two are the
byte-identical proof that the cutting lost nothing, which abi/identity.sh runs.

The library boundary (abi/boundary.yaml) takes precedence over the partition:
every app->library `bl` whose target is named and not marked `keep` becomes an
R_ARM_THM_CALL against that library symbol, every app->library `b.w` (a tail
call) an R_ARM_THM_JUMP24, and every vector-table word marked `relocate` an
R_ARM_ABS32, so those resolve to the source build rather than to the blob's own
copy of the library. Sites marked `keep` are ordinary internal calls into the
blob's copy. Library->app targets and the app's entry points become global
symbols so the source library can be linked against them; everything the
partition names stays local, because the blob's copy of the kernel carries the
same names as the source build.

The ELF structures are written by hand (no pyelftools/lief dependency); the
output is a plain REL object that arm-none-eabi-ld consumes directly.
"""

import argparse
import collections
import json
import os
import struct
import sys

import yaml

import archive_cost

import datagen
import objectify
import ramparts
import shapes
import symbols as symmap
from objectify import (APP_BASE, APP_END, R_ARM_ABS32, R_ARM_THM_CALL,
                       R_ARM_THM_JUMP24, R_ARM_THM_JUMP19, SELF_BL, SELF_B)

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)

STT_NOTYPE, STT_OBJECT, STT_FUNC = 0, 1, 2
STB_LOCAL, STB_GLOBAL = 0, 1
SHN_ABS = 0xFFF1


def address_alias(addr):
    """The name the object gives an original image address, whatever it is called."""
    return "a_%08x" % addr


def decode_branch(data, off):
    """-> ("bl"|"b.w", target), or None if these four bytes are neither."""
    hi, lo = struct.unpack_from("<HH", data, off)
    if (hi & 0xF800) != 0xF000:
        return None
    kind = {0xD000: "bl", 0x9000: "b.w"}.get(lo & 0xD000)
    if kind is None:
        return None
    s = (hi >> 10) & 1
    j1, j2 = (lo >> 13) & 1, (lo >> 11) & 1
    imm = (s << 24) | ((1 - (j1 ^ s)) << 23) | ((1 - (j2 ^ s)) << 22) \
        | ((hi & 0x3FF) << 12) | ((lo & 0x7FF) << 1)
    if s:
        imm -= 1 << 25
    return kind, APP_BASE + off + 4 + imm


def find_sites(data, wanted, spans):
    """Every BL and B.W in the image whose target is one of `wanted`.

    The scan is over raw halfword-aligned bytes rather than a disassembly,
    because a linear disassembly desynchronises on literal pools. It agrees
    exactly with abi/match.py's disassembly on this image (647 BL, 96 B.W),
    which is what makes a data word accidentally decoding as a boundary branch
    implausible.
    """
    sites = []
    for off in range(0, len(data) - 3, 2):
        addr = APP_BASE + off
        if any(lo <= addr <= hi for lo, hi in spans):
            continue            # library calling itself, not the boundary
        found = decode_branch(data, off)
        if found is not None and found[1] in wanted:
            sites.append((addr, found[1], found[0]))
    return sites


class Symbols(object):
    """The object's symbol table: the partition's names local, the boundary's global."""

    def __init__(self):
        self.rows = []              # (name, value, shndx, bind, type)
        self.index = {}

    def add(self, name, value, shndx, bind, styp):
        if name in self.index:
            return self.index[name]
        self.index[name] = len(self.rows)
        self.rows.append((name, value, shndx, bind, styp))
        return self.index[name]

    def undefined(self, name):
        return self.add(name, 0, 0, STB_GLOBAL, STT_NOTYPE)

    def globalise(self, name, value, shndx, styp):
        """A name the source library links against: global whatever the partition made it."""
        if name in self.index:
            i = self.index[name]
            old = self.rows[i]
            if old[1] != value or old[2] != shndx:
                sys.exit("symbol %s is 0x%x in section %d and 0x%x in section %d"
                         % (name, old[1], old[2], value, shndx))
            self.rows[i] = (name, value, shndx, STB_GLOBAL, styp)
            return i
        return self.add(name, value, shndx, STB_GLOBAL, styp)

    def ordered(self):
        """Locals first, as ELF requires; returns (rows, first global index)."""
        locals_ = [r for r in self.rows if r[3] == STB_LOCAL]
        globals_ = [r for r in self.rows if r[3] != STB_LOCAL]
        order = locals_ + globals_
        self.index = {r[0]: i + 1 for i, r in enumerate(order)}
        return order, len(locals_) + 1


def build(sections, blob, symbols, path):
    """Write the object: PROGBITS per section, one REL per section that needs it."""
    shstr, strtab = bytearray(b"\0"), bytearray(b"\0")

    def name_in(table, s):
        off = len(table)
        table += s.encode() + b"\0"
        return off

    rel_of = [(i + 1, s) for i, s in enumerate(sections) if s.relocs]
    sym_rows, first_global = symbols.ordered()
    symtab = bytearray(b"\0" * 16)
    for sym, value, shndx, bind, styp in sym_rows:
        symtab += struct.pack("<IIIBBH", name_in(strtab, sym), value, 0,
                              (bind << 4) | styp, 0, shndx)

    bodies, headers = [], [b"\0" * 40]
    SHT_PROGBITS, SHT_NOBITS, SHT_REL, SHT_SYMTAB, SHT_STRTAB = 1, 8, 9, 2, 3
    SHF_WRITE, SHF_ALLOC, SHF_EXEC = 1, 2, 4
    symtab_index = 1 + len(sections) + len(rel_of)

    for s in sections:
        # A NOBITS section has a size and no bytes; the size is what the linker
        # reserves and the startup's zero fill is what fills it.
        bodies.append(b"" if s.nobits
                      else bytes(blob[s.start - APP_BASE:s.end - APP_BASE]))
        align = 4 if s.start % 4 == 0 else (2 if s.start % 2 == 0 else 1)
        flags = SHF_ALLOC | (SHF_EXEC if s.kind == "code" else 0) \
            | (SHF_WRITE if s.vma is not None else 0)
        headers.append([name_in(shstr, s.name),
                        SHT_NOBITS if s.nobits else SHT_PROGBITS, flags, 0, 0,
                        s.end - s.start, 0, 0, align, 0])
    for target, s in rel_of:
        rel = b"".join(struct.pack("<II", off, (symbols.index[sym] << 8) | rtype)
                       for off, sym, rtype in s.relocs)
        bodies.append(rel)
        headers.append([name_in(shstr, ".rel" + s.name), SHT_REL, 0, 0, 0,
                        len(rel), symtab_index, target, 4, 8])
    bodies.append(bytes(symtab))
    headers.append([name_in(shstr, ".symtab"), SHT_SYMTAB, 0, 0, 0, len(symtab),
                    symtab_index + 1, first_global, 4, 16])
    bodies.append(bytes(strtab))
    headers.append([name_in(shstr, ".strtab"), SHT_STRTAB, 0, 0, 0, len(strtab),
                    0, 0, 1, 0])
    shstr_index = len(headers)
    bodies.append(None)             # .shstrtab, whose own name is in itself
    headers.append([name_in(shstr, ".shstrtab"), SHT_STRTAB, 0, 0, 0, 0, 0, 0, 1, 0])
    bodies[-1] = bytes(shstr)
    headers[-1][5] = len(shstr)

    off = 52
    for i, body in enumerate(bodies):
        off = (off + 3) & ~3
        headers[i + 1][4] = off
        off += len(body)
    shoff = (off + 3) & ~3

    # EF_ARM_EABI_VER5 | EF_ARM_ABI_FLOAT_HARD
    hdr = struct.pack("<16sHHIIIIIHHHHHH",
                      b"\x7fELF\x01\x01\x01\x00" + b"\0" * 8, 1, 40, 1, 0, 0,
                      shoff, 0x05000400, 52, 0, 0, 40, len(headers), shstr_index)
    with open(path, "wb") as f:
        f.write(hdr)
        for i, body in enumerate(bodies):
            f.write(b"\0" * (headers[i + 1][4] - f.tell()))
            f.write(body)
        f.write(b"\0" * (shoff - f.tell()))
        for h in headers:
            f.write(h if isinstance(h, bytes) else struct.pack("<10I", *h))



# The object the delegated data sections are linked from: abi/relink.sh
# assembles out/data/all.s and compiles the typed tables beside it, then folds
# the result into one relocatable so the placement fragment has a single name to
# put in front of every data section.
DATA_OBJECT = "*appl-data.o"


def declared_tables(tables):
    """Every declared table, keyed by the address the map gives it."""
    return dict((t.address, t) for t in tables)


def emit_data(directory, sources, delegated, blob, section_names, forced,
              reached_from, reserved, tables, declared):
    """Write each delegated data section as source, and say which names it publishes.

    A name is published where something outside the section reaches it, and only
    there: the partition gives the blob's own copy of a library object the
    library's name, and a global definition of one of those would answer the
    link that the source build is supposed to answer. The names the boundary and
    the replacements ask for by hand are published whether anything in the
    object reaches them or not, because their caller is outside this link.
    """
    emitted, untyped = [], []
    for s in sorted(delegated, key=lambda s: s.start):
        s.object = DATA_OBJECT
        publish = list(forced.get(id(s), ()))
        by_name = dict((name, (off, is_func)) for off, name, is_func in publish)
        names = []
        for off, name, is_func in section_names(s):
            exported = name in by_name or any(other != id(s) for other
                                              in reached_from.get(name, ()))
            if exported and reserved.get(name, 0) < 0:
                sys.exit("%s would publish %s, which the boundary reserves for"
                         " the library" % (s.sym, name))
            names.append((off, name, exported, is_func))
            by_name.pop(name, None)
        for name, (off, is_func) in sorted(by_name.items()):
            names.append((off, name, True, is_func))
        body = bytes(blob[s.start - APP_BASE:s.end - APP_BASE])
        words = dict((off, sym) for off, sym, t in s.relocs if t == R_ARM_ABS32)
        if s.start in tables:
            text, why = datagen.render_table(s, body, tables[s.start], declared, words,
                                             names)
            if text is not None:
                emitted.append((s, text, "typed"))
                continue
            untyped.append((tables[s.start].name, why))
        # The partition calls a component data when no function entry point
        # opens it, and a few such components still hold instructions the
        # boundary scan found a call in. Those relocations sit on the
        # instruction's own bytes rather than on a blank word, so they go out as
        # `.reloc` against the bytes the assembler has already emitted.
        branches = dict((off, (sym, datagen.RELOC_NAMES[t]))
                        for off, sym, t in s.relocs if t != R_ARM_ABS32)
        emitted.append((s, datagen.render_bytes(s, body, names, words, branches),
                        "bytes"))
    return datagen.write(directory, emitted, sources), untyped


class RamLayout(object):
    """The RAM items as sections, and the symbol a RAM address relocates to.

    A `.data` item and the flash section holding its initialiser are one
    section with two addresses: the bytes, the words inside them and the
    relocations those words take are all at the flash address, and the RAM
    address is what a pointer to the item holds. So the `.data` side is the
    partition's own sections retyped rather than sections of their own, which
    is also what keeps the identity link byte-identical -- the same bytes are
    still linked from the same place, they are only named twice.

    A `.bss` item has no bytes anywhere, so it is a section this makes up.
    """

    def __init__(self, ram, sections, blob_object, reserved):
        self.ram = ram
        self.data, self.bss = [], []
        # The map names a RAM object after the library global it is -- the
        # SoftDevice header's nrf_nvic_state among them -- and a definition of
        # that name here would answer the link the source build is meant to
        # answer, exactly as a blob copy of a library body would. The same
        # uniquing settles it: the blob's copy is called after its address.
        taken = set(s.sym for s in sections)
        run = next(r for r in ram.runs if r.kind == "data")
        inside = sorted((s for s in sections if run.load <= s.start
                         < run.load + (run.end - run.start)),
                        key=lambda s: s.start)
        by_load = dict((i.load, i) for i in ram.items if i.kind == "data")
        for s in inside:
            item = by_load.get(s.start)
            if item is None or item.size != s.end - s.start:
                raise SystemExit(
                    "the initialiser image's section at 0x%x (%d bytes) is not"
                    " one RAM item: the cut and the RAM partition disagree"
                    % (s.start, s.end - s.start))
            s.vma = item.start
            s.sym = objectify.unique(taken, reserved, item.name, item.start)
            s.name = ".data." + s.sym
            s.aliases.append(address_alias(item.start))
            self.data.append(s)
        if len(self.data) != len(by_load):
            raise SystemExit("%d of the %d .data items have no section"
                             % (len(by_load) - len(self.data), len(by_load)))
        for item in ram.items:
            if item.kind != "data":
                sym = objectify.unique(taken, reserved, item.name, item.start)
                s = objectify.Section(item.start, item.end, "data", sym)
                s.name = ".bss." + sym
                s.vma, s.nobits = item.start, True
                s.aliases.append(address_alias(item.start))
                s.object = blob_object
                self.bss.append(s)
        self.by_start = dict((s.vma, s) for s in self.data + self.bss)

    @property
    def sections(self):
        return self.data + self.bss

    @property
    def runs(self):
        return self.ram.runs

    def bound(self, word_addr, value):
        """The linker's name for a run bound, where this word holds one.

        The startup's own three reads are bounds by what they are used for --
        the copy's source, destination and end -- and a run's end is a bound
        wherever it is read, because it is one past the last byte and no item
        holds it. Everything else about a run is a reference to an item in it.
        """
        return self.ram.bounds.get(word_addr) or self.ram.ends.get(value)

    def label(self, addr):
        """The symbol naming a RAM address, or None where no item holds it.

        An address inside an item is an interior label on it, the same way a
        word naming row 17 of a flash table is: the item is one object and the
        offset is a point in it, not a section of its own.
        """
        item = self.ram.at(addr)
        if item is None:
            return None
        s = self.by_start[item.start]
        if addr == item.start:
            return s.sym
        # A label's value is its offset from the section's `start`, which for a
        # `.data` item is the initialiser in flash and not the RAM address the
        # word holds: the key is the byte, the name is what the byte is called
        # at run time.
        return s.labels.setdefault((s.start + (addr - item.start), False),
                                   "D_%08x" % addr)


def ram_relocations(blob, layout, ramlayout, words, skip):
    """Every flash word naming an app RAM item, as an R_ARM_ABS32 against it.

    Until RAM had sections these were constants -- a static's address does not
    change when flash moves -- and the only reason they could be left alone.
    With the items placed by the linker the word is an address like any other
    and has to name the object rather than hold a number.

    A RAM address no item holds keeps its value: the SoftDevice's RAM, the
    retained block below the app's own base, the stack, and the peripheral
    and event buffers facts.yaml pins. Nothing in the image bounds those, so
    there is nothing to relocate them against.
    """
    counts = {"pointer": 0, "fixed": 0, "bound": 0}
    unrelocated = []
    for row in words:
        if row["addr"] in skip:
            continue
        bound = ramlayout.bound(row["addr"], row["value"])
        if bound is None and row["signal"] != "ram":
            # Only the startup's own reads are bounds outside RAM: the copy's
            # source word holds a flash address, and it is the run's load
            # address rather than a pointer to the bytes. A word that lands in
            # a RAM item and is not relocated is still a reference the linker
            # cannot see, so the item it names goes on the keep list rather
            # than being dropped as unreached.
            if ramlayout.ram.at(row["value"]) is not None:
                unrelocated.append(row)
            continue
        sym = bound or ramlayout.label(row["value"])
        if sym is None:
            counts["fixed"] += 1
            continue
        counts["bound"] += 1 if bound else 0
        section = layout.at(row["addr"])
        if section is None or row["addr"] + 4 > section.end:
            raise SystemExit("word 0x%x is not inside one section" % row["addr"])
        off = row["addr"] - APP_BASE
        blob[off:off + 4] = b"\0\0\0\0"
        section.relocs.append((row["addr"] - section.start, sym, R_ARM_ABS32))
        skip.add(row["addr"])
        counts["pointer"] += 1
    return counts, unrelocated


def boundary_relocations(blob, boundary, layout):
    """The library boundary: the sites boundary.yaml owns, blanked and relocated.

    Returns the set of site addresses, so the partition's own pass leaves them
    alone: a call the source library answers must not also be a call into the
    blob's copy of it.
    """
    by_addr = {int(e["addr"]): e for e in boundary["app_to_lib"]}
    relocate = {a: e["symbol"] for a, e in by_addr.items()
                if not e.get("keep") and e["symbol"]}
    sites = find_sites(blob, set(by_addr),
                       [(int(lo), int(hi)) for lo, hi in boundary["library_ranges"]])
    declared = boundary["meta"]["app_to_lib"]
    for kind, key in (("bl", "sites"), ("b.w", "tail_sites")):
        n = sum(1 for _, _, k in sites if k == kind)
        if n != declared[key]:
            sys.exit("found %d %s boundary sites, boundary.yaml declares %d"
                     % (n, kind, declared[key]))

    owned, counts = set(), {R_ARM_THM_CALL: 0, R_ARM_THM_JUMP24: 0, R_ARM_ABS32: 0}
    for addr, tgt, kind in sites:
        if tgt not in relocate:
            continue            # explicitly kept: an ordinary call into the blob
        enc, rtype = ((SELF_BL, R_ARM_THM_CALL) if kind == "bl"
                      else (SELF_B, R_ARM_THM_JUMP24))
        struct.pack_into("<HH", blob, addr - APP_BASE, *enc)
        section = layout.at(addr)
        section.relocs.append((addr - section.start, relocate[tgt], rtype))
        owned.add(addr)
        counts[rtype] += 1

    for v in boundary["data_references"]["vector_table"]:
        if not v.get("relocate"):
            continue
        addr = APP_BASE + v["index"] * 4
        word = struct.unpack_from("<I", blob, addr - APP_BASE)[0]
        if word != v["word"]:
            sys.exit("vector %d holds 0x%x, boundary.yaml expects 0x%x"
                     % (v["index"], word, v["word"]))
        struct.pack_into("<I", blob, addr - APP_BASE, 0)
        section = layout.at(addr)
        section.relocs.append((addr - section.start, v["symbol"], R_ARM_ABS32))
        owned.add(addr)
        counts[R_ARM_ABS32] += 1

    for e in boundary.get("startup", []):
        addr = int(e["site"])
        found = decode_branch(blob, addr - APP_BASE)
        if found != ("bl", int(e["original"])):
            sys.exit("startup site 0x%x is %r, boundary.yaml expects a bl to 0x%x"
                     % (addr, found, int(e["original"])))
        struct.pack_into("<HH", blob, addr - APP_BASE, *SELF_BL)
        section = layout.at(addr)
        section.relocs.append((addr - section.start, e["symbol"], R_ARM_THM_CALL))
        owned.add(addr)
        counts[R_ARM_THM_CALL] += 1
    return owned, counts


def stock_definitions(boundary, replacements, path):
    """Stand-ins for the source library, at the blob's own copy of each function.

    A link with these and nothing else must reproduce the image byte for byte:
    every boundary symbol resolves back to the address the instruction held
    before blobify blanked it, so the relocation writes the original bytes.

    They are an object rather than a linker script because a script-defined
    symbol has no type, and the ARM linker reads Thumb-ness off bit 0 of a
    function symbol only: given a plain absolute symbol it takes every boundary
    call for an interworking one and plants a veneer, which moves the image.
    """
    defs = {}
    for e in boundary["app_to_lib"]:
        if not e.get("keep") and e["symbol"]:
            defs[e["symbol"]] = int(e["addr"]) | 1
    for v in boundary["data_references"]["vector_table"]:
        if v.get("relocate"):
            defs[v["symbol"]] = v["word"]
    for e in boundary.get("startup", []):
        defs[e["symbol"]] = int(e["original"]) | 1
    # A replacement whose definition is the original body: the same stand-in the
    # library gets, so the identity link still reproduces the image.
    for e in replacements.entries:
        defs[e["symbol"]] = e["at"] | 1
    # A RAM global's stand-in is the blob's own copy of the object, and it is
    # data: the Thumb bit is not part of an object's address, and setting it
    # would make the identity link write an address one byte past the object.
    data_defs = dict((e["as"], e["at"]) for e in replacements.globals)

    strtab = bytearray(b"\0")
    shstr = bytearray(b"\0")
    symtab = bytearray(b"\0" * 16)
    for sym, value in sorted(list(defs.items()) + list(data_defs.items())):
        off = len(strtab)
        strtab += sym.encode() + b"\0"
        styp = STT_OBJECT if sym in data_defs else STT_FUNC
        symtab += struct.pack("<IIIBBH", off, value, 0,
                              (STB_GLOBAL << 4) | styp, 0, SHN_ABS)

    names = {}
    for n in (".symtab", ".strtab", ".shstrtab"):
        names[n] = len(shstr)
        shstr += n.encode() + b"\0"
    bodies = [bytes(symtab), bytes(strtab), bytes(shstr)]
    off, offs = 52, []
    for b in bodies:
        off = (off + 3) & ~3
        offs.append(off)
        off += len(b)
    shoff = (off + 3) & ~3
    SHT_SYMTAB, SHT_STRTAB = 2, 3
    sh = [b"\0" * 40,
          struct.pack("<10I", names[".symtab"], SHT_SYMTAB, 0, 0, offs[0],
                      len(symtab), 2, 1, 4, 16),
          struct.pack("<10I", names[".strtab"], SHT_STRTAB, 0, 0, offs[1],
                      len(strtab), 0, 0, 1, 0),
          struct.pack("<10I", names[".shstrtab"], SHT_STRTAB, 0, 0, offs[2],
                      len(shstr), 0, 0, 1, 0)]
    hdr = struct.pack("<16sHHIIIIIHHHHHH",
                      b"\x7fELF\x01\x01\x01\x00" + b"\0" * 8, 1, 40, 1, 0, 0,
                      shoff, 0x05000400, 52, 0, 0, 40, len(sh), 3)
    with open(path, "wb") as f:
        f.write(hdr)
        for o, b in zip(offs, bodies):
            f.write(b"\0" * (o - f.tell()))
            f.write(b)
        f.write(b"\0" * (shoff - f.tell()))
        f.write(b"".join(sh))
    return len(defs) + len(data_defs)


def pinned_addresses(facts, layout):
    """The section starts abi/facts.yaml's fixed points name, checked against the cover."""
    pinned = set()
    for f in facts["fixed_points"]:
        addr = int(f["addr"], 0) if isinstance(f["addr"], str) else f["addr"]
        section = layout.at(addr)
        if section is None:
            sys.exit("fixed point %s at 0x%x is outside the app" % (f["name"], addr))
        pinned.add(section.start)
    return pinned

def derive(spec, relative_to):
    """The replacement entries a `derive:` block stands for, from a measurement.

    The libc group is 95 entries long and every one of them is the same
    decision: the source build reproduces this body, so the link may take it
    from the archive instead of the blob. Writing them out by hand would make
    the list a decision, which it is not; this reads abi/libc_check.py's
    verdicts and turns the ones that reproduce into entries.
    """
    bodies = os.path.join(relative_to, spec["bodies"])
    if not os.path.exists(bodies):
        sys.exit("%s does not exist; run abi/libc_check.py --emit" % bodies)
    verdicts = yaml.safe_load(open(bodies))
    if verdicts["build"] != spec["build"]:
        sys.exit("%s was measured against %s, the group asks for %s"
                 % (bodies, verdicts["build"], spec["build"]))
    if verdicts.get("class") != spec["class"]:
        sys.exit("%s measures the %s bodies, the group asks for %s"
                 % (bodies, verdicts.get("class"), spec["class"]))
    wanted = set(spec["reproduces"])
    excepted = {e["symbol"]: e["why"] for e in spec.get("except", [])}
    found = set(b["symbol"] for b in verdicts["bodies"])
    for symbol in excepted:
        if symbol not in found:
            sys.exit("%s excepts %s, which %s does not measure"
                     % (relative_to, symbol, bodies))
    return [{"at": b["address"], "symbol": b["symbol"], "derived": True,
             "why": "%s: %s" % (b["verdict"], b["why"])}
            for b in verdicts["bodies"]
            if b["verdict"] in wanted and b["symbol"] not in excepted]


# The toolchain the archives were built with, and so the one whose `nm` and
# `size` read them; the same prefix abi/relink.sh links them with.
REF_TOOLS = ("{root}/tc/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi"
             "/bin/arm-none-eabi-")


def archive_paths(spec, build):
    """The `cost:` block's archives and tool prefix, with the variables filled in."""
    root = os.path.expanduser(spec.get("root", "~/ref-build"))
    fill = lambda p: os.path.expanduser(p.format(root=root, build=build))
    return [fill(p) for p in spec["archives"]], fill(spec.get("tools", REF_TOOLS))


def marginal_refusals(group, entries, layout):
    """The derived entries whose archive closure costs more than they free.

    Taking a body from the archive is only a win while the members the linker
    pulls in behind it weigh less than the blob bytes the body returns. The
    ratio the group states is that trade, and the measurement is
    abi/archive_cost.py's: the archive text a root adds that no other root in
    the group already pays for, against the size of the section it replaces.

    A refusal is not a judgement about the body. `_tzset_r` was 15 KB of
    closure for 22 bytes because the name was wrong -- the image's 0x9bd0c is a
    Withings SPI-flash wrapper whose masked bytes happen to match newlib's --
    and a ratio this far out is worth reading as a question about the name
    before it is read as a cost.
    """
    spec = group.get("derive", {}).get("cost")
    if not spec:
        return []
    paths, tools = archive_paths(spec, group["derive"]["build"])
    archives = archive_cost.Archives(paths, tools)
    freed = {}
    for e in entries:
        section = layout.at(e["at"])
        freed[e["symbol"]] = section.end - section.start if section else 0
    cost = archives.marginal(freed)
    limit = float(spec["max_ratio"])
    refused = []
    for symbol, (size, members) in sorted(cost.items(), key=lambda kv: -kv[1][0]):
        if not freed[symbol] or size <= limit * freed[symbol]:
            continue
        refused.append((symbol,
                        "%d B of archive closure for %d B of blob freed"
                        " (%.0fx, the group allows %.0fx): %s"
                        % (size, freed[symbol], size / float(freed[symbol]),
                           limit, ", ".join(members[:4]))))
    return refused


def unbound_callees(replacements, boundary, refs, layout):
    """Replaced bodies that call a body the link still takes from the blob.

    A replacement is a claim that the source defines the same function, and the
    function it calls is part of that claim: if the archive's body calls the
    archive's `strlen` and the image's calls a Withings one at the same address,
    the two are not the same function and the byte comparison that said they
    were was reading a coincidence. Every callee of a replaced body should
    itself be replaced, exported to the source, or relocated by the boundary;
    anything else is worth looking at before the link is trusted.
    """
    bound = set(e["at"] for e in replacements.entries)
    bound |= set(e["at"] for e in replacements.exports)
    bound |= set(int(e["addr"]) for e in boundary["app_to_lib"]
                 if not e.get("keep") and e["symbol"])
    calls = collections.defaultdict(set)
    for row in refs["calls"]:
        section = layout.at(row["from"])
        if section is not None:
            calls[section.start].add(row["to"])
    loose = []
    for e in sorted(replacements.entries, key=lambda e: e["at"]):
        section = layout.at(e["at"])
        if section is None:
            continue
        out = sorted(t for t in calls[section.start]
                     if not section.start <= t < section.end and t not in bound)
        if out:
            loose.append((e, out))
    return loose


class ReferenceIndex(object):
    """Every reference the partition holds, keyed by the address it names.

    The refusal test asks the same four questions of each candidate body, and
    there are 95 candidates and tens of thousands of references; asked
    address-first each question is a dict lookup per byte of the body.
    """

    def __init__(self, refs, reads, words):
        self.falls_into = collections.defaultdict(list)
        self.falls_out_of = collections.defaultdict(list)
        self.read_by = collections.defaultdict(list)
        self.called_by = collections.defaultdict(list)
        self.named_by = collections.defaultdict(list)
        for row in refs["fallthrough"]:
            self.falls_into[row["to"]].append(row["from"])
            self.falls_out_of[row["from"]].append(row["to"])
        for site, target in reads:
            self.read_by[target].append(site)
        for row in refs["calls"]:
            if not row["external"]:
                self.called_by[row["to"]].append(row["from"])
        for word in words:
            # An unclassified word has no target: the value is the address it
            # would name if it turned out to be one, and that is exactly what
            # the refusal below asks about, so index it under the value.
            at = (word["value"] & ~1 if word["class"] == "review"
                  else word.get("target"))
            if at is not None:
                self.named_by[at].append(word)


class Replacements(object):
    """abi/replacements.yaml: partition sections bound to a definition from source.

    The object keeps the original bytes -- renamed `orig_<symbol>` and referenced
    by nothing -- so the identity link still reproduces the image from them and
    --gc-sections drops them from a link that does not. What changes is where
    every reference points: a call, a tail call or a pointer word that named the
    section now names an undefined `<symbol>`, which the source build defines and
    abi/blobify.py's stand-in object binds back to the original address.
    """

    def __init__(self, path, wanted):
        spec = yaml.safe_load(open(path))
        groups = {g["group"]: g for g in spec["groups"]}
        unknown = [name for name in wanted if name not in groups]
        if unknown:
            sys.exit("%s names no group of %s" % (", ".join(unknown), path))
        self.entries, self.exports, self.sources = [], [], []
        self.globals, self.refused = [], []
        self.groups = {name: groups[name] for name in wanted}
        for name in wanted:
            group = groups[name]
            self.sources += group.get("sources", [])
            for e in group.get("exports", []):
                self.exports.append(e)
            for e in group.get("globals", []):
                e = dict(e, group=name)
                e["at"] = int(str(e["at"]), 0)
                self.globals.append(e)
            derived = (derive(group["derive"], os.path.dirname(path))
                       if "derive" in group else [])
            for e in group.get("replacements", []) + derived:
                e = dict(e, group=name)
                e["at"] = int(str(e["at"]), 0)
                self.entries.append(e)
        at_once = {}
        for e in self.entries:
            if at_once.setdefault(e["at"], e["symbol"]) != e["symbol"]:
                sys.exit("0x%x is replaced twice, by %s and by %s"
                         % (e["at"], at_once[e["at"]], e["symbol"]))
        seen = {}
        for e in self.exports:
            e["at"] = int(str(e["at"]), 0)
            if seen.setdefault(e["as"], e["at"]) != e["at"]:
                sys.exit("%s is exported from two addresses" % e["as"])
        self.exports = list({e["as"]: e for e in self.exports}.values())
        seen = {}
        for e in self.globals:
            if seen.setdefault(e["as"], e["at"]) != e["at"]:
                sys.exit("%s is a global at two addresses" % e["as"])
        self.globals = list({e["as"]: e for e in self.globals}.values())
        self.by_start = {}

    def symbols(self):
        return [e["symbol"] for e in self.entries]

    def bind(self, layout, refs, reads, words):
        """Check each replacement against the partition, then rename and retarget.

        Everything refused here is a reference the linker has no way to express
        against a symbol that is not the section's own start: a second function
        in the same bytes, a fall-through, a pool word read from outside, an
        entry into the middle. Each is a fact about the image, so a hand-written
        entry is refused rather than worked around. A derived entry is dropped
        with the same reason instead, because the derivation is a measurement of
        what the source build reproduces and says nothing about how the image
        happens to have cut that body.
        """
        index = ReferenceIndex(refs, reads, words)
        for name, group in self.groups.items():
            derived = [e for e in self.entries
                       if e["group"] == name and e.get("derived")]
            over = dict(marginal_refusals(group, derived, layout))
            if not over:
                continue
            self.refused += [(e, over[e["symbol"]]) for e in derived
                             if e["symbol"] in over]
            self.entries = [e for e in self.entries
                            if not (e["group"] == name and e["symbol"] in over)]
        retarget = {}
        for e in self.entries:
            why = self.refusal(e, layout, index)
            if why is not None:
                if not e.get("derived"):
                    sys.exit("replacement %s: %s" % (e["symbol"], why))
                self.refused.append((e, why))
                continue
            section = layout.at(e["at"])
            section.sym = "orig_" + e["symbol"]
            section.name = ".text." + section.sym
            retarget[e["at"]] = e["symbol"]
            self.by_start[section.start] = e
        return retarget

    @staticmethod
    def refusal(e, layout, index):
        """Why this section cannot be swapped for a definition, or None."""
        at, symbol = e["at"], e["symbol"]
        section = layout.at(at)
        if section is None:
            return "0x%x is outside the app" % at
        if section.start != at:
            return ("0x%x is inside the section %s at 0x%x, not its start"
                    % (at, section.sym, section.start))
        if section.kind != "code":
            return "0x%x is a %s section" % (at, section.kind)
        if len(section.functions) > 1:
            return ("the section at 0x%x holds %d functions (%s); a replacement"
                    " is one definition"
                    % (at, len(section.functions),
                       ", ".join("0x%x" % f for f in section.functions)))
        if section.labels:
            return ("the section at 0x%x carries the interior symbols %s, so"
                    " something names bytes inside the body"
                    % (at, ", ".join(sorted(section.labels.values()))))
        inside = lambda a: section.start <= a < section.end
        for addr in range(section.start, section.end):
            for site in index.falls_into.get(addr, ()):
                if not inside(site):
                    return ("0x%x falls through into 0x%x, and a replacement"
                            " cannot be fallen into" % (site, addr))
            for target in index.falls_out_of.get(addr, ()):
                if not inside(target):
                    return ("0x%x falls through out of the body into 0x%x, so"
                            " the body is not a whole function" % (addr, target))
            for site in index.read_by.get(addr, ()):
                if not inside(site):
                    return ("0x%x reads the word 0x%x inside the body, which goes"
                            " away with it" % (site, addr))
            for site in index.called_by.get(addr, ()):
                if inside(site) or addr == at:
                    continue
                return ("0x%x enters the body at 0x%x, which is not its entry"
                        " point" % (site, addr))
            for word in index.named_by.get(addr, ()):
                if word["class"] == "review":
                    return ("the word 0x%x is still unclassified and may name"
                            " 0x%x" % (word["addr"], addr))
                if word["class"] != "pointer":
                    continue
                if addr != at or not word["thumb_target"]:
                    return ("the word 0x%x names 0x%x%s, not the entry point"
                            % (word["addr"], addr,
                               "" if word["thumb_target"] else " as data"))
        if section.sym == symbol:
            return ("the partition still holds the name, so the reservation did"
                    " not take")
        if "name" in e and e["name"] != section.sym:
            return ("0x%x is called %s in the entry and %s in the partition"
                    % (at, e["name"], section.sym))
        return None

    def header(self, path):
        """out/replace.h: what the sources may call, and nothing else.

        Each declaration is emitted twice for a replacement, once under the
        symbol the call sites now bind to and once under `orig_<symbol>`, which
        is the original body the object still carries.
        """
        lines = ["/* Generated by abi/blobify.py from abi/replacements.yaml --"
                 " do not edit. */", "#ifndef REPLACE_H", "#define REPLACE_H", ""]
        for e in self.exports + self.globals:
            if "proto" not in e:
                continue
            lines.append("/* 0x%x: %s */" % (e["at"], " ".join(str(e["why"]).split())))
            proto = e["proto"]
            lines.append(proto if proto.startswith("extern") else "extern " + proto)
        for e in self.entries:
            if "proto" not in e:
                continue        # the archive defines it; no source of ours calls it
            proto, symbol = e["proto"], e["symbol"]
            marker = " %s(" % symbol
            if proto.count(marker) != 1:
                sys.exit("replacement %s: its proto %r does not name it exactly once"
                         % (symbol, proto))
            lines.append("/* 0x%x: %s */" % (e["at"], " ".join(str(e["why"]).split())))
            lines.append("extern " + proto)
            lines.append("extern " + proto.replace(marker, " orig_%s(" % symbol))
        lines += ["", "#endif", ""]
        with open(path, "w") as fh:
            fh.write("\n".join(lines))
        # What abi/relink.sh has to compile and link: written out rather than
        # re-parsed there, so the group selection is decided in one place.
        with open(os.path.splitext(path)[0] + "-sources.txt", "w") as fh:
            fh.write("".join(src + "\n" for src in self.sources))



def prune_replacements(path, wanted):
    """The replacement groups abi/prunes.yaml's features ask for.

    A removal that ends in a stub is one edit fewer: the tunnel prune used to
    turn the send gate's `bpl` into a `b` because the object exported no symbol
    for the sender the gate tail-calls, and a replacement section is what that
    edit was standing in for.
    """
    spec = yaml.safe_load(open(path))
    features = {f["feature"]: f for f in spec["features"]}
    groups = []
    for name in wanted:
        for group in features[name].get("replace", []):
            if group not in groups:
                groups.append(group)
    return groups


def apply_prunes(blob, items, path, wanted):
    """Edit the image the way abi/prunes.yaml says, or refuse.

    Every edit declares the address it changes and the bytes it expects to find
    there, and the address is the key: it is the coordinate the stock image is
    expressed in, and the one nothing renames. An edit may also name the
    partition symbol whose body or item it lands in, which is then checked and
    is informational; the bytes are the check that catches an edit written
    against another image. An edit is applied before the cut, so what the
    linker sees is a program the feature is already unreachable in and
    --gc-sections is the thing that removes it.
    """
    with open(path) as fh:
        spec = yaml.safe_load(fh)
    features = {f["feature"]: f for f in spec["features"]}
    unknown = [name for name in wanted if name not in features]
    if unknown:
        sys.exit("%s names no feature of %s" % (", ".join(unknown), path))
    owner = {}
    for f in items["functions"]:
        owner[f["start"]] = f["name"]
    ranges = sorted((r["start"], r["end"], owner.get(r["function"]))
                    for r in items["function_ranges"])
    ranges += sorted((d["start"], d["end"], d["name"]) for d in items["data"])
    ranges += sorted((g["start"], g["end"], "gap_%08x" % g["start"])
                     for g in items["gaps"])
    ranges.sort()
    applied, touched = 0, set()
    for name in wanted:
        for edit in features[name]["edits"]:
            at = int(str(edit["at"]), 0)
            old = bytes.fromhex(str(edit["original"]))
            new = bytes.fromhex(str(edit["new"]))
            if len(old) != len(new):
                sys.exit("prune %s at 0x%x replaces %d bytes with %d"
                         % (name, at, len(old), len(new)))
            found = bytes(blob[at - APP_BASE:at - APP_BASE + len(old)])
            if found != old:
                sys.exit("prune %s at 0x%x finds %s, not the %s it expects"
                         % (name, at, found.hex(), old.hex()))
            if "in" in edit:
                holder = [r for r in ranges if r[0] <= at < r[1]]
                if not holder or holder[0][2] != edit["in"]:
                    sys.exit("prune %s at 0x%x is in %s, not the %s it names"
                             % (name, at, holder[0][2] if holder else "nothing",
                                edit["in"]))
            blob[at - APP_BASE:at - APP_BASE + len(new)] = new
            touched.update(range(at, at + len(new)))
            applied += 1
    print("  pruned %s: %d edits, %d bytes" % (", ".join(wanted), applied,
                                               len(touched)))
    # The passes that follow read the export, which describes the image as it
    # was: an edited call site no longer decodes as the call the export claims,
    # and an edited table word no longer holds the pointer the classification
    # found. Both are answered the same way the library boundary answers them,
    # by handing the addresses to the caller as already owned.
    return touched


def ar_members(data):
    """Each member of a `!<arch>` archive, as bytes; the tables are skipped.

    An archive on the link line is read whole rather than as the members the
    link would really pull in: a member that is not pulled in contributes roots
    for nothing, which costs a few sections kept, and guessing which ones the
    link wants would be reimplementing the link.
    """
    at, out = 8, []
    while at + 60 <= len(data):
        name = data[at:at + 16].decode("latin1").strip()
        size = int(data[at + 48:at + 58].decode("latin1").strip() or 0)
        body = at + 60
        at = body + size + (size & 1)
        if name in ("/", "//", "/SYM64/") or data[body:body + 4] != b"\x7fELF":
            continue
        out.append(data[body:body + size])
    return out


def undefined_symbols(path):
    """The names an ELF32-LE relocatable or archive references and does not define."""
    blob = open(path, "rb").read()
    if blob[:8] == b"!<arch>\n":
        out = set()
        for member in ar_members(blob):
            out |= elf_undefined(member, path)
        return out
    return elf_undefined(blob, path)


def elf_undefined(data, path):
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        sys.exit("%s is not an ELF32 little-endian object" % path)
    shoff, = struct.unpack_from("<I", data, 0x20)
    shentsize, shnum, _ = struct.unpack_from("<HHH", data, 0x2E)
    headers = [struct.unpack_from("<10I", data, shoff + i * shentsize)
               for i in range(shnum)]
    found = set()
    for _, styp, _, _, off, size, link, _, _, entsize in headers:
        if styp != 2 or not entsize:            # SHT_SYMTAB
            continue
        stroff = headers[link][4]
        for at in range(entsize, size, entsize):
            name, _, _, _, _, shndx = struct.unpack_from("<IIIBBH", data, off + at)
            if not name or shndx:               # defined here, or unnamed
                continue
            end = data.index(b"\0", stroff + name)
            found.add(data[stroff + name:end].decode())
    return found


def linked_roots(sections, section_names, objects, exported=()):
    """The blob sections the rest of the link line reaches by name.

    --gc-sections walks from every object it links, not from the app alone, so
    a section whose only keeper is the source kernel, the glue, a replacement
    body or the C library is kept by the real link and looks dead to a walk
    that enters the app's vector table and nothing else. That gap is what
    relink.ld's `.blobdead` was catching: the sections a layout left out of the
    placement because the walk called them dead, which the link then kept and
    put in the library region. Reading each object's undefined symbols closes
    it -- an undefined name a blob section defines is an edge into the blob
    from outside it, and that is exactly a root. The archives count: the nine
    syscalls the C library calls are exported from the image rather than
    stubbed, and `_write`, `_lseek`, `_exit`, `_getpid` and `_isatty` are the
    five that no app path reaches, so libc.a is the only thing keeping them.
    """
    owner = dict(exported)
    for s in sections:
        for _, name, _ in section_names(s):
            owner.setdefault(name, s)
    roots, by_object = [], {}
    for path in objects:
        if not os.path.exists(path):
            sys.exit("%s is on the link line but does not exist yet; the gc walk"
                     " needs it before the placement is written" % path)
        named = sorted(n for n in undefined_symbols(path) if n in owner)
        by_object[path] = named
        roots.extend(owner[n] for n in named)
    return roots, by_object


def gc_reachable(sections, section_names, start, also=()):
    """The section starts --gc-sections keeps when it enters the object at `start`.

    One node per section and one edge per relocation, which is the same graph
    abi/reach.py builds out of the written object, but taken from the sections
    in hand so that the replacements and prunes this run applied are in it.
    A relocation against a name no section defines -- what a replaced body's
    callers now hold -- is an edge out of the object and not an edge at all.

    `also` is the rest of the link line, as `linked_roots` reads it: every other
    object and archive is a way into the blob and its entries are roots beside
    the vector table.
    """
    owner = {}
    for s in sections:
        for _, name, _ in section_names(s):
            owner.setdefault(name, s)
    stack = [start] + [s for s in also]
    seen = set(s.start for s in stack)
    while stack:
        for _, sym, _ in stack.pop().relocs:
            target = owner.get(sym)
            if target is not None and target.start not in seen:
                seen.add(target.start)
                stack.append(target)
    return seen


def gc_keep_list(sections, layout, pinned, words, reach_path, section_names,
                 replaced=(), also=()):
    """(section, why) for everything --gc-sections must not be allowed to drop.

    Three kinds, and only three. The fixed points are a contract with the MBR,
    the bootloader and the phone, so they are pinned and kept. The roots
    abi/roots.yaml derives are the places the image is entered that no
    relocation shows -- a vector word is in the vector table, which is itself
    kept, but a dispatch table whose address a walker computes is referenced by
    nothing at all. And a word the classification has not decided may be a
    pointer, so the section it would name stays until it is decided; that is the
    review list made into bytes, and it is the one kind that is not permanent.

    A root the object already reaches is not listed: gc keeps it either way, and
    listing it would hide how much of the root set is really unreferenced.
    "Already reaches" is read off the object being built and not off the reach
    map, because a replacement or a prune is a different program: the reach map
    is the stock graph, in which a section whose only keeper is a body the link
    replaces still looks reached.
    """
    if not os.path.exists(reach_path):
        sys.exit("%s does not exist; run abi/reach.py first" % reach_path)
    with open(reach_path) as fh:
        reach = json.load(fh)
    keep, seen = [], set()
    already = gc_reachable(sections, section_names, layout.at(APP_BASE), also)

    replaced = set(replaced)

    def add(addr, why, redundant_if_reached=True):
        section = layout.at(addr)
        if section is None or section.start in seen:
            return
        if section.start in replaced:
            sys.exit("the replaced body at 0x%x would be KEEPed (%s), so nothing"
                     " would be dropped" % (section.start, why))
        if redundant_if_reached and section.start in already:
            return
        seen.add(section.start)
        keep.append((section, why))

    for start in sorted(pinned):
        add(start, "fixed point", False)
    for root in reach["roots"]:
        add(int(root["addr"], 16), "root %s/%s" % (root["group"], root["name"]))
    for word in words:
        if word["class"] != "review":
            continue
        add(word["value"] & ~1, "named by the unclassified word 0x%x" % word["addr"])
    return keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", default=os.path.join(SIM, "out", "relink", "appl-blob.o"))
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--place", default=os.path.join(SIM, "out", "relink-place.ld"))
    ap.add_argument("--stock", default=os.path.join(SIM, "out", "relink", "stock-defs.o"))
    ap.add_argument("--layout", choices=("shift", "reverse", "pack"),
                    help="move every text section: `shift` keeps the order and"
                         " slides it, `reverse` turns it round. The stale-address"
                         " scan is what these two are for. `pack` closes the"
                         " image up over the bodies a replacement group made"
                         " unreferenced, which leaves one hole at the top of the"
                         " text for --spill to link a library archive into.")
    ap.add_argument("--ram-layout", choices=("shift", "reverse"),
                    help="move every RAM item the same way --layout moves the"
                         " text: `shift` slides the runs up, `reverse` also"
                         " turns each zeroed run's item order round. The"
                         " stale-address scan reads the image and the"
                         " initialiser image for a word that still holds an"
                         " old RAM address.")
    ap.add_argument("--spill", action="append", default=[], metavar="PATTERN",
                    help="a linker input-file pattern whose text and rodata go"
                         " into the flash --layout pack freed, instead of into"
                         " the library region after the image; the library"
                         " region ends at the bootloader and cannot grow, so"
                         " this is the only place a bigger library fits")
    ap.add_argument("--tools", default=os.path.join(
        os.path.expanduser("~/ref-build"), "tc",
        "arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi", "bin",
        "arm-none-eabi-"), help="the binutils prefix that reads --also-linked")
    ap.add_argument("--also-linked", action="append", default=[], metavar="OBJECT",
                    help="another object on the link line, whose undefined"
                         " symbols are roots for the --gc walk; without them the"
                         " walk enters the app alone and calls dead what the"
                         " source kernel, the glue or a replacement keeps")
    ap.add_argument("--reserve-defs", action="append", default=[], metavar="OBJECT",
                    help="an archive or object on the link line whose names the"
                         " partition must not publish; --also-linked implies it,"
                         " and it is given on its own where the walk is not"
                         " wanted but the link line is the same")
    ap.add_argument("--gc", action="store_true",
                    help="place only the fixed points and KEEP only what the"
                         " linker cannot see, so --gc-sections drops the rest;"
                         " reads the root set from abi/reach.py's out/reach")
    ap.add_argument("--reach", default=os.path.join(SIM, "out", "reach", "reach.json"),
                    help="the reachability map --gc takes its roots from")
    ap.add_argument("--prune", action="append", default=[], metavar="FEATURE",
                    help="apply abi/prunes.yaml's edits for a feature to the"
                         " image before cutting it, so the link sees a program"
                         " the feature is already gone from")
    ap.add_argument("--prunes", default=os.path.join(HERE, "prunes.yaml"))
    ap.add_argument("--replace", action="append", default=[], metavar="GROUP",
                    help="bind every reference into the sections abi/"
                         "replacements.yaml's group names to the symbol its"
                         " source defines, and rename the original orig_<symbol>")
    ap.add_argument("--replacements", default=os.path.join(HERE, "replacements.yaml"))
    ap.add_argument("--replace-header", default=os.path.join(SIM, "out", "replace.h"))
    ap.add_argument("--data-source", metavar="DIR",
                    help="write every data section to DIR as assembler or C"
                         " source and leave it out of the object, so the link"
                         " takes the app's data from source like its kernel")
    ap.add_argument("--data-sources",
                    default=os.path.join(SIM, "out", "data-sources.txt"),
                    help="where to list the C files --data-source wrote, for"
                         " the build script to compile")
    ap.add_argument("--keep-also", help="a file of section names to add to the"
                    " --gc KEEP list; bisecting a gc link that does not boot"
                    " over the sections it dropped is what this is for")
    ap.add_argument("--reclaim", action="store_true",
                    help="place nothing but the fixed points, so --gc-sections"
                         " can drop and pack; for the size measurement only")
    ap.add_argument("--move", action="append", default=[], metavar="SECTION=ADDR",
                    help="place one section elsewhere; the stale-address scan's"
                         " dry run is what this is for")
    args = ap.parse_args()
    moves = {}
    for spec in args.move:
        name, _, where = spec.partition("=")
        moves[name] = int(where, 0)

    replacements = Replacements(
        args.replacements,
        args.replace + [g for g in prune_replacements(args.prunes, args.prune)
                        if g not in args.replace])
    boundary = yaml.safe_load(open(os.path.join(HERE, "boundary.yaml")))
    facts = yaml.safe_load(open(os.path.join(HERE, "facts.yaml")))
    types = shapes.load()
    smap = symmap.load()
    tables = types.tables(smap)
    blob = bytearray(open(args.image, "rb").read())
    if len(blob) != APP_END - APP_BASE:
        sys.exit("%s is %d bytes, the app image is %d"
                 % (args.image, len(blob), APP_END - APP_BASE))

    items, refs = objectify.read_export(args.export)
    pruned = set()
    if args.prune:
        pruned = apply_prunes(blob, items, args.prunes, args.prune)
    with open(os.path.join(args.export, "words.json")) as fh:
        classified = json.load(fh)
    words = classified["words"]
    # Names the boundary owns, and the address each has to be at; the partition
    # must not hand one of them to a function of its own. The library's own
    # names are reserved at NOWHERE, because the partition gave the blob's copy
    # of the kernel the same names as the source build: a local
    # `xQueueGenericSend` in the blob would swallow the relocation meant for the
    # source one, and the app would go on calling its own copy.
    NOWHERE = -1
    reserved = {"appl_vector_table": APP_BASE, "appl_blob_start": APP_BASE}
    for e in boundary.get("startup", []):
        reserved[e["original_symbol"]] = int(e["original"])
        reserved[e["symbol"]] = NOWHERE
    for e in boundary["lib_to_app"]:
        if e["symbol"] and not e.get("library_not_app"):
            reserved[e["symbol"]] = int(e["addr"])
    for e in boundary["app_to_lib"]:
        if not e.get("keep") and e["symbol"]:
            reserved[e["symbol"]] = NOWHERE
    for v in boundary["data_references"]["vector_table"]:
        reserved[v["symbol"]] = NOWHERE if v.get("relocate") else v["word"] & ~1
    # A replacement's symbol has to be undefined in the object for the same
    # reason a boundary symbol has: the partition already calls the body by that
    # name, and a local definition would swallow the relocation.
    for e in replacements.entries:
        reserved[e["symbol"]] = NOWHERE
    for e in replacements.exports:
        reserved[e["as"]] = e["at"]
    # Every name the rest of the link line already defines. The partition gives
    # the blob's own copy of a library body the library's name, and two
    # definitions of one name is a link error the moment an archive member that
    # carries it is pulled in for something else: the blob's `__subdf3` section
    # holds __aeabi_dadd, __floatunsidf and __muldf3 as interior labels, and
    # libgcc's soft-float members define all three. Reserving them makes the
    # partition call its copy `__aeabi_dadd__8d7d0`, which is what it is. A
    # name the boundary or a replacement already reserved keeps that
    # reservation, because that one says where the body is rather than only
    # that the name is taken.
    link_line = sorted(set(args.also_linked) | set(args.reserve_defs))
    if link_line:
        for name in archive_cost.Archives(link_line, args.tools).defines:
            reserved.setdefault(name, NOWHERE)
    # A RAM global has no section of its own in the object -- the object is only
    # flash -- so there is nothing to rename, but the name still has to stay
    # undefined or a partition section that happens to carry it would swallow
    # the relocation.
    for e in replacements.globals:
        reserved[e["as"]] = NOWHERE
    # `adr rN,#imm` is the other pc-relative reference the image holds and the
    # only one with no relocation at all: 21 of the 179 in this image name a
    # data item outside the function's section, so unless the two ends are one
    # section the instruction computes a stale address as soon as the code
    # moves. They bind exactly like a literal pool's word does.
    reads = [(r["site"], r["target"])
             for r in refs["pool_reads"] + refs["pc_addresses"]]
    bits = bytes.fromhex(items["instruction_bytes"]["bits"])
    covered = bytes((bits[i >> 3] >> (i & 7)) & 1 for i in range(len(blob)))
    # What a data tile has to be its own section for: a word may hold its
    # address, an `adr` computes it, a pc-relative load reads it from too far
    # away for the reader to be known, the boundary or a fixed point needs the
    # symbol at that address. Anything else in a run of data is a field of the
    # object above it.
    named = set(w["target"] for w in words if w["class"] == "pointer")
    named |= set(w["value"] & ~1 for w in words if w["class"] == "pointer")
    named |= set(r["target"] for r in refs["pc_addresses"])
    named |= set(t for _, t in reads)
    named |= set(a for a in reserved.values() if a >= 0)
    named |= set(int(str(f["addr"]), 0) for f in facts["fixed_points"])
    # The initialiser image is one run to the copy loop and one item per RAM
    # object to the linker: cutting it where the RAM partition cuts RAM is what
    # lets each `.data` item be its own section, so the copy the linker lays out
    # is the copy the startup makes.
    ram = ramparts.load(args.export, args.image, smap, types)
    named |= set(i.load for i in ram.items if i.kind == "data")
    named |= set(f["start"] for f in items["functions"])
    # A declared table is an object whatever the run around it looks like, and
    # both its ends are points: the declaration is what says where the rows
    # stop, and a table that shares a section with the bytes after it cannot be
    # emitted as the array it is.
    for table in tables:
        lo, hi = table.address, table.end
        # A declaration is stronger than the generic rule: the rows are one
        # object, so a word naming row 17 of the asset table is a label inside
        # it and not a second section. Both ends are points, and nothing
        # between them is.
        named -= set(a for a in named if lo < a < hi)
        named.add(lo)
        named.add(hi)

    # Only the declared tables stop a string run: see objectify.string_runs.
    table_bounds = set()
    for table in tables:
        table_bounds.add(table.address)
        table_bounds.add(table.end)
    # A RAM item's initialiser starts at a point the code takes the address of,
    # which is a declaration in the same sense a table's bounds are: the bytes
    # on both sides are still whatever they were, and a run of non-NUL bytes
    # that crosses the point would put two RAM objects in one section.
    table_bounds |= set(i.load for i in ram.items if i.kind == "data")
    strings = objectify.string_runs(blob, covered, table_bounds)
    # A word that becomes a relocation is one slot whatever its target is, so
    # the four bytes may not be split between two sections. A RAM word is one
    # of those now that the item it names has a section.
    slots = [w["addr"] for w in words if w["class"] == "pointer"]
    slots += [w["addr"] for w in words
              if w["signal"] == "ram" and ram.at(w["value"]) is not None]
    sections, shared, slivers, distant = objectify.build_sections(
        items, refs["calls"], reads, refs["fallthrough"], slots, strings,
        reserved, named)
    layout = objectify.Layout(sections)
    ramlayout = RamLayout(ram, sections, "*" + os.path.basename(args.o),
                          reserved)

    retarget = replacements.bind(layout, refs, reads, words)
    owned, boundary_counts = boundary_relocations(blob, boundary, layout)
    owned |= pruned
    counts, unrelocatable, indirect = objectify.internal_relocations(
        blob, layout, refs["calls"], owned, retarget)
    # A replacement's RAM global goes first: the object it names has moved into
    # the library, so the word is the library's to answer and not a reference
    # to the blob's item at that address. Then the RAM pass, before the word
    # pass, because a word the startup reads a run bound out of is a bound and
    # not a pointer to whatever happens to be there: the copy's source word
    # names the whole initialiser image, and binding it to the first item's
    # section would relocate it to that item's RAM address.
    global_counts = objectify.global_relocations(blob, layout, words, owned,
                                                 replacements.globals)
    ram_counts, ram_unrelocated = ram_relocations(blob, layout, ramlayout,
                                                  words, owned)
    word_counts = objectify.word_relocations(blob, layout, words, owned, retarget)
    if unrelocatable:
        for row in unrelocatable[:20]:
            print("0x%x: %s (%d bytes) to 0x%x leaves its section and cannot be"
                  " relocated" % (row["from"], row["mnemonic"], row["width"], row["to"]),
                  file=sys.stderr)
        sys.exit("%d branches cross a section boundary without a relocation;"
                 " the partition has to put them back together" % len(unrelocatable))

    # --data-source hands the data sections to abi/datagen.py: they leave this
    # object entirely and are assembled or compiled from the files it writes,
    # which means every name that crosses between the two halves has to become
    # global. The reference sets decide which: a symbol is exported only where
    # something in the other object reaches it, because the partition hands the
    # blob's own copy of a library body the library's own name and a global one
    # would swallow the link meant for the source build.
    delegated = [s for s in sections if s.kind == "data"] if args.data_source else []
    in_blob = [s for s in sections if s.kind != "data"] if args.data_source else list(sections)
    # A `.bss` section has no bytes, so there is nothing for datagen to write
    # and nothing a source file could say about it that the size does not: it
    # stays in the blob object under every configuration.
    in_blob += ramlayout.bss
    for i, s in enumerate(in_blob):
        s.index = i
    delegated_set = set(id(s) for s in delegated)

    def section_names(s):
        """Every name the object gives this section, as (offset, name, is_func)."""
        rows = [(0, s.sym, s.kind == "code"), (0, address_alias(s.start),
                                               s.kind == "code")]
        rows += [(0, alias, False) for alias in s.aliases]
        for addr in s.functions:
            rows.append((addr - s.start, address_alias(addr), True))
        for (addr, is_func), label in sorted(s.labels.items()):
            rows.append((addr - s.start, label, is_func))
            rows.append((addr - s.start, address_alias(addr), is_func))
        seen, out = set(), []
        for off, name, is_func in rows:
            if name not in seen:
                seen.add(name)
                out.append((off, name, is_func))
        return out

    reached_from = collections.defaultdict(set)
    for s in sections:
        for _, sym, _ in s.relocs:
            reached_from[sym].add(id(s))
    forced = {}

    symbols = Symbols()
    for s in in_blob:
        styp = STT_FUNC if s.kind == "code" else STT_OBJECT
        # ARM ELF carries Thumb-ness in bit 0 of a function symbol's value.
        symbols.add(s.sym, 1 if styp == STT_FUNC else 0, s.index + 1, STB_LOCAL, styp)
        for (addr, is_func), label in sorted(s.labels.items()):
            # STT_FUNC, because bit 0 only means Thumb on a function symbol: a
            # notype target makes the linker read the call as interworking and
            # plant a veneer.
            symbols.add(label, (addr - s.start) | (1 if is_func else 0), s.index + 1,
                        STB_LOCAL, STT_FUNC if is_func else STT_OBJECT)
    # The name a body carries is whatever the analysis last called it, so a file
    # that has to point at a body cannot key on it. The original address is the
    # one coordinate nothing renames, and this is it as a symbol: sim.yaml,
    # prunes.yaml and replacements.yaml resolve through these, so a link that
    # moved the body still answers where it went.
    for s in in_blob:
        styp = STT_FUNC if s.kind == "code" else STT_OBJECT
        symbols.add(address_alias(s.start), 1 if styp == STT_FUNC else 0,
                    s.index + 1, STB_LOCAL, styp)
        for alias in s.aliases:
            symbols.add(alias, 0, s.index + 1, STB_LOCAL, STT_OBJECT)
        for addr in s.functions:
            symbols.add(address_alias(addr), (addr - s.start) | 1, s.index + 1,
                        STB_LOCAL, STT_FUNC)
        for (addr, is_func), _ in sorted(s.labels.items()):
            symbols.add(address_alias(addr), (addr - s.start) | (1 if is_func else 0),
                        s.index + 1, STB_LOCAL, STT_FUNC if is_func else STT_OBJECT)

    exported = {}

    def export(name, addr, styp):
        section = layout.at(addr)
        if section is None:
            sys.exit("cannot export %s: 0x%x is outside the app" % (name, addr))
        # The boundary's and the replacements' published names are how the rest
        # of the link line reaches into the blob, and they are not among the
        # names `section_names` gives a section, so the gc walk needs them
        # separately: the kernel's three application hooks and the five
        # syscalls only libc.a calls are all of this kind.
        exported.setdefault(name, section)
        if id(section) in delegated_set:
            # The definition is in datagen's file, so this object only declares
            # the name and the fragment there publishes it. A function name can
            # land here: a body range no function entry point opens is a data
            # component, and the send gate's tail-call target is one, so the
            # Thumb bit travels with the name rather than the section's kind.
            forced.setdefault(id(section), []).append(
                (addr - section.start, name, styp == STT_FUNC))
            symbols.undefined(name)
            return
        thumb = 1 if styp == STT_FUNC else 0
        symbols.globalise(name, (addr - section.start) | thumb, section.index + 1, styp)

    for e in boundary.get("startup", []):
        export(e["original_symbol"], int(e["original"]), STT_FUNC)
    # A name a replacement owns is undefined in the object, so the boundary
    # cannot also export the blob's copy of it: memset and memcpy are the app
    # bodies the source kernel calls and are also libc bodies the newlib group
    # takes from the archive, and both halves have to reach the same one.
    replaced_names = set(replacements.symbols())
    for e in boundary["lib_to_app"]:
        if e["symbol"] and not e.get("library_not_app") \
                and e["symbol"] not in replaced_names:
            export(e["symbol"], int(e["addr"]), STT_FUNC)
    for v in boundary["data_references"]["vector_table"]:
        if not v.get("relocate") and v["symbol"] not in replaced_names:
            export(v["symbol"], v["word"] & ~1, STT_FUNC)
    for e in replacements.exports:
        export(e["as"], e["at"], STT_OBJECT if e.get("kind") == "data" else STT_FUNC)
    for e in replacements.entries:
        export("orig_" + e["symbol"], e["at"], STT_FUNC)
    replacements.header(args.replace_header)
    export("appl_vector_table", APP_BASE, STT_NOTYPE)
    export("appl_blob_start", APP_BASE, STT_NOTYPE)
    symbols.globalise("appl_blob_end", APP_END, SHN_ABS, STT_NOTYPE)

    # A name datagen's half reaches has to be global here, and the other way
    # round is the `exported` flag below.
    for s in delegated:
        for _, sym, _ in s.relocs:
            row = symbols.rows[symbols.index[sym]] if sym in symbols.index else None
            if row is not None and row[2] != 0:
                symbols.globalise(sym, row[1], row[2], row[4])

    used = set()
    for s in in_blob:
        used.update(sym for _, sym, _ in s.relocs)
    for sym in sorted(used - set(symbols.index)):
        symbols.undefined(sym)
    # The boundary's whole point is that the library answers these, so each has
    # to be undefined here. A defined one means the partition named a blob
    # section after it and the call would go back into the blob's own copy,
    # which the byte-identical link cannot see because the addresses agree.
    for name, where in reserved.items():
        if where != NOWHERE or name not in symbols.index:
            continue
        if symbols.rows[symbols.index[name]][2] != 0:
            sys.exit("%s is defined in the blob, so the library would never be"
                     " called for it" % name)

    # The keep list is decided before the layout, because --layout pack needs to
    # know what --gc-sections will drop: a section the object does not reach and
    # the list does not keep is one the linker removes, and its bytes are free
    # for the packing exactly as a replaced body's are. A section that survives
    # after all is not placed by the fragment and falls into relink.ld's
    # `.blobdead`, where it is linked correctly and shows up in the map.
    keep, outside = None, []
    if args.gc or args.also_linked:
        outside, by_object = linked_roots(sections, section_names,
                                          args.also_linked, exported)
        for path, named in sorted(by_object.items()):
            print("  %s enters the blob at %d of its sections"
                  % (os.path.basename(path), len(named)))
    if args.gc:
        keep = gc_keep_list(sections, layout, pinned_addresses(facts, layout),
                            words, args.reach, section_names,
                            replacements.by_start, outside)
        if args.keep_also:
            by_name = {s.name: s for s in sections}
            listed = set(s.name for s, _ in keep)
            for line in open(args.keep_also):
                name = line.strip()
                if name and name not in listed:
                    keep.append((by_name[name], "asked for on the command line"))

    # What --gc-sections may not drop in RAM. Every relocation into a RAM item
    # is an edge the linker walks itself, so the only items that need naming
    # are the ones a word reaches without a relocation: a word the
    # classification left undecided, and a word whose value lands in an item
    # but which some other rule already claimed. Both are the flash side's
    # review list, read in RAM.
    ram_keep = {}
    for row in ram_unrelocated:
        ram_keep.setdefault(ram.at(row["value"]).start,
                            "named by the unrelocated word 0x%x" % row["addr"])
    ram_was = [s.vma for s in ramlayout.sections]
    if args.ram_layout:
        # LIBRARY_RAM's base is the bound: facts.yaml argues that the RAM
        # between the app's statics and it was never written in a 600 s run,
        # which is the room a moved layout has, and the library's own statics
        # start there.
        free_end = next(r["start"] for r in facts["regions"]
                        if r["name"] == "LIBRARY_RAM")
        objectify.ram_relayout(ram.runs, ramlayout.sections, args.ram_layout,
                               free_end)
        print("  ram layout %s: %d items moved, 0x%08x..0x%08x"
              % (args.ram_layout,
                 sum(1 for s, was in zip(ramlayout.sections, ram_was)
                     if s.vma != was),
                 ram.runs[0].start, ram.runs[-1].end))

    hole, dead = None, set()
    if args.layout:
        # A layout moves the app's data as well as its text, and the data can
        # only be linked where it has been taken out of the blob object: the
        # placement has to name the object each section comes from, and the
        # proof that the source reproduces the image's data is the byte-identical
        # identity link with --data-source on. Moving it without that would be
        # testing the cut and the move at once.
        if not args.data_source:
            sys.exit("--layout %s moves the app's data as well as its text, and"
                     " the data has to be linked from abi/datagen.py's source"
                     " for that: rerun with DATA=1 (--data-source <dir>)"
                     % args.layout)
        pinned = pinned_addresses(facts, layout)
        anchors = dict((w["value"] & ~1, "review") for w in words
                       if w["class"] == "review")
        for d in classified["displacements"]:
            anchors[d["site"]] = "displacement"
            anchors[d["target"]] = "displacement"
        # A run that is copied out in one go is one object to the code that
        # reads it and many items to the partition: the copy's length comes
        # from two RAM addresses and only the run's first byte is ever named,
        # so every byte behind it is reached by still being where it was. The
        # RAM initialiser image at 0xefe58 is 4836 bytes over 79 items, and
        # scattering them leaves the copy reading whatever the layout put after
        # the first one -- which is the app's whole .data, so the watch comes up
        # with its device tables full of the wrong words. Holding the run is the
        # honest fix while the cut has no way to say "these items move together".
        # A run that is copied out in one go is one object to the code that
        # reads it and many items to the partition: the copy's length comes
        # from two RAM addresses and only the run's first byte is ever named,
        # so every byte behind it is reached by still being where it was. The
        # length is also the one thing in the image that says where such an
        # object ends, so it is a cut as well as a hold: without it the version
        # trailer, which starts where the RAM initialiser image stops, is held
        # by a copy that does not reach it.
        cuts = set()
        for w in words:
            if not w.get("span"):
                continue
            cuts.add(w["target"])
            cuts.add(w["target"] + w["span"])
            for s in sections:
                if s.start < w["target"] + w["span"] and w["target"] < s.end:
                    anchors[s.start] = "block a single copy reads"
        if args.layout == "pack":
            dead = set(replacements.by_start)
            if keep is not None:
                survives = gc_reachable(sections, section_names,
                                        layout.at(APP_BASE),
                                        [s for s, _ in keep] + outside)
                dead |= set(s.start for s in sections if s.start not in survives)
        else:
            dead = set()
        moves, spare, held, dead = objectify.relayout(
            sections, args.layout, pinned, anchors, cuts, dead, data=True)
        by_kind = collections.Counter(s.kind for s in sections if s.sym in moves)
        if args.layout == "pack":
            hole, spare = spare, objectify.SPARE_BASE
            print("  layout pack: %d sections (%d text, %d data) packed over %d"
                  " dropped, leaving 0x%x..0x%x, %d bytes, for %s"
                  % (len(moves), by_kind["code"], by_kind["data"], len(dead),
                     hole[0], hole[1], hole[1] - hole[0],
                     ", ".join(args.spill) or "nothing"))
        else:
            print("  layout %s: %d sections moved (%d text, %d data), %d bytes"
                  " of spare flash used"
                  % (args.layout, len(moves), by_kind["code"], by_kind["data"],
                     spare - objectify.SPARE_BASE))
        for why in sorted(set(held.values())):
            for kind in ("code", "data"):
                kept = [s for s in sections
                        if held.get(s.start) == why and s.kind == kind]
                if kept:
                    print("  %d %s sections held in place by a %s (%d bytes)"
                          % (len(kept), "text" if kind == "code" else "data",
                             why, sum(s.end - s.start for s in kept)))
        for kind in ("code", "data"):
            pins = [s for s in sections if s.start in pinned and s.kind == kind]
            if pins:
                print("  %d %s sections pinned by a fixed point: %s"
                      % (len(pins), "text" if kind == "code" else "data",
                         ", ".join(s.sym for s in pins)))
    unknown = set(moves) - set(s.sym for s in sections)
    if unknown:
        sys.exit("no section named %s" % ", ".join(sorted(unknown)))
    for path in (args.o, args.place, args.stock):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    data_files, untyped = [], []
    if args.data_source:
        data_files, untyped = emit_data(
            args.data_source, args.data_sources, delegated, blob, section_names,
            forced, reached_from, reserved, declared_tables(tables),
            types.declared)
    build(in_blob, blob, symbols, args.o)
    obj = "*" + os.path.basename(args.o)
    if args.gc:
        objectify.placement(sections, moves, args.place, obj,
                            keep=dict((s.start, why) for s, why in keep),
                            drop=dead, hole=hole, spill=args.spill,
                            ram=ramlayout, ram_keep=ram_keep)
        reasons = collections.Counter(why.split(" 0x")[0].split("/")[0]
                                      for _, why in keep)
        print("  --gc keeps %d sections (%d bytes) the linker cannot see: %s"
              % (len(keep), sum(s.end - s.start for s, _ in keep),
                 ", ".join("%s %d" % (k, n) for k, n in sorted(reasons.items()))))
    elif args.reclaim:
        objectify.reclaim_placement(sections, pinned_addresses(facts, layout),
                                    args.place, obj, ramlayout)
    else:
        objectify.placement(sections, moves, args.place, obj, drop=dead,
                            hole=hole, spill=args.spill, ram=ramlayout)
    # Where each RAM item is and where its bytes come from, for the scan: a
    # word inside the initialiser image is read at a RAM address in the linked
    # ELF and was classified at the flash address it is loaded from, so the
    # scan cannot join the two without this.
    with open(os.path.splitext(args.place)[0] + "-ram.json", "w") as fh:
        json.dump([{"section": s.name, "vma": s.vma, "was": was,
                    "size": s.end - s.start,
                    "lma": None if s.nobits else s.start}
                   for s, was in zip(ramlayout.sections, ram_was)], fh)
    with open(os.path.splitext(args.place)[0] + "-moves.json", "w") as fh:
        json.dump([{"section": s.sym, "old": s.start, "end": s.end,
                    "new": moves[s.sym]} for s in sections if s.sym in moves], fh)
    stock = stock_definitions(boundary, replacements, args.stock)

    for s in slivers:
        print("0x%x..0x%x belongs to no item of the partition" % (s.start, s.end),
              file=sys.stderr)
    code = sum(1 for s in sections if s.kind == "code")
    print("%s: %d sections (%d code, %d data), %d symbols"
          % (args.o, len(sections), code, len(sections) - code, len(symbols.rows)))
    print("  %d sections hold more than one function (%d functions, %d bytes):"
          " interleaved code that cannot be separated without moving bytes"
          % (len(shared), sum(len(s.functions) for s in shared),
             sum(s.end - s.start for s in shared)))
    print("  %d NUL-delimited runs outside the disassembly keep their section"
          " whole" % len(strings))
    print("  %d literal pools sit out of load range of the function the export"
          " reads them from, so their reader is still unknown" % len(distant))
    print("  boundary: %d R_ARM_THM_CALL, %d R_ARM_THM_JUMP24, %d R_ARM_ABS32"
          % (boundary_counts[R_ARM_THM_CALL], boundary_counts[R_ARM_THM_JUMP24],
             boundary_counts[R_ARM_ABS32]))
    print("  internal: %d R_ARM_THM_CALL, %d R_ARM_THM_JUMP24, %d R_ARM_THM_JUMP19,"
          " %d cross-section transfers through a register or a word"
          % (counts[R_ARM_THM_CALL], counts[R_ARM_THM_JUMP24],
             counts[R_ARM_THM_JUMP19], indirect))
    print("  words: %d R_ARM_ABS32 (%d into code, %d into RAM, %d RAM addresses"
          " no item holds and that stay constants)"
          % (word_counts["pointer"] + ram_counts["pointer"],
             word_counts["into_code"], ram_counts["pointer"],
             ram_counts["fixed"]))
    print("  ram: %d items (%d .data in %d bytes, %d .bss in %d bytes) over"
          " 0x%08x..0x%08x"
          % (len(ram.items),
             sum(1 for i in ram.items if i.kind == "data"),
             sum(i.size for i in ram.items if i.kind == "data"),
             sum(1 for i in ram.items if i.kind == "bss"),
             sum(i.size for i in ram.items if i.kind == "bss"),
             ram.start, ram.end))
    if replacements.globals:
        print("  globals: %s"
              % ", ".join("%s (0x%x) relocated in %d words" % (g["as"], g["at"],
                                                               global_counts[g["as"]])
                          for g in replacements.globals))
    if replacements.refused:
        print("  %d derived replacements refused, so the blob keeps those bodies:"
              % len(replacements.refused))
        for e, why in replacements.refused:
            print("      %-22s 0x%05x  %s" % (e["symbol"], e["at"], why))
    loose = unbound_callees(replacements, boundary, refs, layout)
    if loose:
        print("  %d replaced bodies call a body the link still takes from the"
              " blob:" % len(loose))
        for e, targets in loose:
            print("      %-22s 0x%05x -> %s" % (e["symbol"], e["at"],
                  ", ".join("0x%05x %s" % (t, layout.at(t).sym
                                           if layout.at(t) else "?")
                            for t in targets[:4])))
    if replacements.entries:
        print("  replaced %s: %d sections (%d bytes) renamed orig_* and left"
              " referenced by nothing"
              % (", ".join(sorted(set(e["group"] for e in replacements.entries))),
                 len(replacements.by_start),
                 sum(layout.at(a).end - a for a in replacements.by_start)))
    if args.data_source:
        typed = [f for f in data_files if f.kind == "typed"]
        raw = [f for f in data_files if f.kind == "bytes"]
        print("  data from source: %d sections (%d bytes) as bytes, %d (%d bytes)"
              " as typed tables, in %s"
              % (len(raw), sum(f.bytes for f in raw), len(typed),
                 sum(f.bytes for f in typed), args.data_source))
        for name, why in untyped:
            print("      %-28s stays bytes: %s" % (name, why))
    print("  %s, %s (%d stand-in definitions)"
          % (args.place, args.stock, stock))


if __name__ == "__main__":
    main()
