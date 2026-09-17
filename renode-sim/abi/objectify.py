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
        self.relocs = []            # (offset, symbol, type)
        self.labels = {}            # (address, is_function) -> symbol, interior targets
        self.functions = []         # the entry points the section holds
        self.index = 0

    def __contains__(self, addr):
        return self.start <= addr < self.end


MIN_STRING = 2


def string_runs(blob, instruction_bytes):
    """Every run of non-NUL bytes the disassembly does not cover.

    A string is delimited by NULs, so that is what the partition has to keep
    whole, and not the printable part of it: this image logs with ANSI escapes,
    so "\x1b[31mNot charging\x1b[m" is one object whose first byte is not
    printable and whose pointer names that byte. Bytes an instruction covers end
    a run and disqualify it, which is what keeps this away from code that
    happens to read as text.
    """
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
        if end - i >= MIN_STRING:
            runs.append((APP_BASE + i, APP_BASE + end))
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


def build_sections(items, calls, reads, falls, pointer_words, strings, reserved):
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
    tiles = tiles_of(items)
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


def internal_relocations(blob, layout, calls, skip):
    """Every internal control transfer that crosses a section, as a relocation.

    Sites the library boundary owns are skipped: boundary.yaml decides those,
    and its symbols resolve to the source build rather than to the blob's own
    copy.
    """
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
                                    layout.label(dst, thumb=True), rtype))
        counts[rtype] += 1
    return counts, unrelocatable, indirect


def word_relocations(blob, layout, words, skip):
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
    counts = {"pointer": 0, "into_code": 0, "ram": 0}
    for row in words:
        if row["class"] != "pointer" or row["addr"] in skip:
            continue
        section = layout.at(row["addr"])
        if section is None or row["addr"] + 4 > section.end:
            raise SystemExit("word 0x%x is not inside one section" % row["addr"])
        sym = layout.label(row["target"], thumb=row["thumb_target"])
        off = row["addr"] - APP_BASE
        blob[off:off + 4] = b"\0\0\0\0"
        section.relocs.append((row["addr"] - section.start, sym, R_ARM_ABS32))
        counts["pointer"] += 1
        counts["into_code"] += 1 if row["in_code"] else 0
    counts["ram"] = sum(1 for r in words if r["signal"] == "ram")
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
    """
    return section.start % 4


def relayout(sections, mode, pinned, anchors):
    """Give every text section a new address, and prove none keeps its old one.

    Data does not move in this step, so the space the text may use is exactly
    the space it uses now: the intervals the text sections cover, plus the free
    flash after the image for whatever alignment costs. The two modes are the
    two ways of being sure a stale address cannot survive by luck: `shift` keeps
    the order and starts one word further in, so every section slides; `reverse`
    puts the last function first, so nothing is near where it was.
    """
    # A section a word points into that the classification could not decide is
    # not moved: the word is either a pointer that would be left stale or a
    # constant that must not be rewritten, and there is no way to be right about
    # both while the target moves. Untyped record tables end up inside code
    # sections when the span between two code tiles is one component, which is
    # how they come to be movable at all. The other anchor is a pc-relative
    # displacement, whose two ends have to keep their distance.
    held = {}
    for s in sections:
        for v, why in anchors.items():
            if s.start <= v < s.end:
                held.setdefault(s.start, why)
    movable = [s for s in sections
               if s.kind == "code" and s.start not in pinned and s.start not in held]
    held = dict(held)
    if not movable:
        raise SystemExit("no text section to move")
    free = []
    for s in sorted(movable, key=lambda s: s.start):
        if free and free[-1][1] == s.start:
            free[-1][1] = s.end
        else:
            free.append([s.start, s.end])
    free.append([SPARE_BASE, SPARE_END])
    if mode == "shift":
        order, free[0][0] = sorted(movable, key=lambda s: s.start), free[0][0] + 4
    elif mode == "reverse":
        order = sorted(movable, key=lambda s: -s.start)
    else:
        raise SystemExit("unknown layout %r" % mode)

    # First fit over the whole free list rather than a single cursor: the text
    # is cut into intervals by the data between them, and a cursor that gives up
    # on an interval as soon as one section does not fit wastes its tail. The
    # spare flash is last in the list, so it is only used for what the image's
    # own space cannot hold.
    moves = {}
    for s in order:
        size, want = s.end - s.start, residue(s)
        for interval in free:
            at = interval[0] + (want - interval[0]) % 4
            if at == s.start:
                # The point of the layout is that no function keeps its
                # address, so the one address this section may not have is its
                # own; the next slot up is still in the same interval.
                at += 4
            if at + size <= interval[1]:
                moves[s.sym] = at
                interval[0] = at + size
                break
        else:
            raise SystemExit("the text does not fit: %s needs %d bytes"
                             % (s.sym, size))
    kept = [s.sym for s in movable if moves[s.sym] == s.start]
    if kept:
        raise SystemExit("%d text sections keep their address under %s: %s"
                         % (len(kept), mode, ", ".join(kept[:5])))
    spare = max((a + (s.end - s.start) for s, a in
                 ((s, moves[s.sym]) for s in movable) if a >= SPARE_BASE),
                default=SPARE_BASE)
    return moves, spare, held


def reclaim_placement(sections, pinned, path, obj):
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
                fh.write("  KEEP(%s(%s))\n" % (obj, s.name))
        fh.write("  %s(.text.*)\n  %s(.rodata.*)\n} > APP\n" % (obj, obj))


def placement(sections, moves, path, obj):
    """The SECTIONS fragment that places every section.

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
    placed = sorted((moves.get(s.sym, s.start), s) for s in sections)
    image = [(a, s) for a, s in placed if a < APP_END]
    spare = [(a, s) for a, s in placed if a >= APP_END]
    for (a, s), (b, t) in zip(image, image[1:]):
        if a + (s.end - s.start) > b:
            raise SystemExit("%s at 0x%x overlaps %s at 0x%x" % (s.sym, a, t.sym, b))
    with open(path, "w") as fh:
        fh.write("/* Generated by abi/blobify.py, do not edit: where each of the app's"
                 " sections goes. */\n")
        fh.write(".blob 0x%08x :\n{\n" % APP_BASE)
        for at, s in image:
            fh.write("  . = 0x%06x; KEEP(%s(%s))   /* 0x%08x */\n"
                     % (at - APP_BASE, obj, s.name, at))
        fh.write("  . = 0x%06x;\n} > APP\n" % (APP_END - APP_BASE))
        if not spare:
            return
        fh.write(".blobmoved 0x%08x :\n{\n" % spare[0][0])
        for at, s in spare:
            fh.write("  . = 0x%06x; KEEP(%s(%s))   /* 0x%08x */\n"
                     % (at - spare[0][0], obj, s.name, at))
        fh.write("} > LIB\n")
