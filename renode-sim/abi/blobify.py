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

import objectify
from objectify import (APP_BASE, APP_END, R_ARM_ABS32, R_ARM_THM_CALL,
                       R_ARM_THM_JUMP24, R_ARM_THM_JUMP19, SELF_BL, SELF_B)

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)

STT_NOTYPE, STT_OBJECT, STT_FUNC = 0, 1, 2
STB_LOCAL, STB_GLOBAL = 0, 1
SHN_ABS = 0xFFF1


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
    SHT_PROGBITS, SHT_REL, SHT_SYMTAB, SHT_STRTAB = 1, 9, 2, 3
    SHF_ALLOC, SHF_EXEC = 2, 4
    symtab_index = 1 + len(sections) + len(rel_of)

    for s in sections:
        bodies.append(bytes(blob[s.start - APP_BASE:s.end - APP_BASE]))
        align = 4 if s.start % 4 == 0 else (2 if s.start % 2 == 0 else 1)
        flags = SHF_ALLOC | (SHF_EXEC if s.kind == "code" else 0)
        headers.append([name_in(shstr, s.name), SHT_PROGBITS, flags, 0, 0,
                        len(bodies[-1]), 0, 0, align, 0])
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

    strtab = bytearray(b"\0")
    shstr = bytearray(b"\0")
    symtab = bytearray(b"\0" * 16)
    for sym, value in sorted(defs.items()):
        off = len(strtab)
        strtab += sym.encode() + b"\0"
        symtab += struct.pack("<IIIBBH", off, value, 0,
                              (STB_GLOBAL << 4) | STT_FUNC, 0, SHN_ABS)

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
    return len(defs)


def pinned_addresses(manifest, layout):
    """The section starts hwa10.yaml's fixed points name, checked against the cover."""
    pinned = set()
    for f in manifest["fixed_points"]:
        addr = int(f["addr"], 0) if isinstance(f["addr"], str) else f["addr"]
        section = layout.at(addr)
        if section is None:
            sys.exit("fixed point %s at 0x%x is outside the app" % (f["name"], addr))
        pinned.add(section.start)
    return pinned

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
        for name in wanted:
            group = groups[name]
            self.sources += group.get("sources", [])
            for e in group.get("exports", []):
                self.exports.append(e)
            for e in group["replacements"]:
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
        self.by_start = {}

    def symbols(self):
        return [e["symbol"] for e in self.entries]

    def bind(self, layout, refs, reads, words):
        """Check each replacement against the partition, then rename and retarget.

        Everything refused here is a reference the linker has no way to express
        against a symbol that is not the section's own start: a second function
        in the same bytes, a fall-through, a pool word read from outside, an
        entry into the middle. Each is a fact about the image, so it is refused
        rather than worked around.
        """
        retarget = {}
        for e in self.entries:
            at, symbol = e["at"], e["symbol"]
            section = layout.at(at)
            if section is None:
                sys.exit("replacement %s: 0x%x is outside the app" % (symbol, at))
            if section.start != at:
                sys.exit("replacement %s: 0x%x is inside the section %s at 0x%x,"
                         " not its start" % (symbol, at, section.sym, section.start))
            if section.kind != "code":
                sys.exit("replacement %s: 0x%x is a %s section"
                         % (symbol, at, section.kind))
            if len(section.functions) > 1:
                sys.exit("replacement %s: the section at 0x%x holds %d functions"
                         " (%s); a replacement is one definition"
                         % (symbol, at, len(section.functions),
                            ", ".join("0x%x" % f for f in section.functions)))
            if section.labels:
                sys.exit("replacement %s: the section at 0x%x carries the interior"
                         " symbols %s, so something names bytes inside the body"
                         % (symbol, at, ", ".join(sorted(section.labels.values()))))
            inside = lambda a: section.start <= a < section.end
            for row in refs["fallthrough"]:
                if inside(row["to"]) and not inside(row["from"]):
                    sys.exit("replacement %s: 0x%x falls through into 0x%x, and a"
                             " replacement cannot be fallen into"
                             % (symbol, row["from"], row["to"]))
                if inside(row["from"]) and not inside(row["to"]):
                    sys.exit("replacement %s: 0x%x falls through out of the body"
                             " into 0x%x, so the body is not a whole function"
                             % (symbol, row["from"], row["to"]))
            for site, target in reads:
                if inside(target) and not inside(site):
                    sys.exit("replacement %s: 0x%x reads the word 0x%x inside the"
                             " body, which goes away with it"
                             % (symbol, site, target))
            for row in refs["calls"]:
                if row["external"] or not inside(row["to"]) or inside(row["from"]):
                    continue
                if row["to"] != at:
                    sys.exit("replacement %s: 0x%x enters the body at 0x%x, which"
                             " is not its entry point" % (symbol, row["from"], row["to"]))
            for word in words:
                target = word.get("target")
                if target is None or not inside(target):
                    continue
                if word["class"] == "review":
                    sys.exit("replacement %s: the word 0x%x is still unclassified"
                             " and may name 0x%x" % (symbol, word["addr"], target))
                if word["class"] != "pointer":
                    continue
                if target != at or not word["thumb_target"]:
                    sys.exit("replacement %s: the word 0x%x names 0x%x%s, not the"
                             " entry point" % (symbol, word["addr"], target,
                                               "" if word["thumb_target"] else " as data"))
            if section.sym == symbol:
                sys.exit("replacement %s: the partition still holds the name, so"
                         " the reservation did not take" % symbol)
            section.sym = "orig_" + symbol
            section.name = ".text." + section.sym
            retarget[at] = symbol
            self.by_start[section.start] = e
        return retarget

    def header(self, path):
        """out/replace.h: what the sources may call, and nothing else.

        Each declaration is emitted twice for a replacement, once under the
        symbol the call sites now bind to and once under `orig_<symbol>`, which
        is the original body the object still carries.
        """
        lines = ["/* Generated by abi/blobify.py from abi/replacements.yaml --"
                 " do not edit. */", "#ifndef REPLACE_H", "#define REPLACE_H", ""]
        for e in self.exports:
            lines.append("/* 0x%x: %s */" % (e["at"], " ".join(str(e["why"]).split())))
            proto = e["proto"]
            lines.append(proto if proto.startswith("extern") else "extern " + proto)
        for e in self.entries:
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

    Every edit declares the bytes it expects and the partition symbol whose
    body or item the address is in. Both are checked: the bytes catch an edit
    written against another image, and the symbol catches a partition that has
    moved the site under it. An edit is applied before the cut, so what the
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


def gc_keep_list(sections, layout, pinned, words, reach_path, replaced=()):
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
    """
    if not os.path.exists(reach_path):
        sys.exit("%s does not exist; run abi/reach.py first" % reach_path)
    with open(reach_path) as fh:
        reach = json.load(fh)
    keep, seen = [], set()
    already = set(reach["gc_sections"]["addrs"])

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
    ap.add_argument("--layout", choices=("shift", "reverse"),
                    help="move every text section: `shift` keeps the order and"
                         " slides it, `reverse` turns it round. The stale-address"
                         " scan is what this is for.")
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
    manifest = yaml.safe_load(open(os.path.join(HERE, "hwa10.yaml")))
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
    # `adr rN,#imm` is the other pc-relative reference the image holds and the
    # only one with no relocation at all: 21 of the 179 in this image name a
    # data item outside the function's section, so unless the two ends are one
    # section the instruction computes a stale address as soon as the code
    # moves. They bind exactly like a literal pool's word does.
    reads = [(r["site"], r["target"])
             for r in refs["pool_reads"] + refs["pc_addresses"]]
    bits = bytes.fromhex(items["instruction_bytes"]["bits"])
    covered = bytes((bits[i >> 3] >> (i & 7)) & 1 for i in range(len(blob)))
    strings = objectify.string_runs(blob, covered)
    # What a data tile has to be its own section for: a word names it, an `adr`
    # computes it, the boundary or a fixed point needs the symbol at that
    # address. Anything else in a run of data is a field of the object above it.
    named = set(w["target"] for w in words if w["class"] == "pointer")
    named |= set(w["value"] & ~1 for w in words if w["class"] == "pointer")
    named |= set(r["target"] for r in refs["pc_addresses"])
    named |= set(a for a in reserved.values() if a >= 0)
    named |= set(int(str(f["addr"]), 0) for f in manifest["fixed_points"])
    named |= set(f["start"] for f in items["functions"])
    sections, shared, slivers, distant = objectify.build_sections(
        items, refs["calls"], reads, refs["fallthrough"],
        [w["addr"] for w in words if w["class"] == "pointer"], strings, reserved,
        named)
    layout = objectify.Layout(sections)

    retarget = replacements.bind(layout, refs, reads, words)
    owned, boundary_counts = boundary_relocations(blob, boundary, layout)
    owned |= pruned
    counts, unrelocatable, indirect = objectify.internal_relocations(
        blob, layout, refs["calls"], owned, retarget)
    word_counts = objectify.word_relocations(blob, layout, words, owned, retarget)
    if unrelocatable:
        for row in unrelocatable[:20]:
            print("0x%x: %s (%d bytes) to 0x%x leaves its section and cannot be"
                  " relocated" % (row["from"], row["mnemonic"], row["width"], row["to"]),
                  file=sys.stderr)
        sys.exit("%d branches cross a section boundary without a relocation;"
                 " the partition has to put them back together" % len(unrelocatable))

    symbols = Symbols()
    for s in sections:
        styp = STT_FUNC if s.kind == "code" else STT_OBJECT
        # ARM ELF carries Thumb-ness in bit 0 of a function symbol's value.
        symbols.add(s.sym, 1 if styp == STT_FUNC else 0, s.index + 1, STB_LOCAL, styp)
        for (addr, is_func), label in sorted(s.labels.items()):
            # STT_FUNC, because bit 0 only means Thumb on a function symbol: a
            # notype target makes the linker read the call as interworking and
            # plant a veneer.
            symbols.add(label, (addr - s.start) | (1 if is_func else 0), s.index + 1,
                        STB_LOCAL, STT_FUNC if is_func else STT_OBJECT)

    def export(name, addr, styp):
        section = layout.at(addr)
        if section is None:
            sys.exit("cannot export %s: 0x%x is outside the app" % (name, addr))
        thumb = 1 if styp == STT_FUNC else 0
        symbols.globalise(name, (addr - section.start) | thumb, section.index + 1, styp)

    for e in boundary.get("startup", []):
        export(e["original_symbol"], int(e["original"]), STT_FUNC)
    for e in boundary["lib_to_app"]:
        if e["symbol"] and not e.get("library_not_app"):
            export(e["symbol"], int(e["addr"]), STT_FUNC)
    for v in boundary["data_references"]["vector_table"]:
        if not v.get("relocate"):
            export(v["symbol"], v["word"] & ~1, STT_FUNC)
    for e in replacements.exports:
        export(e["as"], e["at"], STT_OBJECT if e.get("kind") == "data" else STT_FUNC)
    for e in replacements.entries:
        export("orig_" + e["symbol"], e["at"], STT_FUNC)
    replacements.header(args.replace_header)
    export("appl_vector_table", APP_BASE, STT_NOTYPE)
    export("appl_blob_start", APP_BASE, STT_NOTYPE)
    symbols.globalise("appl_blob_end", APP_END, SHN_ABS, STT_NOTYPE)

    used = set()
    for s in sections:
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

    if args.layout:
        pinned = pinned_addresses(manifest, layout)
        anchors = dict((w["value"] & ~1, "review") for w in words
                       if w["class"] == "review")
        for d in classified["displacements"]:
            anchors[d["site"]] = "displacement"
            anchors[d["target"]] = "displacement"
        moves, spare, held = objectify.relayout(sections, args.layout, pinned,
                                                anchors)
        print("  layout %s: %d text sections moved, %d bytes of spare flash used"
              % (args.layout, len(moves), spare - objectify.SPARE_BASE))
        for why in sorted(set(held.values())):
            kept = [s for s in sections if held.get(s.start) == why]
            print("  %d text sections held in place by a %s (%d bytes)"
                  % (len(kept), why, sum(s.end - s.start for s in kept)))
    unknown = set(moves) - set(s.sym for s in sections)
    if unknown:
        sys.exit("no section named %s" % ", ".join(sorted(unknown)))
    for path in (args.o, args.place, args.stock):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    build(sections, blob, symbols, args.o)
    obj = "*" + os.path.basename(args.o)
    if args.gc:
        pinned = pinned_addresses(manifest, layout)
        keep = gc_keep_list(sections, layout, pinned, words, args.reach,
                            replacements.by_start)
        if args.keep_also:
            by_name = {s.name: s for s in sections}
            listed = set(s.name for s, _ in keep)
            for line in open(args.keep_also):
                name = line.strip()
                if name and name not in listed:
                    keep.append((by_name[name], "asked for on the command line"))
        objectify.placement(sections, moves, args.place, obj,
                            keep=dict((s.start, why) for s, why in keep))
        reasons = collections.Counter(why.split(" 0x")[0].split("/")[0]
                                      for _, why in keep)
        print("  --gc keeps %d sections (%d bytes) the linker cannot see: %s"
              % (len(keep), sum(s.end - s.start for s, _ in keep),
                 ", ".join("%s %d" % (k, n) for k, n in sorted(reasons.items()))))
    elif args.reclaim:
        objectify.reclaim_placement(sections, pinned_addresses(manifest, layout),
                                    args.place, obj)
    else:
        objectify.placement(sections, moves, args.place, obj)
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
    print("  words: %d R_ARM_ABS32 (%d into code, %d RAM pointers left as"
          " constants because RAM does not move yet)"
          % (word_counts["pointer"], word_counts["into_code"], word_counts["ram"]))
    if replacements.entries:
        print("  replaced %s: %d sections (%d bytes) renamed orig_* and left"
              " referenced by nothing"
              % (", ".join(sorted(set(e["group"] for e in replacements.entries))),
                 len(replacements.by_start),
                 sum(layout.at(a).end - a for a in replacements.by_start)))
    print("  %s, %s (%d stand-in definitions)"
          % (args.place, args.stock, stock))


if __name__ == "__main__":
    main()
