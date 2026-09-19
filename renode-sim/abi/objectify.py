"""Turn the partition export into sections, symbols and relocations.

abi/blobify.py is the entry point; this module holds the parts of the job that
are about the image rather than about the ELF container: cutting the app into
one section per function and per data item, naming them, and deciding which
reference at which site becomes which relocation.

The partition (abi/out/ghidra/items.json) tiles 0x27000..0xf117c exactly, so a
section per tile-run is a cover by construction and a link that pins every
section at its original address reproduces the image byte for byte. That is the
proof this step rests on: nothing is moved, so any difference is a bug in the
cutting.
"""

import bisect
import collections
import json
import os

APP_BASE = 0x27000
APP_END = 0xF117C

R_ARM_ABS32 = 2
R_ARM_THM_CALL = 10
R_ARM_THM_JUMP24 = 30
R_ARM_THM_JUMP19 = 51

# The displacement a REL addend of 0 has to encode: the branch to `.-4`.
SELF_BL = (0xF7FF, 0xFFFE)
SELF_B = (0xF7FF, 0xBFFE)

COND_CODES = {"eq": 0, "ne": 1, "cs": 2, "hs": 2, "cc": 3, "lo": 3, "mi": 4,
              "pl": 5, "vs": 6, "vc": 7, "hi": 8, "ls": 9, "ge": 10, "lt": 11,
              "gt": 12, "le": 13}

# Instructions whose target is in a register or in a word, so the instruction
# itself holds no address to relocate: the word is step 3's work, and a return
# has no target at all. Everything else that crosses a section either takes a
# relocation below or is a partition error.
INDIRECT = ("ldr", "ldr.w", "vldr", "bx", "pop")

# How far a Thumb-2 pc-relative load reaches, and so how far a literal pool can
# sit from the instruction that reads it.
LDR_RANGE = 4096


def self_bcond(cond):
    """`b<cond>.w .-4`, the T3 encoding of a displacement of -4."""
    return (0xF000 | (1 << 10) | (cond << 6) | 0x3F,
            0x8000 | (1 << 13) | (1 << 11) | 0x7FE)


class Section(object):
    """One output section: a run of tiles with the same owner."""

    def __init__(self, start, end, kind, sym):
        self.start, self.end, self.kind, self.sym = start, end, kind, sym
        self.name = ("%s.%s" % (".text" if kind == "code" else ".rodata", sym))
        # Every other name the object gives the section's first byte: the
        # address alias of a RAM item is its RAM address, while the section's
        # own `start` is where its initialiser sits in flash.
        self.aliases = []
        # Where the section's bytes live at run time, when that is not where
        # they are in the image. A `.data` item is the only thing with two
        # addresses: `start` is its initialiser in flash, which is where its
        # words are and where the link loads it from, and `vma` is the RAM the
        # startup copies it to, which is what a pointer to it holds.
        self.vma = None
        # A `.bss` item has no bytes at all: the section reserves RAM and the
        # startup's zero fill is what puts the zeroes there.
        self.nobits = False
        self.relocs = []            # (offset, symbol, type)
        self.labels = {}            # (address, is_function) -> symbol, interior targets
        self.functions = []         # the entry points the section holds
        self.index = 0
        # Which object file the section is linked from. The data sections the
        # relink delegates to abi/datagen.py are not in the blob object, and the
        # placement has to name the object each one really comes from.
        self.object = None

    def __contains__(self, addr):
        return self.start <= addr < self.end


MIN_STRING = 2


def string_runs(blob, instruction_bytes, stops=()):
    """Every run of non-NUL bytes the disassembly does not cover.

    A string is delimited by NULs, so that is what the partition has to keep
    whole, and not the printable part of it: this image logs with ANSI escapes,
    so "\x1b[31mNot charging\x1b[m" is one object whose first byte is not
    printable and whose pointer names that byte. Bytes an instruction covers end
    a run and disqualify it, which is what keeps this away from code that
    happens to read as text.

    `stops` are the bounds of the tables the manifest declares, and a run is cut
    at every one of them inside it rather than ended there, because the bytes on
    both sides are still whatever they were. The string at 0xbe2e7 runs two
    bytes into ble_conn_params_table, whose first record's two u16 fields happen
    to be non-zero, and without the cut the declaration and the string are one
    section. Only a declaration is a stop: a pointer into the middle of a run is
    the shared string tail this rule exists to keep whole.
    """
    stops = sorted(stops)
    runs, i = [], 0
    while i < len(blob):
        if blob[i] == 0 or instruction_bytes[i]:
            i += 1
            continue
        j = i
        while j < len(blob) and blob[j] != 0 and not instruction_bytes[j]:
            j += 1
        # The NUL belongs to the object: a pointer to the run is a pointer to
        # everything up to and including its terminator.
        end = j + 1 if j < len(blob) and blob[j] == 0 else j
        cuts = [i]
        at = bisect.bisect_right(stops, APP_BASE + i)
        while at < len(stops) and stops[at] < APP_BASE + end:
            cuts.append(stops[at] - APP_BASE)
            at += 1
        cuts.append(end)
        for lo, hi in zip(cuts, cuts[1:]):
            if hi - lo >= MIN_STRING:
                runs.append((APP_BASE + lo, APP_BASE + hi))
        i = j + 1
    return runs


def unique(names, reserved, sym, start):
    """Export names repeat (`caseD_2` once per switch); the address is the key.

    A name the boundary needs at a particular address is reserved there, because
    the export hands the same name to two functions often enough that whichever
    comes first would otherwise take it.
    """
    # Ghidra builds a string's name out of its content, which puts characters
    # the linker script grammar does not accept into a section name.
    sym = "".join(c if c.isalnum() or c in "_." else "_" for c in sym)
    if sym in names or reserved.get(sym, start) != start:
        sym = "%s__%x" % (sym, start)
    names.add(sym)
    return sym


def read_export(directory):
    with open(os.path.join(directory, "items.json")) as fh:
        items = json.load(fh)
    with open(os.path.join(directory, "references.json")) as fh:
        refs = json.load(fh)
    return items, refs


def tiles_of(items):
    """Every tile of the cover as (start, end, owner, kind).

    `owner` is the function entry point for code and for the literal pools and
    jump tables the export attributes to a function, and None for everything
    that stands on its own.
    """
    tiles = []
    for r in items["function_ranges"]:
        tiles.append((r["start"], r["end"], r["function"], "code"))
    for r in items["inline"]:
        tiles.append((r["start"], r["end"], r["function"] or None, "pool"))
    for r in items["data"]:
        tiles.append((r["start"], r["end"], None, "data", r["name"], r["class"]))
    for r in items["gaps"]:
        tiles.append((r["start"], r["end"], None, "gap"))
    tiles.sort()
    covered, at = [], APP_BASE
    for t in tiles:
        if t[0] < at:
            raise SystemExit("partition claims 0x%x..0x%x twice" % (t[0], at))
        if t[0] > at:
            # Ghidra hands back the odd body range whose head byte belongs to no
            # code unit; the bytes are still the image's, so they get a section
            # of their own and the caller reports them.
            covered.append((at, t[0], None, "sliver"))
        covered.append(t)
        at = t[1]
    if at != APP_END:
        raise SystemExit("partition ends at 0x%x, not 0x%x" % (at, APP_END))
    return covered


def split_named(tiles, named):
    """Cut every data tile at the interior points something names.

    The partition's tile boundaries follow what the analysis could type, so an
    object's head and the object after it land in one tile whenever the bytes
    between them are a gap. Joining such a tile to the one above it (which is
    what a run of data is) then puts both objects in one section, and the
    section lives or dies on the head's references alone. A point something
    names is an object's start, so the tile ends there.

    Code and pool tiles are left whole: a pc-relative distance inside them is
    not expressible as a relocation, and they are joined back together below in
    any case.
    """
    points = sorted(named)
    out = []
    for tile in tiles:
        if tile[3] not in ("data", "gap", "sliver"):
            out.append(tile)
            continue
        lo = bisect.bisect_right(points, tile[0])
        hi = bisect.bisect_left(points, tile[1])
        at = tile[0]
        for cut in points[lo:hi]:
            piece = list(tile)
            piece[0], piece[1] = at, cut
            out.append(tuple(piece))
            at = cut
        if at != tile[0]:
            piece = list(tile)
            piece[0] = at
            # The analysis named the head of the tile, not this remainder.
            if len(piece) > 4:
                piece[4] = ""
            out.append(tuple(piece))
        elif at == tile[0] and lo == hi:
            out.append(tile)
    return out


class Union(object):
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, i, j):
        a, b = self.find(i), self.find(j)
        self.parent[max(a, b)] = min(a, b)


def build_sections(items, calls, reads, falls, pointer_words, strings, reserved,
                   named=()):
    """Cut the cover into sections and name each one.

    Two tiles have to share a section when the distance between them is part of
    an instruction: a 2-byte branch reaches +-2 KB and carries no relocation, a
    `tbb`/`tbh` offset is measured from the table, and a literal pool is read
    pc-relative. Those are the edges; a section is a connected component of them,
    widened to a whole span so that what lies between two joined tiles comes
    along. Everything else -- a data item, a gap, a function nothing short
    branches into -- is a section of its own.

    The components that hold more than one function come back to the caller:
    Ghidra attributes interleaved code to whichever entry reaches it, and in
    step 3 each such component moves as a unit. So do the pools that sit further
    from the function the export names as their reader than a load can reach,
    which means the real reader is still unknown.
    """
    functions = {f["start"]: f for f in items["functions"]}
    named = set(named)
    tiles = split_named(tiles_of(items), named)
    starts = [t[0] for t in tiles]

    def tile_of(addr):
        lo, hi = 0, len(starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid] <= addr:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1

    read_targets = set(t for _, t in reads)
    union, distant_pools = Union(len(tiles)), []
    # A data tile nothing names is not an object: the partition cuts a record
    # table into one item per field, and only the head of such a run is ever the
    # target of a word, so the fields after it are reachable only by walking
    # from the head. Cut apart they survive a link that places everything, and
    # disappear under --gc-sections, which is how a live table loses every row
    # but its first. They belong to the tile above them, which is where the
    # walk that reads them starts.
    for i in range(1, len(tiles)):
        if tiles[i][3] == "code" or tiles[i - 1][3] == "code":
            continue
        if tiles[i][0] in named:
            continue
        union.join(i - 1, i)
    for row in calls:
        if row["external"] or is_relocatable(base_mnemonic(row["mnemonic"]),
                                             row["width"]):
            continue
        if base_mnemonic(row["mnemonic"]) in INDIRECT or row["mnemonic"] == "blx":
            continue
        union.join(tile_of(row["from"]), tile_of(row["to"]))
    owner_tiles = {}
    for i, tile in enumerate(tiles):
        if tile[2] is not None and tile[3] == "code":
            owner_tiles.setdefault(tile[2], []).append(i)
    # A pc-relative load carries no relocation, so the word and every
    # instruction that reads it have to end up at the same distance from each
    # other as they are now, which means one section. The export names each
    # reading instruction; a pool read from two functions binds both.
    # A NUL-delimited run is one object however the partition cut it. This
    # image shares string tails -- "BEFORE_PREDICTED_OVULATION" and "PREDICTED_OVULATION" are
    # one run of bytes with a pointer into each -- so the analysis types the
    # suffix and leaves the head to whatever claims it, and a section boundary
    # inside the run puts the head and the tail in different places.
    for start, end in strings:
        union.join(tile_of(start), tile_of(end - 1))
    # Falling off the end of one item into the next: nothing encodes the
    # distance, so the only way for it to survive a move is one section.
    for row in falls:
        union.join(tile_of(row["from"]), tile_of(row["to"]))
    # A word that holds an address is one slot, so the four bytes cannot be
    # split between two sections: the linker would write the relocation into the
    # first and then copy the second over its tail.
    for addr in pointer_words:
        union.join(tile_of(addr), tile_of(addr + 3))
    for site, target in reads:
        if abs(site - target) >= LDR_RANGE:
            distant_pools.append(target)
            continue
        union.join(tile_of(site), tile_of(target))
    for i, tile in enumerate(tiles):
        if tile[3] != "pool" or tile[2] is None or tile[0] in read_targets:
            continue
        # A jump table, or a pool the export attributes to a function without
        # naming the instruction: the owning function's range closest to it is
        # the only candidate, and only if a load could reach that far.
        near = min(owner_tiles[tile[2]], key=lambda j: abs(tiles[j][0] - tile[0]))
        if abs(tiles[near][0] - tile[0]) < LDR_RANGE:
            union.join(i, near)
        else:
            distant_pools.append(tile[0])

    while True:
        spans, has_code = {}, set()
        for i, tile in enumerate(tiles):
            root = union.find(i)
            lo, hi = spans.get(root, (tile[0], tile[1]))
            spans[root] = (min(lo, tile[0]), max(hi, tile[1]))
            if tile[3] == "code":
                has_code.add(root)
        order = sorted(spans.items(), key=lambda kv: kv[1])
        merged = False
        for (a, (alo, ahi)), (b, (blo, bhi)) in zip(order, order[1:]):
            if blo < ahi:
                union.join(a, b)
                merged = True
        # An interior symbol's value is its offset from the section start with
        # bit 0 set for Thumb, so a code section that starts at an odd address
        # would have that bit already spent on the base. Swallowing the byte
        # before it is the only way to keep both.
        for root, (lo, hi) in order:
            if lo % 2 and root in has_code:
                union.join(root, tile_of(lo - 1))
                merged = True
        if not merged:
            break

    names, sections, current = set(), [], None
    for i, tile in enumerate(tiles):
        start, end, owner, kind = tile[:4]
        root = union.find(i)
        lo, hi = spans[root]
        if lo != start or hi != end:
            if current is not None and current.end >= end:
                continue                # inside the component already opened
            # A function belongs to the section its entry point is in; a body
            # range of some other function reaching into this one does not make
            # it a member, or its name would land on the wrong bytes.
            owners = sorted(o for o in owner_tiles if lo <= o < hi)
            # The name belongs to whatever starts at the first byte; a component
            # that opens on data or on the tail of a function gets its address,
            # and every function in it gets a symbol where it really starts.
            sym = functions[lo]["name"] if lo in functions else "block_%08x" % lo
            section = Section(lo, hi, "code" if owners else "data",
                              unique(names, reserved, sym, lo))
            section.functions = owners
            for entry in owners:
                if entry != lo:
                    section.labels[(entry, True)] = unique(
                        names, reserved, functions[entry]["name"], entry)
            sections.append(section)
            current = sections[-1]
            continue
        if kind == "code":
            sections.append(Section(start, end, "code",
                                    unique(names, reserved, functions[owner]["name"], start)))
            sections[-1].functions = [owner]
        else:
            if kind == "data":
                sym = tile[4] or "DAT_%08x" % start
            elif kind in ("gap", "sliver"):
                sym = "%s_%08x" % (kind, start)
            else:
                sym = "pool_%08x" % start   # a pool the export gives no reader
            sections.append(Section(start, end, "data", unique(names, reserved, sym, start)))
        current = sections[-1]

    covered = APP_BASE
    for s in sections:
        if s.start != covered:
            raise SystemExit("sections do not tile: 0x%x after 0x%x" % (s.start, covered))
        covered = s.end
    if covered != APP_END:
        raise SystemExit("sections end at 0x%x, not 0x%x" % (covered, APP_END))

    shared = [s for s in sections if len(s.functions) > 1]
    slivers = [s for s in sections if s.sym.startswith("sliver_")]
    return sections, shared, slivers, distant_pools


class Layout(object):
    """The sections in address order, with the lookup the relocation pass needs."""

    def __init__(self, sections):
        self.sections = sections
        self.starts = [s.start for s in sections]
        for i, s in enumerate(sections):
            s.index = i

    def at(self, addr):
        lo, hi = 0, len(self.starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.starts[mid] <= addr:
                lo = mid + 1
            else:
                hi = mid
        if not lo:
            return None
        s = self.sections[lo - 1]
        return s if addr in s else None

    def label(self, addr, thumb=None):
        """The symbol naming `addr`, creating an interior one if it is not a start.

        `thumb` overrides what the section's kind would say: a word inside a
        function body is data in a code section, and a symbol that carries the
        Thumb bit would relocate one byte past it.
        """
        s = self.at(addr)
        if s is None:
            raise SystemExit("0x%x is outside the cover" % addr)
        is_func = (s.kind == "code") if thumb is None else thumb
        if s.start == addr and is_func == (s.kind == "code"):
            return s.sym
        # The same address can be named twice, once as code and once as the
        # data the compiler left in the middle of it; Thumb-ness is part of the
        # symbol, so the two cannot share one.
        name = "%s_%08x" % ("L" if is_func else "D", addr)
        return s.labels.setdefault((addr, is_func), name)


def base_mnemonic(mnemonic):
    """Ghidra renders an instruction inside an IT block as `bl.eq`; the encoding
    is the unpredicated one, so the predicate is not part of the decision."""
    head, _, tail = mnemonic.rpartition(".")
    return head if head and tail in COND_CODES else mnemonic


def decode_call(blob, addr, mnemonic):
    """The relocation a 4-byte branch at `addr` takes, and the bytes it becomes.

    Verifying the encoding before blanking it is what makes the rewrite safe:
    the export says what the instruction is, and the bytes have to agree.
    """
    off = addr - APP_BASE
    hi = blob[off] | (blob[off + 1] << 8)
    lo = blob[off + 2] | (blob[off + 3] << 8)
    if (hi & 0xF800) != 0xF000:
        return None
    if mnemonic == "bl":
        return (R_ARM_THM_CALL, SELF_BL) if (lo & 0xD000) == 0xD000 else None
    if mnemonic == "b.w":
        return (R_ARM_THM_JUMP24, SELF_B) if (lo & 0xD000) == 0x9000 else None
    cond = COND_CODES.get(mnemonic[1:-2]) if mnemonic.endswith(".w") else None
    if cond is None or (lo & 0xD000) != 0x8000 or ((hi >> 6) & 0xF) != cond:
        return None
    return R_ARM_THM_JUMP19, self_bcond(cond)


def is_relocatable(mnemonic, width):
    return width == 4 and (mnemonic == "bl" or mnemonic == "b.w"
                           or (mnemonic.endswith(".w") and mnemonic[1:-2] in COND_CODES))


def internal_relocations(blob, layout, calls, skip, retarget=None):
    """Every internal control transfer that crosses a section, as a relocation.

    Sites the library boundary owns are skipped: boundary.yaml decides those,
    and its symbols resolve to the source build rather than to the blob's own
    copy.

    `retarget` maps the start address of a replaced section to the symbol its
    callers must bind to instead (abi/replacements.yaml). It is the same move
    the boundary makes for a library call, expressed against the partition
    rather than against a scan for branch encodings.
    """
    retarget = retarget or {}
    counts = {R_ARM_THM_CALL: 0, R_ARM_THM_JUMP24: 0, R_ARM_THM_JUMP19: 0}
    unrelocatable, indirect = [], 0
    for row in calls:
        if row["external"] or row["from"] in skip:
            continue
        src, dst = row["from"], row["to"]
        mnemonic = base_mnemonic(row["mnemonic"])
        from_section, to_section = layout.at(src), layout.at(dst)
        if from_section is None or to_section is None:
            raise SystemExit("reference 0x%x -> 0x%x leaves the cover" % (src, dst))
        if from_section is to_section:
            continue
        # `blx` is the register form at 2 bytes; the 4-byte form branches to an
        # ARM target, which this image has none of, so it is not written off.
        if mnemonic in INDIRECT or (mnemonic == "blx" and row["width"] == 2):
            indirect += 1
            continue
        if not is_relocatable(mnemonic, row["width"]):
            unrelocatable.append(row)
            continue
        decoded = decode_call(blob, src, mnemonic)
        if decoded is None:
            raise SystemExit("0x%x does not decode as the %s the export claims"
                             % (src, mnemonic))
        rtype, encoding = decoded
        off = src - APP_BASE
        blob[off], blob[off + 1] = encoding[0] & 0xFF, encoding[0] >> 8
        blob[off + 2], blob[off + 3] = encoding[1] & 0xFF, encoding[1] >> 8
        # A branch target is code whatever the partition made of the bytes:
        # a `b.w` into a run Ghidra never disassembled still lands in Thumb
        # state, and a symbol without bit 0 makes the linker plant a veneer.
        from_section.relocs.append((src - from_section.start,
                                    retarget.get(dst) or layout.label(dst, thumb=True),
                                    rtype))
        counts[rtype] += 1
    return counts, unrelocatable, indirect


def word_relocations(blob, layout, words, skip, retarget=None):
    """Every word the classification calls a pointer, as an R_ARM_ABS32.

    The symbol names the exact target rather than the enclosing item, so the
    addend is always zero and the word is blanked: an interior pointer is then a
    label in the item's section, which is the same thing the linker would
    compute from symbol-plus-addend and leaves nothing for a sign or an
    alignment to go wrong in. Bit 0 of a function symbol is what carries
    Thumb-ness, so a word that names data inside a code section has to take a
    symbol that is not one.

    Words the boundary already relocated are skipped: the vector table's kernel
    entries resolve to the source build, not to the blob's own copy.
    """
    retarget = retarget or {}
    counts = {"pointer": 0, "into_code": 0, "ram": 0}
    for row in words:
        if row["class"] != "pointer" or row["addr"] in skip:
            continue
        section = layout.at(row["addr"])
        if section is None or row["addr"] + 4 > section.end:
            raise SystemExit("word 0x%x is not inside one section" % row["addr"])
        sym = (retarget.get(row["target"])
               or layout.label(row["target"], thumb=row["thumb_target"]))
        off = row["addr"] - APP_BASE
        blob[off:off + 4] = b"\0\0\0\0"
        section.relocs.append((row["addr"] - section.start, sym, R_ARM_ABS32))
        counts["pointer"] += 1
        counts["into_code"] += 1 if row["in_code"] else 0
    counts["ram"] = sum(1 for r in words if r["signal"] == "ram")
    return counts


def global_relocations(blob, layout, words, skip, globals_):
    """Every pool word naming a RAM global, as an R_ARM_ABS32 onto its definition.

    A RAM address is a constant to the classification -- nothing in flash moves
    when it changes -- which is right until the object that lives there is the
    one the source build defines. Then the address in the word is the blob's
    copy and the definition is somewhere else, and the two have to be the same
    object or the app and the library each keep half the state. The word is
    blanked and bound to the symbol, which is undefined in the object, so the
    link decides where the object really is.

    A word is matched on the whole address and not on a range: an offset into
    the object would need an addend the classification has not established, and
    a global whose interior is named is refused rather than guessed at.
    """
    by_value = {g["at"]: g["as"] for g in globals_}
    counts = collections.Counter()
    for row in words:
        if row["addr"] in skip or row["value"] not in by_value:
            continue
        section = layout.at(row["addr"])
        if section is None or row["addr"] + 4 > section.end:
            raise SystemExit("word 0x%x is not inside one section" % row["addr"])
        sym = by_value[row["value"]]
        off = row["addr"] - APP_BASE
        blob[off:off + 4] = b"\0\0\0\0"
        section.relocs.append((row["addr"] - section.start, sym, R_ARM_ABS32))
        skip.add(row["addr"])
        counts[sym] += 1
    missing = [g["as"] for g in globals_ if not counts[g["as"]]]
    if missing:
        raise SystemExit("no word names %s, so the global is not the one the image"
                         " uses" % ", ".join(missing))
    return counts


SPARE_BASE, SPARE_END = 0xF1180, 0xFC000


def residue(section):
    """The address modulo 4 a section has to keep.

    A literal pool is read with a pc-relative load that rounds the program
    counter down to a word, and a `tbb`/`tbh` table is indexed from the
    instruction, so every offset inside a section has to keep its alignment as
    well as its distance. Preserving the section's own address modulo 4 is what
    makes that true for everything inside it at once, and it costs at most three
    bytes per section.

    A data section has no pc-relative reader, so that argument does not reach
    it; the rule stands anyway because the export records where each item starts
    and how long it is and nothing about what alignment the item needs. A word
    array read with `ldr` and a record table indexed by a scaled offset both
    require their own alignment and neither declares it, so the only alignment
    that can be preserved is the one the image already had.
    """
    return section.start % 4


class Unit(object):
    """Sections that have to move together, as one thing the layout places.

    An item boundary is not an object boundary. The partition cuts data at every
    address something names, and a struct whose fields are each named by their
    own pool word comes out as one item per field: the SPI device descriptor at
    0xb2404 is eight words and eight items, and the driver reads its name at
    +0x10 off the one pointer it is given. The RAM initialiser image at 0xefe58
    is the same shape the other way round -- 79 items, one memcpy, a length
    nothing in flash names. Neither is visible as an extent anywhere in the
    export, so the only safe reading of a run of adjacent data sections is that
    it is one object, and a data unit is a maximal run of them.

    That is conservative and it is what the evidence supports: naming a section
    start says something reaches it by name, never that nothing reaches it by
    offset from below. Relocations are unaffected either way -- each section
    keeps its own symbol and its own relocations, and the unit only decides that
    their addresses move together.

    Code is one section per unit. A function's entry points are all named, and a
    fall-through between two of them is exported as a reference and relocated,
    so there is nothing holding a code section to its neighbour.
    """

    def __init__(self, sections):
        self.sections = sections
        self.start, self.end = sections[0].start, sections[-1].end
        self.sym = sections[0].sym

    def place(self, at, moves):
        for s in self.sections:
            moves[s.sym] = at + (s.start - self.start)


def units(sections, cuts=()):
    """`sections` grouped into the blocks a layout may move, in address order.

    Adjacency is what joins: two data sections with a code section between them
    are not one run, and a gap the partition left is not something anything
    indexes across.

    `cuts` are the addresses an object is known to end at, which is the only
    thing that can shorten a run: a memcpy whose length the classification
    recovered says where the image it copies stops, and the bytes after it are
    not part of it however adjacent they are. The version trailer is the case
    this exists for -- the RAM initialiser at 0xefe58 ends at 0xf113c and the
    build string and the version word start there, so without the cut the
    trailer is held by a copy it is merely next to.
    """
    cuts = set(cuts)
    out = []
    for s in sorted(sections, key=lambda s: s.start):
        joins = (out and s.kind == "data" and out[-1][-1].kind == "data"
                 and out[-1][-1].end == s.start and s.start not in cuts)
        if joins:
            out[-1].append(s)
        else:
            out.append([s])
    return [Unit(g) for g in out]


def first_fit(order, free):
    """Place each unit in the lowest free interval it fits, or fail.

    `free` is consumed: each interval's start is the cursor. A unit keeps its
    address modulo 4, because a literal pool is read with a pc-relative load
    that rounds the program counter down to a word.
    """
    free = [list(i) for i in free]
    moves = {}
    for u in order:
        size, want = u.end - u.start, residue(u)
        for interval in free:
            at = interval[0] + (want - interval[0]) % 4
            if at + size <= interval[1]:
                u.place(at, moves)
                interval[0] = at + size
                break
        else:
            return None
    return moves


def pack(movable, free):
    """Pack the image down and leave the space it saved as one contiguous hole.

    The saving is real but it is scattered: the movable units are cut into runs
    by the held ones between them, so packing each run leaves its own small
    tail. A hole a library can be linked into has to be asked for instead of
    hoped for, so the biggest run's tail is reserved before anything is placed
    and the rest is packed into what is left; the reservation is the largest one
    a first fit still fits, and what bounds it is the alignment each unit's own
    address modulo 4 costs. With the data movable as well the runs are far
    longer -- the data between two stretches of text used to end both of them --
    so the hole is the image's own slack rather than one run's tail.
    """
    order = sorted(movable, key=lambda u: u.start)
    host = max(range(len(free)), key=lambda i: free[i][1] - free[i][0])
    if first_fit(order, free) is None:
        raise SystemExit("the image does not fit in its own space")

    def attempt(want):
        trimmed = [list(i) for i in free]
        trimmed[host][1] -= want
        return (None if trimmed[host][1] < trimmed[host][0]
                else first_fit(order, trimmed))

    low, high = 0, free[host][1] - free[host][0]
    while low < high:
        mid = (low + high + 1) // 2
        if attempt(mid) is None:
            high = mid - 1
        else:
            low = mid
    moves = attempt(low) if low else first_fit(order, free)
    return moves, (free[host][1] - low, free[host][1])


def relayout(sections, mode, pinned, anchors, cuts=(), dead=(), data=False):
    """Give every section a new address, and prove none keeps its old one.

    `cuts` are handed to `units`: the addresses an object is known to end at.

    `data` says the app's data is linked from the source abi/datagen.py writes
    (blobify's --data-source, DATA=1 on the scripts), which is the condition for
    moving it: the data half is only known to be independently linkable where
    the byte-identical identity link with it in has been run, and a layout that
    moved it without that proof would be testing the cut and the move at once.
    Without it the data stays where it is and the space it covers is not free.

    With it, data moves under the text's rules plus one of its own. A unit keeps
    its address modulo 4 (see `residue`), a unit a review word might name is
    held whatever its kind, and only the fixed points are pinned. Everything the
    export found inside a data section -- an interior label, the tail a second
    string pointer names, a typed table's rows -- is a label or an initialiser
    of that section and moves with it, because it is the section's own offset
    and not an address of its own. The rule of its own is `units`: what the
    layout places is a run of sections nothing names the interior of, because
    the partition cuts data finer than the code reads it.

    The space a layout may use is the space its movable units cover, plus the
    free flash after the image for whatever alignment costs. The two
    stale-address modes are the two ways of being sure an old address cannot
    survive by luck: `shift` keeps the order and starts one word further in, so
    every unit slides; `reverse` puts the last unit first, so nothing is near
    where it was.

    `pack` is the third, and it is not a stale-address test: it closes the image
    up. `dead` is the sections a replacement has already made unreferenced --
    their names are undefined in the object and every call to them binds to the
    source's symbol -- so their bytes are free whatever --gc-sections decides,
    and packing the rest over them leaves one contiguous hole at the top. That
    hole is flash the library can be linked into, which is the only way it grows:
    the library region runs from the end of the app image to the bootloader and
    there is nothing above it to take. A section the link would drop out of the
    middle of a unit is not dropped: the sections above it in the unit are
    reached by offset from its head, and taking bytes out of the run would move
    them. The set that really is dropped comes back with the moves.
    """
    # A section a word points into that the classification could not decide is
    # not moved: the word is either a pointer that would be left stale or a
    # constant that must not be rewritten, and there is no way to be right about
    # both while the target moves. The other anchor is a pc-relative
    # displacement, whose two ends have to keep their distance.
    held = {}
    for s in sections:
        for v, why in anchors.items():
            if s.start <= v < s.end:
                held.setdefault(s.start, why)
    if not data:
        for s in sections:
            if s.kind == "data" and s.start not in pinned:
                held.setdefault(s.start, "data source that is switched off")
    dead = set(dead)
    kinds = ("code", "data") if data else ("code",)
    all_units = units(sections, cuts)
    dead = set(u.sections[0].start for u in all_units
               if len(u.sections) == 1 and u.sections[0].start in dead)
    stuck = pinned | set(held)
    movable = [u for u in all_units
               if all(s.kind in kinds for s in u.sections)
               and not any(s.start in stuck or s.start in dead for s in u.sections)]
    held = dict(held)
    if not movable:
        raise SystemExit("no section to move")
    # The free list is the space the movable units cover, and under `pack` the
    # space of the sections the link will drop as well, since nothing will link
    # those in.
    free = []
    for u in sorted([u for u in all_units if u.start in dead] + movable,
                    key=lambda u: u.start):
        if free and free[-1][1] == u.start:
            free[-1][1] = u.end
        else:
            free.append([u.start, u.end])
    if mode == "shift":
        free.append([SPARE_BASE, SPARE_END])
        order, free[0][0] = sorted(movable, key=lambda u: u.start), free[0][0] + 4
    elif mode == "reverse":
        free.append([SPARE_BASE, SPARE_END])
        order = sorted(movable, key=lambda u: -u.start)
    elif mode == "pack":
        moves, hole = pack(movable, free)
        return moves, hole, held, dead
    else:
        raise SystemExit("unknown layout %r" % mode)

    # First fit over the whole free list rather than a single cursor: the
    # movable units are cut into intervals by the held ones between them, and a
    # cursor that gives up on an interval as soon as one does not fit wastes its
    # tail. The spare flash is last in the list, so it is only used for what the
    # image's own space cannot hold.
    moves = {}
    for u in order:
        size, want = u.end - u.start, residue(u)
        for interval in free:
            at = interval[0] + (want - interval[0]) % 4
            if at == u.start:
                # The point of the layout is that no section keeps its address,
                # so the one address this unit may not have is its own; the next
                # slot up is still in the same interval.
                at += 4
            if at + size <= interval[1]:
                u.place(at, moves)
                interval[0] = at + size
                break
        else:
            raise SystemExit("the image does not fit: %s needs %d bytes"
                             % (u.sym, size))
    kept = [u.sym for u in movable if moves[u.sym] == u.start]
    if kept:
        raise SystemExit("%d units keep their address under %s: %s"
                         % (len(kept), mode, ", ".join(kept[:5])))
    spare = max((moves[u.sym] + (u.end - u.start) for u in movable
                 if moves[u.sym] >= SPARE_BASE), default=SPARE_BASE)
    return moves, spare, held, dead


def ram_relayout(runs, sections, mode, free_end, established):
    """Give every RAM item a new address, keeping what the image depends on.

    Two constraints, the same two the flash side has and for the same reasons.
    An item keeps its address modulo 4, because the code indexes into it and a
    field's alignment is part of its layout. And an item stays inside its own
    run: a `.data` item's bytes come out of one copy and a `.bss` item's zeroes
    out of one fill, so an item that changed runs would be initialised by the
    wrong statement.

    `shift` slides every run up by one page of the free RAM above the app's
    `.bss`, so every RAM address changes and nothing else does. `reverse` also
    turns the zeroed runs' order round -- but by unit, not by item.

    A unit is a maximal run of items with no established boundary inside it.
    This is where RAM is weaker evidence than flash: the partition knows where
    a flash object ends, while a RAM item's end is usually only the next
    address something took, so two items may be two fields of one array the
    code walks from its head. Separating them is what a per-item permutation
    would do, and the image says so: reversing every item wedges the external
    flash driver, whose device rows are exactly that shape, while the same
    image with the region slid rigidly boots with a zero-line log diff. So an
    item may be moved with its neighbours and may only be moved away from them
    where a declared size, a zero fill's own bounds or a run's edge says the
    object stops there.

    The `.data` run keeps its unit order under both. Its items are laid out
    twice -- once in RAM and once as the load image in flash -- and the flash
    half has to stay the length the image gave it, since the version trailer
    starts where it stops; reordering it would cost alignment padding it has
    nowhere to put.
    """
    if mode not in ("shift", "reverse"):
        raise SystemExit("no RAM layout called %s" % mode)
    by_run = collections.defaultdict(list)
    for s in sections:
        run = next(r for r in runs if r.start <= s.vma < r.end)
        by_run[run.start].append(s)
    # Both modes slide as well as reorder, so that under `reverse` the `.data`
    # run moves too: its unit order is fixed, and without the slide it would be
    # the one run a reverse layout leaves exactly where it was.
    at = runs[0].start + RAM_SHIFT
    moves = {}
    for run in runs:
        units, held = [], sorted(by_run[run.start], key=lambda s: s.vma)
        for s in held:
            if not units or s.vma in established:
                units.append([])
            units[-1].append(s)
        if mode == "reverse" and run.kind == "bss":
            units.reverse()
        at = (at + 3) & ~3
        run.start = at
        for unit in units:
            while at % 4 != unit[0].vma % 4:
                at += 1
            base = at
            for s in unit:
                # Inside a unit the items keep their distances: nothing in the
                # image says where one stops, so the only safe statement is
                # that they are where they were relative to each other.
                moves[s.vma] = base + (s.vma - unit[0].vma)
                at = moves[s.vma] + (s.end - s.start)
        run.end = at
    if at > free_end:
        raise SystemExit("the RAM layout runs to 0x%x, past the 0x%x the app's"
                         " static RAM may reach" % (at, free_end))
    for s in sections:
        s.vma = moves[s.vma]
    return moves


# How far `--ram-layout shift` slides the app's static RAM. It is one page of
# the free RAM between the top of the app's `.bss` and facts.yaml's LIBRARY_RAM,
# which is the only room a shift has: below the app's base is the SoftDevice's
# RAM and the retained block, and above LIBRARY_RAM is the main stack.
RAM_SHIFT = 0x1000


def reclaim_placement(sections, pinned, path, obj, ram):
    """A placement that lets the linker drop and pack, for the size measurement.

    Nothing is pinned but the fixed points and nothing is KEEPed but them, so
    `--gc-sections` walks from the vector table and the hooks the library calls
    and drops every section nothing reaches -- the blob's own copy of the kernel
    first of all. The result is not an image anything can boot, because the
    version trailer is no longer where the bootloader reads it and the sim's
    patch addresses no longer mean anything; it answers how much is dead, which
    is what this step measures.
    """
    with open(path, "w") as fh:
        fh.write("/* Generated by abi/blobify.py --reclaim, do not edit: the"
                 " placement that measures what nothing references. */\n")
        fh.write(".blob 0x%08x :\n{\n" % APP_BASE)
        for s in sections:
            if s.start in pinned:
                fh.write("  KEEP(%s(%s))\n" % (s.object or obj, s.name))
        fh.write("  %s(.text.*)\n  %s(.rodata.*)\n} > APP\n" % (obj, obj))
        # The RAM items are swept up whole rather than measured: this link
        # answers how much flash is dead, and a RAM section with nowhere to go
        # would be placed by the linker's own orphan rules, which is the one
        # outcome that makes the flash number wrong for a reason nothing names.
        fh.write(".appdata 0x%08x : AT(0x%08x)\n{\n  %s(.data.*)\n} > RAM\n"
                 % (ram.runs[0].start, min(s.start for s in ram.data), obj))
        fh.write(".appbss (NOLOAD) :\n{\n  %s(.bss.*)\n} > RAM\n" % obj)


def placement(sections, moves, path, obj, keep=None, drop=(), hole=None,
              spill=(), ram=None, ram_keep=None):
    """The SECTIONS fragment that places every section.

    `ram` is the RAM partition. Its `.data` items are sections of the image
    with two addresses, so the flash placement is cut in two around their load
    image and `.appdata` sits between the halves: the location counter of the
    output section runs in RAM while `AT()` puts the bytes back in flash where
    the image has them, which is the same arrangement the original linker
    script made and the reason the byte-identical link still holds. The `.bss`
    runs are NOLOAD and take no flash at all.

    `keep` is the gc link: a section named in it is KEEPed, every other one is
    named without KEEP so `--gc-sections` may drop it. The addresses stay the
    ones the layout chose either way, because the constraints on where a section
    may go -- its address modulo 4, the parity a Thumb symbol needs, the pairs a
    pc-relative displacement holds together -- are not suspended by dropping its
    neighbour; a dropped section leaves a hole instead of packing the rest.

    One `. = <addr>` per section: if a section ever grows, the location counter
    moves backwards and the link fails instead of silently shifting the image.
    Everything not in `moves` keeps its original address, which is what makes
    the link byte-identical; the placements are emitted in address order, so a
    layout that permutes the text is the same fragment with different numbers.
    What does not fit in the image's own span goes to `.blobmoved` in the free
    flash after it.

    Each placement names the blob object: the partition's names include the
    library ones the blob carries its own copy of, so a bare `*(.text.<name>)`
    would also swallow the source build's section of that name. Inside an output
    section the location counter runs from the section's start, so the
    placements are offsets and the comment carries the address.
    """
    # `drop` is the sections a replacement has already made unreferenced. Under
    # `pack` they are left out of the fragment entirely so their bytes are the
    # hole, and relink.ld's `.blobdead` catches them: if one is somehow still
    # referenced it lands in the library region and overflows it, loudly, rather
    # than overlapping whatever was packed over it.
    placed = sorted((moves.get(s.sym, s.start), s) for s in sections
                    if s.start not in drop)
    image = [(a, s) for a, s in placed if a < APP_END]
    spare = [(a, s) for a, s in placed if a >= APP_END]
    for (a, s), (b, t) in zip(image, image[1:]):
        if a + (s.end - s.start) > b:
            raise SystemExit("%s at 0x%x overlaps %s at 0x%x" % (s.sym, a, t.sym, b))
    def line(at, base, s):
        why = None if keep is None else keep.get(s.start)
        if keep is None or why is not None:
            return ("  . = 0x%06x; KEEP(%s(%s))   /* 0x%08x%s */\n"
                    % (at - base, s.object or obj, s.name, at,
                       "" if why is None else ": " + why))
        return ("  . = 0x%06x; %s(%s)   /* 0x%08x */\n"
                % (at - base, s.object or obj, s.name, at))

    with open(path, "w") as fh:
        fh.write("/* Generated by abi/blobify.py, do not edit: where each of the app's"
                 " sections goes%s. */\n"
                 % ("" if keep is None else ", and which of them --gc-sections"
                    " may not drop"))
        # The flash the packing freed, handed to the library: the archive whose
        # replacements made the hole is the archive linked into it. The `. =`
        # that follows is the bound -- the location counter moves backwards and
        # the link fails if the spill does not fit. Nothing is placed inside the
        # hole, so it goes after the last section below it.
        def spill_block(base):
            fh.write("  /* 0x%08x..0x%08x: %d bytes the packing freed */\n"
                     % (hole[0], hole[1], hole[1] - hole[0]))
            fh.write("  . = 0x%06x;\n" % (hole[0] - base))
            for pattern in spill:
                fh.write("  %s(.text .text.* .rodata .rodata.*)\n" % pattern)
            fh.write("  . = 0x%06x;\n" % (hole[1] - base))

        state = {"spilled": hole is None}

        def flash_block(name, base, end, rows, region="APP"):
            fh.write("%s 0x%08x :\n{\n" % (name, base))
            for at, s in rows:
                if not state["spilled"] and at >= hole[1]:
                    spill_block(base)
                    state["spilled"] = True
                fh.write(line(at, base, s))
            if not state["spilled"] and end >= hole[1]:
                spill_block(base)
                state["spilled"] = True
            fh.write("  . = 0x%06x;\n} > %s\n" % (end - base, region))

        if ram is None:
            flash_block(".blob", APP_BASE, APP_END, image)
        else:
            # The load image's own extent in flash, read off the sections
            # rather than off the run: a RAM layout changes where the run is
            # and how long it is, and the bytes stay where the image has them.
            lo = min(s.start for s in ram.data)
            hi = max(s.end for s in ram.data)
            flash_block(".blob", APP_BASE, lo,
                        [(a, s) for a, s in image if a < lo])
            # The RAM runs in address order, so the location counter only ever
            # moves forward. The initialiser image is placed in RAM and loaded
            # from flash: one `. =` per item and one `AT()` for the run, which
            # is the arrangement under which the single copy the startup makes
            # is the copy the linker described. Both ends of every run are
            # linker-defined, because the startup reads them out of words that
            # are now relocations against these names.
            for zero in ram.runs:
                held = sorted((s for s in ram.sections
                               if zero.start <= s.vma < zero.end),
                              key=lambda s: s.vma)
                if zero.kind == "data":
                    fh.write(".appdata 0x%08x : AT(0x%08x)\n{\n"
                             % (zero.start, lo))
                else:
                    fh.write(".app%s 0x%08x (NOLOAD) :\n{\n"
                             % (zero.name, zero.start))
                fh.write("  __%s_start__ = .;\n" % zero.name)
                for s in held:
                    why = None if ram_keep is None else ram_keep.get(s.vma)
                    fh.write("  . = 0x%06x; %s   /* 0x%08x%s%s */\n"
                             % (s.vma - zero.start,
                                "%s(%s)" % (s.object or obj, s.name)
                                if ram_keep is not None and why is None
                                else "KEEP(%s(%s))" % (s.object or obj, s.name),
                                s.vma, "" if s.nobits
                                else " from 0x%08x" % s.start,
                                "" if why is None else ": " + why))
                fh.write("  . = 0x%06x;\n  __%s_end__ = .;\n} > RAM\n"
                         % (zero.end - zero.start, zero.name))
                if zero.kind == "data":
                    fh.write("__data_load__ = LOADADDR(.appdata);\n")
            flash_block(".blobtail", hi, APP_END,
                        [(a, s) for a, s in image if a >= hi])
        if not spare:
            return
        fh.write(".blobmoved 0x%08x :\n{\n" % spare[0][0])
        for at, s in spare:
            fh.write(line(at, spare[0][0], s))
        fh.write("} > LIB\n")
