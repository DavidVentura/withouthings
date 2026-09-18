#!/usr/bin/env python3
"""Check a newlib build against the library bodies the image carries.

    python3 abi/libc_check.py [--build newlib-nano-big] [--class libc|libm]

abi/autonames.py names 98 libc and libgcc bodies by matching the Arm GNU
Toolchain 13.2.Rel1 prebuilt archives against the image. This asks the stronger
question the relink needs answered: does a newlib built from source
(abi/refbuild.sh) reproduce those bodies byte for byte where they sit?

A body is compared with every field a relocation owns masked out -- the
displacement of a call, the word of a literal pool -- because those are the only
bytes the linker writes and they cannot agree before a link. Everything else is
the compiler's output and has to agree exactly or the configuration is wrong.
That is what decides configure options the archives cannot: the image's
__sseek stores FILE->_offset at +0x50, which is the full struct _reent layout,
so _REENT_SMALL is off, and with it off five stdio bodies that differed agree.

The bytes are not the whole question. The linker owns every call displacement,
so two functions that lock, call one thing and unlock are the same bytes and
differ only in where the three branches go: the image's 0x9bd0c is a Withings
SPI-flash wrapper whose ten unrelocated bytes are newlib's _tzset_r exactly, and
replacing it would have put tzset behind a flash lock that only the shell
reaches. So a body that reproduces is then held to three more refusals --
`callees_differ` where the image's calls do not land on the archive's callees,
`bad_entry` where the partition starts no function, `multi_entry` where the
instruction before falls through in and the section has a second entry libgcc
exports, and `shape_only` where the body is too short to be anything and does
nothing of its own.

--emit writes the same verdicts as a file, which is what abi/replacements.yaml's
`newlib` group derives its entries from: a body the build reproduces is one the
link may take from the archive instead of the blob, and a body it does not is
one the blob has to keep. Deriving them rather than listing them is the point --
the list is 95 names long and is a measurement, not a decision.
"""

import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
APP_BASE = 0x27000


class Archive(object):
    """Every function body an archive defines, by symbol name."""

    def __init__(self, tools, scratch):
        self.tools, self.scratch, self.bodies = tools, scratch, {}
        # Two symbols at one object offset are one body under two names, which
        # is the only thing that makes a callee's name disagree harmlessly.
        self.aliases = collections.defaultdict(set)
        self.at = {}
        self._relocs, self._disasm = {}, {}

    def add(self, path):
        into = os.path.join(self.scratch, os.path.basename(path).replace(".", "_"))
        if not os.path.isdir(into):
            os.makedirs(into)
            subprocess.run([self.tools + "ar", "x", path], cwd=into, check=True)
        for obj in sorted(glob.glob(os.path.join(into, "*.o"))):
            listing = subprocess.run([self.tools + "objdump", "-t", obj],
                                     capture_output=True, text=True).stdout
            found, offsets = [], {}
            for line in listing.splitlines():
                row = line.split()
                sections = [w for w in row if w.startswith(".text")]
                if len(row) < 6 or "F" not in row[2:5] or not sections:
                    continue
                section = sections[0]
                off = int(row[0], 16)
                size = int(row[row.index(section) + 1], 16)
                found.append((section, off, size, row[-1], row[1]))
                offsets.setdefault(section, set()).add(off)
            # An assembly body has no .size directive, so its symbol carries
            # size 0: memchr and the __aeabi_?ldivmod pair are written in .S and
            # would otherwise read as absent from the build. The extent is then
            # the next symbol in the same section, or the section's end.
            ends = self.section_sizes(obj) if any(s == 0 for _, _, s, _, _ in found) else {}
            sized = []
            for section, off, size, name, bind in found:
                if not size:
                    after = sorted(o for o in offsets[section] if o > off)
                    size = (after[0] if after else ends.get(section, off)) - off
                sized.append((section, off, size, name, bind))
                if size and name not in self.bodies:
                    self.bodies[name] = (obj, section, off, size, bind)
            here = {}
            for section, off, size, name, bind in sized:
                here.setdefault((section, off), []).append((name, bind))
            self.at[obj] = here
            for names in here.values():
                if len(names) > 1:
                    for name, _ in names:
                        self.aliases[name] |= set(n for n, _ in names)

    def section_sizes(self, obj):
        listing = subprocess.run([self.tools + "objdump", "-h", obj],
                                 capture_output=True, text=True).stdout
        sizes = {}
        for line in listing.splitlines():
            row = line.split()
            if len(row) > 2 and row[1].startswith(".text"):
                sizes[row[1]] = int(row[2], 16)
        return sizes

    def body(self, name):
        obj, section, off, size, _ = self.bodies[name]
        out = os.path.join(self.scratch, "body.bin")
        subprocess.run([self.tools + "objcopy", "-O", "binary", "--only-section",
                        section, obj, out], check=True)
        with open(out, "rb") as fh:
            return fh.read()[off:off + size], self.relocated(obj, section, off, size)

    def records(self, obj):
        """Every relocation the object holds, by section: (at, type, value)."""
        if obj not in self._relocs:
            listing = subprocess.run([self.tools + "objdump", "-r", obj],
                                     capture_output=True, text=True).stdout
            out, current = collections.defaultdict(list), None
            for line in listing.splitlines():
                if line.startswith("RELOCATION RECORDS FOR ["):
                    current = line.split("[")[1].split("]")[0]
                    continue
                row = line.split()
                if current is None or len(row) < 3:
                    continue
                try:
                    at = int(row[0], 16)
                except ValueError:
                    continue
                out[current].append((at, row[1], row[2]))
            self._relocs[obj] = out
        return self._relocs[obj]

    def relocated(self, obj, section, off, size):
        """The byte offsets inside the body that a relocation owns."""
        owned = set()
        for at, _, _ in self.records(obj)[section]:
            owned.update(range(at - off, at - off + 4))
        return owned

    def callees(self, name):
        """Where the body calls out, as byte offset -> (symbol, bind).

        A branch relocation is the only way one archive body names another
        before the link, so this is the callee list the image's own call graph
        has to agree with. A relocation against a section rather than a symbol
        is a call to a static in the same object, and the symbol at that
        section offset is the one it means.
        """
        obj, section, off, size, _ = self.bodies[name]
        out = {}
        for at, kind, value in self.records(obj)[section]:
            if kind not in BRANCH_RELOCS or not off <= at < off + size:
                continue
            sym, _, addend = value.partition("+")
            if sym.startswith("."):
                where = int(addend, 16) if addend else 0
                local = self.at[obj].get((sym, where))
                if not local:
                    out[at - off] = (value, "?")
                    continue
                out[at - off] = local[0]
                continue
            out[at - off] = (sym, self.bodies.get(sym, (None,) * 5)[4] or "g")
        return out

    def insns(self, name):
        """The body disassembled, as (byte offset, mnemonic)."""
        if name not in self._disasm:
            built, _ = self.body(name)
            raw = os.path.join(self.scratch, "insns.bin")
            with open(raw, "wb") as fh:
                fh.write(built)
            listing = subprocess.run(
                [self.tools + "objdump", "-D", "-b", "binary", "-m", "armv7e-m",
                 "-Mforce-thumb", raw], capture_output=True, text=True).stdout
            out = []
            for line in listing.splitlines():
                m = INSN.match(line)
                if m:
                    out.append((int(m.group(1), 16), m.group(2)))
            self._disasm[name] = out
        return self._disasm[name]


# A branch relocation is the only place an archive body names another body, and
# a call and a tail call are the same crossing: b.w into another section is how
# gcc ends a wrapper.
BRANCH_RELOCS = {"R_ARM_THM_CALL", "R_ARM_THM_JUMP24", "R_ARM_THM_JUMP19",
                 "R_ARM_THM_PC22", "R_ARM_CALL", "R_ARM_JUMP24", "R_ARM_PLT32"}
INSN = re.compile(r"^\s*([0-9a-f]+):\t[0-9a-f ]+\t(\S+)")
# GCC gives a specialised clone a name with a dot in it, which is not an
# identifier the rest of the repo can carry; the base name is what it is named.
CLONE = re.compile(r"\.(isra|constprop|part|cold)\.\d+$")
# The partition names a split function's tail after its head plus an index, and
# such a name is derived, not a claim about what the function is.
DERIVED = re.compile(r"__\d+$")
REPLACEABLE = ("exact", "masked")
# Under this many unrelocated bytes a body is a shape and not a function unless
# it does something of its own: the bytes left after the linker's fields are a
# prologue, a call and a return, and every lock/call/unlock wrapper in every
# library has them.
SMALL_BODY = 32
# Stack, control flow and register moves are what the shape is made of; an
# instruction outside this set is the body doing something of its own.
SHAPE_INSNS = {"push", "pop", "stmdb", "stmdb.w", "ldmia", "ldmia.w", "ldm",
               "stm", "bx", "blx", "bl", "b", "b.n", "b.w", "bxns", "nop",
               "it", "ite", "itt", "itte", "ittt", "mov", "mov.w", "movs",
               "cpy", "add", "add.w", "sub", "sub.w", "adds", "subs", "push.w",
               "pop.w"}


def base_name(sym):
    return CLONE.sub("", sym).split(".")[0]


class Image(object):
    """What the partition and its references say about the image's own code."""

    def __init__(self, export):
        items = json.load(open(os.path.join(export, "items.json")))
        refs = json.load(open(os.path.join(export, "references.json")))
        self.starts = set(f["start"] for f in items["functions"])
        self.fallthrough_into = set(f["to"] for f in refs["fallthrough"])
        self.branch_from = {}
        for c in refs["calls"]:
            if c["kind"] in ("call", "jump"):
                self.branch_from[c["from"]] = c["to"]


def name_index():
    """Every name the repo claims at an address.

    A callee agrees when the image function the call lands on is the twin of
    the archive's callee, and a name is one of the two ways to know that. The
    names that count are claims: the derived classes, the hand ABI, the kernel
    and syscall names the boundary and the replacements export, and the
    matcher. A partition spelling like `_scanf_i__2` is a split function's tail
    numbered after its head and claims nothing, so it is not one of them.
    """
    auto = yaml.safe_load(open(os.path.join(HERE, "autonames.yaml")))
    hand = yaml.safe_load(open(os.path.join(HERE, "hwa10.yaml")))
    matched = yaml.safe_load(open(os.path.join(HERE, "matches.yaml")))
    boundary = yaml.safe_load(open(os.path.join(HERE, "boundary.yaml")))
    repl = yaml.safe_load(open(os.path.join(HERE, "replacements.yaml")))
    names = collections.defaultdict(set)

    def claim(addr, name):
        if addr is None or not name or DERIVED.search(name) or \
                name.startswith("FUN_"):
            return
        names[addr].add(base_name(name))

    for f in auto["functions"]:
        claim(f["address"], f["name"])
    for f in matched.get("functions", []) or []:
        claim(f.get("address"), f.get("name") or f.get("symbol"))
    for f in hand.get("functions", []) or []:
        claim(f["address"], f["name"])
    for group in ("app_to_lib", "lib_to_app"):
        for e in boundary.get(group, []) or []:
            claim(e.get("addr"), e.get("symbol"))
    for g in repl.get("groups", []) or []:
        for what in ("exports", "replacements"):
            for e in g.get(what, []) or []:
                claim(e.get("at"), e.get("symbol"))
    return names


class Rules(object):
    """The three refusals a byte-identical body still has to survive.

    A masked comparison proves the bytes the compiler wrote are the same bytes.
    It does not prove the function is the same function: the linker owns every
    call displacement, so a Withings lock/operate/unlock wrapper and newlib's
    _tzset_r are the same ten unrelocated bytes and differ only in where their
    three branches go. These ask the call graph, the partition and the body's
    own instruction mix the questions the bytes cannot answer.
    """

    def __init__(self, archives, image, names, blob):
        self.archives, self.image, self.names, self.blob = (archives, image,
                                                            names, blob)
        self._twin = {}

    def archive_of(self, symbol):
        for ar in self.archives:
            if symbol in ar.bodies:
                return ar
        return None

    def entry(self, addr):
        """Where the partition says a name may and may not land.

        Two different refusals wear the same shape. An address the previous
        instruction falls through into is a second entry point of one section,
        which libgcc's soft-float objects are full of: the identity is right and
        only the replacement is impossible, because dropping the section takes
        the neighbour with it. An address the partition starts no function at is
        a name in the wrong place -- the matcher's __kernel_rem_pio2, cut at its
        25th byte -- and there the identity is what is wrong.
        """
        if addr in self.image.fallthrough_into:
            return "multi_entry", ("the instruction before 0x%x falls through"
                                   " into it, so its section has another entry"
                                   % addr)
        if addr not in self.image.starts:
            return "bad_entry", "the partition starts no function at 0x%x" % addr
        return None, None

    def spellings(self, symbol):
        """Every name one body answers to, over every archive on the line.

        libgcc exports its soft-float bodies twice, once under the EABI name and
        once under the gcc one, and the two are the same section offset: a
        callee named __muldf3 in the image is __aeabi_dmul in the archive and
        the call agrees. The union is over all the archives because the body
        being audited and its callee need not come from the same one.
        """
        out = set([base_name(symbol)])
        for ar in self.archives:
            out |= set(base_name(n) for n in ar.aliases.get(symbol, ()))
        return out

    def named(self, addr, symbol):
        """Is the image function at `addr` called `symbol` by something we trust?"""
        return bool(self.spellings(symbol) & self.names.get(addr, set()))

    def contradicted(self, addr, symbol):
        """Is the image function at `addr` called something else outright?"""
        return bool(self.names.get(addr)) and not self.named(addr, symbol)

    def bytes_are(self, symbol, addr):
        ar = self.archive_of(symbol)
        if ar is None:
            return False
        built, owned = ar.body(symbol)
        off = addr - APP_BASE
        there = self.blob[off:off + len(built)]
        return len(there) == len(built) and not any(
            built[i] != there[i] and i not in owned for i in range(len(built)))

    def twin(self, symbol, addr, seen=()):
        """Does the image at `addr` carry the archive's `symbol`? None if it does.

        The bytes, plus the calls the replacement would bring with it. A call to
        a symbol of the same object is part of the body being replaced -- drop
        the caller's section and the archive links its own callee beside it --
        so the image's target has to be that callee or the replacement is
        provably the wrong function. A call out of the object binds to whatever
        the link resolves, the blob's own copy included, and its identity is a
        separate question this does not answer.
        """
        key = (symbol, addr)
        if key in self._twin:
            return self._twin[key]
        if key in seen:
            # A cycle: two bodies that call each other are confirmed or refuted
            # together, and the outermost frame is where that is decided.
            return None
        ar = self.archive_of(symbol)
        if ar is None:
            return "the build defines no %s" % symbol
        if not self.bytes_are(symbol, addr):
            return "the bytes at 0x%x are not %s" % (addr, symbol)
        self._twin[key] = None
        why = None
        for at, callee, target, bind, same in self.calls(symbol, addr):
            if target is None:
                why = "+0x%x wants %s, the image branches nowhere" % (at, callee)
                break
            if bind == "l" or self.named(target, callee):
                continue
            if self.contradicted(target, callee):
                why = "+0x%x wants %s, 0x%x is %s" % (
                    at, callee, target,
                    ", ".join(sorted(self.names[target])))
                break
            if same and self.twin(callee, target, tuple(seen) + (key,)):
                why = "+0x%x wants %s, 0x%x is not it" % (at, callee, target)
                break
        self._twin[key] = why
        return why

    def calls(self, symbol, addr):
        """(offset, callee, image target, bind, same object) for every call."""
        ar = self.archive_of(symbol)
        obj = ar.bodies[symbol][0]
        out = []
        for at, (callee, bind) in sorted(ar.callees(symbol).items()):
            same = ar.bodies.get(callee, (None,))[0] == obj
            out.append((at, callee, self.image.branch_from.get(addr + at),
                        bind, same))
        return out

    def audit(self, symbol, addr):
        """The callees that refute the body, that contradict it, and that fail to confirm it.

        Three strengths, because a name and a replacement are not the same
        claim. A call to a symbol of the same object whose image target is not
        that symbol refutes the body outright: the archive brings that callee
        with it, so the pair is either the image's pair or the body is not the
        body. A call whose image target the repo already calls something else
        contradicts the replacement without touching the identity -- the image's
        __assert_func is newlib's __assert_func and calls nrf_fprintf where the
        archive calls fiprintf, so replacing it moves a call and naming it does
        not. A call out of the object onto an unnamed target that the archive
        does not reproduce is neither: it is the absence of evidence, and the
        image's __sinit is built from source the archive does not reproduce and
        is still __sinit.
        """
        if not self.bytes_are(symbol, addr):
            # The body the image carries is built from other source, so the
            # linker's fields are not where the archive put them and no branch
            # of the two is comparable. That the bytes differ is the verdict
            # already; the call graph has nothing to add to it.
            return [], [], []
        refuting, against, unsure = [], [], []
        for at, callee, target, bind, same in self.calls(symbol, addr):
            if target is None:
                refuting.append((at, callee, "the image branches nowhere"))
                continue
            if bind == "l" or self.named(target, callee):
                continue
            if self.contradicted(target, callee):
                against.append((at, callee, "0x%x is %s" % (
                    target, ", ".join(sorted(self.names[target])))))
                continue
            bad = self.twin(callee, target, ((symbol, addr),))
            if not bad:
                continue
            if same:
                refuting.append((at, callee, "0x%x is not it, %s" % (target, bad)))
            else:
                unsure.append((at, callee, "0x%x is unnamed and is not it" % target))
        return refuting, against, unsure

    def distinguishing(self, symbol):
        """Instructions the body has beyond a prologue, a branch and a return."""
        ar = self.archive_of(symbol)
        _, owned = ar.body(symbol)
        return [(off, m) for off, m in ar.insns(symbol)
                if off not in owned and m not in SHAPE_INSNS]


def apply_rules(rules, verdicts):
    """Demote every verdict the partition, the shape or the call graph refuses."""
    out = {}
    for addr, symbol, verdict, why in verdicts:
        if verdict not in REPLACEABLE:
            out[addr] = (verdict, why)
            continue
        kind, reason = rules.entry(addr)
        if kind:
            out[addr] = (kind, reason)
            continue
        ar = rules.archive_of(symbol)
        built, owned = ar.body(symbol)
        solid = len(built) - len(owned)
        refuting, against, unsure = rules.audit(symbol, addr)
        small = solid < SMALL_BODY
        if refuting or against:
            out[addr] = ("callees_differ",
                         "; ".join("+0x%x wants %s, %s" % r
                                   for r in refuting + against))
            continue
        if small and not rules.distinguishing(symbol):
            out[addr] = ("shape_only",
                         "%d unrelocated bytes and no instruction beyond the"
                         " prologue, the calls and the return" % solid)
            continue
        if unsure:
            why += "; unconfirmed callees: " + ", ".join(
                "+0x%x %s %s" % u for u in unsure)
        out[addr] = (verdict, why)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="newlib-nano-big",
                    help="the abi/refbuild.sh newlib build to check")
    ap.add_argument("--class", dest="cls", default="libc",
                    help="the abi/autonames.yaml class whose bodies to check;"
                         " libc is the C library and libgcc, libm the math one,"
                         " and they are separate because a link may take one"
                         " without the other")
    ap.add_argument("--root", default=os.path.expanduser("~/ref-build"))
    ap.add_argument("--scratch", default="/tmp/libc_check")
    ap.add_argument("--list", type=int, default=20)
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"),
                    help="the partition and reference export the callee and"
                         " entry rules read the image's call graph from")
    ap.add_argument("--emit", help="write the verdict for every body as YAML,"
                    " for abi/replacements.yaml's `newlib` group to derive from")
    args = ap.parse_args()

    tools = os.path.join(args.root, "tc",
                         "arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi",
                         "bin", "arm-none-eabi-")
    newlib = os.path.join(args.root, "build", args.build, "arm-none-eabi", "newlib")
    libgcc = os.path.join(args.root, "tc",
                          "arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi", "lib",
                          "gcc", "arm-none-eabi", "13.2.1", "thumb", "v7e-m+fp",
                          "hard", "libgcc.a")
    archive = Archive(tools, os.path.join(args.scratch, args.build))
    for path in (os.path.join(newlib, "libc.a"), os.path.join(newlib, "libm.a"),
                 libgcc):
        if not os.path.exists(path):
            sys.exit("%s does not exist; run abi/refbuild.sh" % path)
        archive.add(path)

    with open(os.path.join(SIM, "appl.bin"), "rb") as fh:
        image = fh.read()
    names = yaml.safe_load(open(os.path.join(HERE, "autonames.yaml")))

    # GCC clones a function it specialises and gives the clone a name with a dot
    # in it, which is not an identifier the rest of the repo can carry, so the
    # name in abi/autonames.yaml is the base one. The body is still the clone's.
    clones = {}
    for sym in archive.bodies:
        base = sym.split(".")[0]
        if base != sym and base not in archive.bodies:
            clones.setdefault(base, sym)

    # Both classes are measured whichever one is asked for: the callee rule is a
    # question about the image's call graph, and a libm body calls libc bodies,
    # so a verdict decided from the libc half alone would be decided from half
    # the graph.
    every = sorted((f["address"], f["name"], f["class"]) for f in names["functions"]
                   if f["class"] in ("libc", "libm"))
    verdicts, byte_kind = [], {}
    for addr, name, cls in every:
        name = clones.get(name, name)
        if name not in archive.bodies:
            verdicts.append((addr, name, "absent", "no symbol in the build", cls))
            continue
        built, owned = archive.body(name)
        there = image[addr - APP_BASE:addr - APP_BASE + len(built)]
        if archive.bodies[name][4] == "l":
            # A static: the body is there and may well be the image's, but the
            # name is the source file's private one and no link can bind to it.
            verdicts.append((addr, name, "local",
                             "the archive defines it local to its object", cls))
            continue
        if built == there:
            verdicts.append((addr, name, "exact", "%d bytes" % len(built), cls))
            continue
        bad = [i for i in range(len(built))
               if built[i] != there[i] and i not in owned]
        if bad:
            verdicts.append((addr, name, "differs",
                             "%d of %d bytes outside the relocated fields"
                             % (len(bad), len(built)), cls))
        else:
            verdicts.append((addr, name, "masked",
                             "%d bytes, the relocated fields masked"
                             % len(built), cls))
    for addr, name, verdict, _, _ in verdicts:
        byte_kind[addr] = verdict

    known = name_index()
    rules = Rules([archive], Image(args.export), known, image)
    settled = apply_rules(rules, [v[:4] for v in verdicts])

    kinds = collections.Counter()
    demoted, mine = [], []
    for addr, name, _, _, cls in verdicts:
        verdict, why = settled[addr]
        if byte_kind[addr] in REPLACEABLE and verdict not in REPLACEABLE:
            demoted.append((addr, name, cls, verdict, why))
        if cls != args.cls:
            continue
        mine.append((addr, name, verdict, why))
        kinds[verdict] += 1

    if args.emit:
        os.makedirs(os.path.dirname(os.path.abspath(args.emit)), exist_ok=True)
        with open(args.emit, "w") as fh:
            fh.write("# Generated by abi/libc_check.py, do not edit: whether a\n"
                     "# source build reproduces each libc body the image carries,\n"
                     "# and whether the body it reproduces is the same function.\n")
            fh.write("build: %s\nclass: %s\n" % (args.build, args.cls))
            fh.write("bodies:\n")
            for addr, name, verdict, why in mine:
                fh.write("  - address: 0x%x\n    symbol: %s\n    verdict: %s\n"
                         "    why: %s\n" % (addr, name, verdict, json.dumps(why)))

    print("%s against appl.bin: %d bodies abi/autonames.yaml classes %s"
          % (args.build, len(mine), args.cls))
    print("  %d reproduce the image's bytes exactly" % kinds["exact"])
    print("  %d reproduce them once the relocated fields are masked"
          % kinds["masked"])
    for kind, what in (("differs", "still differ"),
                       ("absent", "have no symbol in the build"),
                       ("local", "are static in the build, so nothing can link"
                                 " against them"),
                       ("callees_differ", "reproduce the bytes and call"
                                          " somewhere else"),
                       ("bad_entry", "sit where the partition starts no"
                                     " function"),
                       ("multi_entry", "are a second entry point of a section"
                                       " that has another"),
                       ("shape_only", "are a shape short enough that every"
                                      " wrapper shares it")):
        if kinds[kind]:
            print("  %d %s" % (kinds[kind], what))
            for addr, name, verdict, why in mine:
                if verdict == kind:
                    print("      %-24s 0x%05x  %s" % (name, addr, why))
    if demoted:
        print("  %d names the bytes accepted and the call graph, the partition"
              " or the shape refused:" % len(demoted))
        for addr, name, cls, verdict, why in demoted:
            print("    %-24s 0x%05x %-5s %-14s %s"
                  % (name, addr, cls, verdict, why))
    # --emit is the generator, and a body that differs is one of its answers;
    # only the standalone check treats it as a failure.
    return 1 if kinds["differs"] and not args.emit else 0


if __name__ == "__main__":
    sys.exit(main())
