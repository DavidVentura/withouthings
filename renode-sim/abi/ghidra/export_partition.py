# Ghidra post-script: export the analysed app as a partition plus its references.
#
# Run by abi/ghidra/analyze.sh, which passes the output directory as the one
# argument. Writes items.json, references.json and symbols.txt there.
#
# The partition is taken from the listing's code units, which tile the address
# space by construction, so the cover is exact and any overlap can only come
# from two function bodies claiming the same bytes (reported, not repaired).
# @category HWA10

import json
import os

from ghidra.program.model.address import AddressSet
from ghidra.program.model.data import Array, Structure
from ghidra.program.model.listing import Instruction
from ghidra.program.model.symbol import RefType, SourceType

APP_BASE = 0x27000
APP_END = 0xF117C
RAM_BASE, RAM_END = 0x20000000, 0x20040000


def in_app(v):
    return APP_BASE <= v < APP_END


def hexs(v):
    return "0x%x" % v


def emit(path, header, sections):
    """JSON with one row per line, so runs diff by row and consumers load the
    12 MB references file in well under a second (PyYAML took 40 s)."""
    with open(path, "w") as fh:
        fh.write('{"_generated": %s' % json.dumps(header))
        for name, rows in sections:
            if isinstance(rows, dict):
                fh.write(',\n"%s": %s' % (name, json.dumps(rows)))
                continue
            fh.write(',\n"%s": [' % name)
            for i, row in enumerate(rows):
                fh.write("%s\n%s" % ("," if i else "", json.dumps(row)))
            fh.write("]")
        fh.write("}\n")


out_dir = getScriptArgs()[0]
listing = currentProgram.getListing()
fm = currentProgram.getFunctionManager()
rm = currentProgram.getReferenceManager()
mem = currentProgram.getMemory()
space = currentProgram.getAddressFactory().getDefaultAddressSpace()
app_set = AddressSet(space.getAddress(APP_BASE), space.getAddress(APP_END - 1))


def word_at(off):
    return mem.getInt(space.getAddress(off)) & 0xFFFFFFFF


# --- SVC wrappers return, whatever the analysis concluded about the SVC ------
svc_fixed = 0
for fn in fm.getFunctions(app_set, True):
    if fn.hasNoReturn():
        body = fn.getBody()
        if body.getNumAddresses() == 4:
            start = body.getMinAddress().getOffset()
            if word_at(start) & 0xFF00FFFF == 0x4770DF00:
                fn.setNoReturn(False)
                svc_fixed += 1

# --- functions ---------------------------------------------------------------
functions, fn_by_start, fn_ranges = [], {}, []
for fn in fm.getFunctions(app_set, True):
    body = fn.getBody()
    start = fn.getEntryPoint().getOffset()
    sym = fn.getSymbol()
    functions.append({
        "start": start,
        "end": body.getMaxAddress().getOffset() + 1,
        "name": fn.getName(),
        "named": "ghidra" if sym.getSource() in (SourceType.DEFAULT, SourceType.ANALYSIS) else "seed",
        "bytes": body.getNumAddresses(),
        "ranges": body.getNumAddressRanges(),
    })
    fn_by_start[start] = fn.getName()
    for r in body.getAddressRanges():
        fn_ranges.append((r.getMinAddress().getOffset(), r.getMaxAddress().getOffset() + 1, start))

fn_ranges.sort()
overlaps = []
for (s0, e0, f0), (s1, e1, f1) in zip(fn_ranges, fn_ranges[1:]):
    if s1 < e0:
        overlaps.append({"a": f0, "b": f1, "start": s1, "end": min(e0, e1)})

# --- what reads each data item ----------------------------------------------
# Pool words are the targets of pc-relative loads, which is where the compiler
# put every address constant; a computed jump reading a word instead marks an
# inline switch table. Neither lands inside the reading function's body, so the
# reader is what attributes them.
#
# The reader is decoded out of the instruction rather than taken from Ghidra's
# reference: the reference is what the constant analyzer chose to record, which
# it skips wherever it could not follow the flow, and a pool attributed to the
# wrong function lands in a section a pc-relative load cannot reach from.


def literal_target(addr, raw, length):
    """The address a pc-relative word load at `addr` reads, or None.

    Only the word-sized literal forms: a byte or halfword load cannot be
    holding an address, and an address is the only thing step 3 relocates.
    """
    base = (addr + 4) & ~3
    if length == 2:
        hw = raw[0] | (raw[1] << 8)
        if hw & 0xF800 == 0x4800:                       # LDR (literal) T1
            return base + (hw & 0xFF) * 4, 4
        return None
    hw1 = raw[0] | (raw[1] << 8)
    hw2 = raw[2] | (raw[3] << 8)
    if hw1 & 0xFF7F == 0xF85F:                          # LDR (literal) T2
        imm = hw2 & 0xFFF
        return base + (imm if hw1 & 0x80 else -imm), 4
    if hw1 & 0xFE7F == 0xE85F:                          # LDRD (literal)
        imm = (hw2 & 0xFF) * 4
        return base + (imm if hw1 & 0x80 else -imm), 8
    if hw1 & 0xFF3F == 0xED1F and hw2 & 0xE00 == 0xA00:  # VLDR (literal)
        imm = (hw2 & 0xFF) * 4
        return base + (imm if hw1 & 0x80 else -imm), 4
    return None


image = bytearray(b & 0xFF for b in
                  getBytes(space.getAddress(APP_BASE), APP_END - APP_BASE))


def raw_at(addr, n):
    return image[addr - APP_BASE:addr - APP_BASE + n]


pool_words, pool_owner, jump_owner = set(), {}, {}
pool_reads, computed_jumps = [], []
for instr in listing.getInstructions(app_set, True):
    fn = fm.getFunctionContaining(instr.getMinAddress())
    owner = fn.getEntryPoint().getOffset() if fn is not None else None
    site = instr.getMinAddress().getOffset()
    mnem = instr.getMnemonicString().lower()
    if mnem.startswith(("ldr", "vldr")):
        found = literal_target(site, raw_at(site, instr.getLength()),
                               instr.getLength())
        if found is not None and in_app(found[0]) and found[0] % 4 == 0:
            target, width = found
            for at in range(target, target + width, 4):
                pool_words.add(at)
                pool_owner.setdefault(at, owner)
                pool_reads.append({"site": site, "target": at,
                                   "function": owner or 0, "mnemonic": mnem,
                                   "width": instr.getLength()})
    if not (instr.getFlowType().isJump() and instr.getFlowType().isComputed()):
        continue
    targets = sorted(set(r.getToAddress().getOffset()
                         for r in instr.getReferencesFrom()
                         if r.getReferenceType().isJump()
                         and listing.getInstructionAt(r.getToAddress()) is not None))
    computed_jumps.append({"site": site, "function": owner or 0, "mnemonic": mnem,
                           "width": instr.getLength(), "targets": targets})
    for ref in instr.getReferencesFrom():
        to = ref.getToAddress().getOffset()
        if in_app(to) and listing.getInstructionAt(ref.getToAddress()) is None:
            jump_owner.setdefault(to, owner)


def jump_table(jump):
    """The whole indexed table a computed jump reads, decoded and checked.

    Ghidra types the first entry and leaves the rest untyped, so the table's
    tail falls into the gap runs and nothing binds it to the switch. The extent
    is the smallest case target above the table, and every entry has to decode
    to a target Ghidra also found, or the table is not claimed at all.
    """
    site, targets = jump["site"], set(jump["targets"])
    if not targets:
        return None
    mnem = jump["mnemonic"]
    if mnem in ("tbb", "tbh"):
        base = site + jump["width"]
        stride = 1 if mnem == "tbb" else 2
        start = base
    else:
        reads = [to for to in (r.getToAddress().getOffset()
                               for r in listing.getInstructionAt(
                                   space.getAddress(site)).getReferencesFrom())
                 if in_app(to) and to not in targets]
        if len(set(reads)) != 1:
            return None
        base, stride, start = 0, 4, sorted(set(reads))[0]

    def entry(at):
        raw = raw_at(at, stride)
        if stride == 1:
            return base + 2 * raw[0]
        if stride == 2:
            return base + 2 * (raw[0] | (raw[1] << 8))
        return (raw[0] | (raw[1] << 8) | (raw[2] << 16) | (raw[3] << 24)) & ~1

    entries = []
    if stride == 4:
        # An absolute table can sit anywhere, so its extent is where it stops
        # decoding rather than where the first case body starts.
        at = start
        while at + 4 <= APP_END and entry(at) in targets:
            entries.append(entry(at))
            at += 4
        end = at
    else:
        # The table is inline, so it runs up to the first case body.
        end = min((t for t in targets if t > start), default=None)
        if end is None or end - start > 4096:
            return None
        # An odd-length byte table is padded to the halfword the next
        # instruction needs; the pad byte is not an entry.
        pad = (end - start) % stride
        for at in range(start, end - pad, stride):
            if entry(at) not in targets:
                return None
            entries.append(entry(at))
    if not entries or set(entries) != targets:
        return None
    return {"site": site, "function": jump["function"], "mnemonic": mnem,
            "start": start, "end": end, "stride": stride,
            "absolute": stride == 4, "entries": len(entries)}


# --- how the loaded register is used ----------------------------------------
# The structural signals (does the value land on a function start, on an item
# start) decide most words; what is left needs the other end, which is what the
# code does with the value. Following the destination register forward from the
# load separates a pointer (branched to, dereferenced, handed on) from a number
# (compared, added, multiplied) without running the decompiler.
USE_CLASSES = [
    (("blx", "bx", "bl", "b"), "call"),
    (("ldr", "ldrb", "ldrh", "ldrsb", "ldrsh", "ldrd", "ldm", "ldmia", "vldr",
      "str", "strb", "strh", "strd", "stm", "stmia", "stmdb", "vstr"), "memory"),
    (("cmp", "cmn", "tst", "teq"), "compare"),
    (("add", "adds", "sub", "subs", "mul", "muls", "and", "ands", "orr", "orrs",
      "eor", "eors", "lsl", "lsls", "lsr", "lsrs", "asr", "asrs", "rsb", "rsbs",
      "mla", "sdiv", "udiv", "mvn", "bic", "adc", "sbc", "uxtb", "uxth", "sxtb",
      "sxth"), "arith"),
]
FOLLOW = 32


def register_use(site, length, function):
    """What the first instruction that reads the loaded register does with it.

    Returns one of the USE_CLASSES names, "argument" when the value goes into an
    argument register that a call then consumes, "escapes" when it is stored or
    moved somewhere this walk does not follow, or None when nothing was found
    within FOLLOW instructions.
    """
    instr = listing.getInstructionAt(space.getAddress(site))
    out = [o for o in instr.getResultObjects() if hasattr(o, "getName")]
    if len(out) != 1:
        return None
    reg, at, steps = out[0], site + length, 0
    while steps < FOLLOW:
        steps += 1
        nxt = listing.getInstructionAt(space.getAddress(at))
        if nxt is None:
            return None
        fn = fm.getFunctionContaining(nxt.getMinAddress())
        if fn is None or fn.getEntryPoint().getOffset() != function:
            return None
        at += nxt.getLength()
        mnem = nxt.getMnemonicString().lower().split(".")[0]
        reads = any(o == reg for o in nxt.getInputObjects())
        writes = any(o == reg for o in nxt.getResultObjects())
        if not reads:
            if writes:
                return None             # redefined before anything read it
            continue
        if mnem in ("mov", "movs", "cpy"):
            moved = [o for o in nxt.getResultObjects() if hasattr(o, "getName")]
            if len(moved) == 1:
                reg = moved[0]
                continue
            return "escapes"
        if mnem in ("push",):
            return "escapes"
        for mnemonics, name in USE_CLASSES:
            if mnem in mnemonics:
                if name == "memory" and not mnem.startswith(("ldr", "ldm", "vldr")):
                    # `str rX, [rY, #n]`: rY is a pointer, rX is the value, and
                    # Ghidra lists the stored value first.
                    inputs = nxt.getInputObjects()
                    return "escapes" if inputs and inputs[0] == reg else "memory"
                return name
        return "escapes"
    return None


tables, unclaimed_jumps = [], []
for jump in computed_jumps:
    found = jump_table(jump)
    (tables if found else unclaimed_jumps).append(found or jump)
table_at = {}
for t in tables:
    table_at[t["start"]] = t
table_span = {}
for t in tables:
    for at in range(t["start"], t["end"]):
        table_span[at] = t


def item_class(dt, name):
    """classify_gaps.py's name prefixes, plus what Ghidra itself typed."""
    if name.startswith("pad_"):
        return "padding"
    if name.startswith("ptrtab_"):
        return "pointer_table"
    if name.startswith("flttab_"):
        return "float_table"
    if dt.getName().lower().startswith(("string", "unicode", "char")):
        return "string"
    if dt.getName().startswith("undefined"):
        return "untyped"
    return "typed"


# --- the cover: every code unit is code, data or undefined -------------------
data_items, inline_items, gaps, array_rows = [], [], [], []
code_bytes = data_bytes = gap_bytes = 0
orphan_code = []
gap_run = None
claim_end = 0

# Which bytes an instruction occupies. A 32-bit window over two Thumb
# instructions holds an in-range value often enough to drown a stale-address
# scan in noise (`0x0009e92d` is `movs r1,r1` followed by `push.w`), and a word
# the disassembly covers cannot be a relocation slot, so the candidate set is
# exactly what this bitmap leaves out.
instruction_bytes = bytearray(APP_END - APP_BASE)

for cu in listing.getCodeUnits(app_set, True):
    start = cu.getMinAddress().getOffset()
    length = cu.getLength()
    fn = fm.getFunctionContaining(cu.getMinAddress())
    if isinstance(cu, Instruction):
        code_bytes += length
        for i in range(start - APP_BASE, start - APP_BASE + length):
            instruction_bytes[i] = 1
        if fn is None:
            if orphan_code and orphan_code[-1]["end"] == start:
                orphan_code[-1]["end"] = start + length
            else:
                orphan_code.append({"start": start, "end": start + length})
        if gap_run:
            gaps.append(gap_run)
            gap_run = None
        continue
    if start < claim_end:
        data_bytes += length
        continue
    def undefined_word(at):
        """All four bytes untyped, so claiming them as one item loses nothing."""
        for i in range(4):
            unit = listing.getCodeUnitAt(space.getAddress(at + i))
            if unit is None or isinstance(unit, Instruction) or unit.isDefined():
                return False
        return True

    if start in table_at and start not in pool_owner:
        # A switch table: Ghidra types its first entry and leaves the tail in
        # the gap runs, so the cover would split a table the switch indexes as
        # one object. Claim the whole extent for the function that switches.
        t = table_at[start]
        inline_items.append({"start": t["start"], "end": t["end"],
                             "type": "%s[%d]" % ("undefined4" if t["stride"] == 4
                                                 else "byte" if t["stride"] == 1
                                                 else "ushort", t["entries"]),
                             "name": "", "class": "jumptable", "kind": "jumptable",
                             "function": t["function"]})
        data_bytes += length
        claim_end = t["end"]
        if gap_run:
            gaps.append(gap_run)
            gap_run = None
        continue
    if (start in pool_owner or start in jump_owner) and undefined_word(start):
        # An undefined word the code reads as a constant: still an item, and the
        # objectification needs it as one even though Ghidra left it untyped.
        inline_items.append({"start": start, "end": start + 4, "type": "undefined4",
                             "name": "", "class": "untyped",
                             "kind": "jumptable" if start in jump_owner else "pool",
                             "function": jump_owner.get(start) or pool_owner.get(start) or 0})
        data_bytes += length
        claim_end = start + 4
        if gap_run:
            gaps.append(gap_run)
            gap_run = None
        continue
    if not cu.isDefined():
        gap_bytes += length
        if gap_run and gap_run["end"] == start:
            gap_run["end"] = start + length
        else:
            if gap_run:
                gaps.append(gap_run)
            gap_run = {"start": start, "end": start + length}
        continue
    if gap_run:
        gaps.append(gap_run)
        gap_run = None
    data_bytes += length
    sym = getSymbolAt(cu.getMinAddress())
    dt = cu.getDataType()
    name = sym.getName() if sym is not None else ""
    item = {"start": start, "end": start + length, "type": dt.getName(), "name": name,
            "class": item_class(dt, name)}
    if isinstance(dt, Array) and isinstance(dt.getDataType(), (Structure, Array)):
        stride = dt.getElementLength()
        for i in range(dt.getNumElements()):
            array_rows.append({"start": start + i * stride,
                               "end": start + (i + 1) * stride, "array": start})
    if start in jump_owner or start in pool_owner:
        item["kind"] = "jumptable" if start in jump_owner else "pool"
        item["function"] = (jump_owner.get(start) or pool_owner.get(start) or
                            (fn.getEntryPoint().getOffset() if fn is not None else 0))
        inline_items.append(item)
    else:
        data_items.append(item)
if gap_run:
    gaps.append(gap_run)

item_starts = (set(d["start"] for d in data_items) | set(d["start"] for d in inline_items)
               | set(r["start"] for r in array_rows))
# The lookup the classification uses: the cover, plus the code Ghidra
# disassembled but attributed to no function, which is a class of its own
# because a pointer to the head of such a run is a function Ghidra missed.
spans = [(d["start"], d["end"], "item") for d in data_items + inline_items]
spans += [(s, e, "code") for s, e, _ in fn_ranges]
spans += [(g["start"], g["end"], "gap") for g in gaps]
spans += [(o["start"], o["end"], "orphan") for o in orphan_code]
spans.sort()
uncovered = [(a[1], b[0]) for a, b in zip(spans, spans[1:]) if a[1] < b[0]]
orphan_starts = set(o["start"] for o in orphan_code)

# --- references --------------------------------------------------------------


def base_mnemonic(mnemonic):
    """Ghidra renders an instruction inside an IT block as `bl.eq`."""
    head, _, tail = mnemonic.rpartition(".")
    return head if head and tail in ("eq", "ne", "cs", "hs", "cc", "lo", "mi",
                                     "pl", "vs", "vc", "hi", "ls", "ge", "lt",
                                     "gt", "le") else mnemonic

# The relocation a site needs follows from the instruction, not from Ghidra's
# reference type: Ghidra records a `b.w` tail call as a call like any other.
def branch_target(addr, raw, length):
    """The target of a Thumb branch at `addr`, or None if it is not one.

    Ghidra's reference manager is not the authority here: it records what the
    analysis followed, and a branch into bytes it never disassembled leaves no
    reference at all, which would silently cost a relocation. The encoding is
    unambiguous, so the bytes are.
    """
    hw1 = raw[0] | (raw[1] << 8)
    if length == 2:
        if hw1 & 0xF800 == 0xE000:                      # B (T2)
            imm = (hw1 & 0x7FF) << 1
            return addr + 4 + (imm - (1 << 12) if imm & (1 << 11) else imm)
        if hw1 & 0xF000 == 0xD000 and (hw1 >> 8) & 0xF < 0xE:   # B<c> (T1)
            imm = (hw1 & 0xFF) << 1
            return addr + 4 + (imm - (1 << 9) if imm & (1 << 8) else imm)
        return None
    if length != 4 or hw1 & 0xF800 != 0xF000:
        return None
    hw2 = raw[2] | (raw[3] << 8)
    sign = (hw1 >> 10) & 1
    if hw2 & 0xD000 in (0xD000, 0x9000):                # BL, B (T4)
        j1, j2 = (hw2 >> 13) & 1, (hw2 >> 11) & 1
        imm = ((sign << 24) | ((1 - (j1 ^ sign)) << 23) | ((1 - (j2 ^ sign)) << 22)
               | ((hw1 & 0x3FF) << 12) | ((hw2 & 0x7FF) << 1))
        return addr + 4 + (imm - (1 << 25) if sign else imm)
    if hw2 & 0xD000 == 0x8000 and (hw1 >> 6) & 0xF < 0xE:       # B<c> (T3)
        imm = ((sign << 20) | (((hw2 >> 13) & 1) << 19) | (((hw2 >> 11) & 1) << 18)
               | ((hw1 & 0x3F) << 12) | ((hw2 & 0x7FF) << 1))
        return addr + 4 + (imm - (1 << 21) if sign else imm)
    return None


# Every direct branch, decoded from the bytes. The reference manager is not the
# authority for control flow: it stores what the analysis chose to record, and a
# branch into a run Ghidra never disassembled can be missing from it entirely,
# which costs a relocation and moves the image's execution into whatever landed
# at the old target. The encoding is unambiguous, so the bytes decide, and the
# reference manager only adds the indirect transfers the bytes cannot give.
calls, ext_calls, decoded = [], [], set()
for instr in listing.getInstructions(app_set, True):
    site = instr.getMinAddress().getOffset()
    mnem = instr.getMnemonicString().lower()
    if not base_mnemonic(mnem).startswith("b"):
        continue
    target = branch_target(site, raw_at(site, instr.getLength()), instr.getLength())
    if target is None:
        continue
    owner = fm.getFunctionContaining(instr.getMinAddress())
    decoded.add(site)
    row = {"from": site, "to": target,
           "kind": "call" if base_mnemonic(mnem) == "bl" else "jump",
           "mnemonic": mnem, "width": instr.getLength(),
           "function": owner.getEntryPoint().getOffset() if owner is not None else 0,
           "target_is_function": target in fn_by_start, "external": not in_app(target)}
    (calls if in_app(target) else ext_calls).append(row)

for src in rm.getReferenceSourceIterator(app_set, True):
    instr = listing.getInstructionAt(src)
    if instr is None or src.getOffset() in decoded:
        continue
    owner = fm.getFunctionContaining(src)
    for ref in rm.getReferencesFrom(src):
        t = ref.getReferenceType()
        if not (t.isCall() or t.isJump()):
            continue
        to = ref.getToAddress().getOffset()
        row = {"from": src.getOffset(), "to": to,
               "kind": "call" if t.isCall() else "jump",
               "mnemonic": instr.getMnemonicString().lower(),
               "width": instr.getLength(),
               "function": owner.getEntryPoint().getOffset() if owner is not None else 0,
               "target_is_function": to in fn_by_start,
               "external": not in_app(to)}
        (calls if in_app(to) else ext_calls).append(row)

# The candidates: every aligned word of the image the disassembly does not
# cover. That is the whole slot set by construction -- a literal pool, an
# absolute jump table, a word of a data item, a word of an untyped record table
# and a word the compiler left between two basic blocks are all just bytes no
# instruction claims -- so nothing has to be enumerated a second way.
item_spans = sorted([(d["start"], d["end"], "data") for d in data_items] +
                    [(d["start"], d["end"], d["kind"]) for d in inline_items])
item_starts_sorted = [s for s, _, _ in item_spans]


def word_kind(off):
    if off in pool_words:
        return "pool"
    lo, hi = 0, len(item_spans)
    while lo < hi:
        mid = (lo + hi) // 2
        if item_starts_sorted[mid] <= off:
            lo = mid + 1
        else:
            hi = mid
    if lo and item_spans[lo - 1][1] > off:
        return item_spans[lo - 1][2]
    return "gap"


candidates = [off for off in range(APP_BASE, APP_END - 3, 4)
              if not any(instruction_bytes[off - APP_BASE + i] for i in range(4))]


def locate(value):
    """Where an in-range value lands in the partition."""
    if value in fn_by_start:
        return "function_start", value
    if value in item_starts:
        return "item_start", value
    if value in orphan_starts:
        return "orphan_code_start", value
    lo, hi = 0, len(spans)
    while lo < hi:
        mid = (lo + hi) // 2
        if spans[mid][0] <= value:
            lo = mid + 1
        else:
            hi = mid
    if lo and spans[lo - 1][0] <= value < spans[lo - 1][1]:
        return "interior_" + spans[lo - 1][2], spans[lo - 1][0]
    return "nothing", 0


readers, uses = {}, {}
for r in pool_reads:
    readers.setdefault(r["target"], []).append(r["site"])
    r["use"] = register_use(r["site"], r["width"], r["function"])
    if r["use"]:
        uses.setdefault(r["target"], set()).add(r["use"])

words, counts = [], {}
for addr_off in candidates:
    kind = word_kind(addr_off)
    value = word_at(addr_off)
    thumb = bool(value & 1)
    if in_app(value & ~1) and thumb:
        klass, item = locate(value & ~1)
        klass = "thumb_" + klass
    elif in_app(value):
        klass, item = locate(value)
    elif RAM_BASE <= value < RAM_END:
        klass, item = "ram", 0
    elif APP_BASE > value >= 0x1000:
        klass, item = "softdevice", 0
    else:
        klass, item = "out_of_range", 0
    resolved = len(rm.getReferencesFrom(space.getAddress(addr_off))) > 0
    counts[kind + ":" + klass] = counts.get(kind + ":" + klass, 0) + 1
    words.append({"addr": addr_off, "value": value, "kind": kind, "class": klass,
                      "item": item, "offset": (value & ~1) - item if item else 0,
                      "thumb": thumb, "resolved": resolved,
                      "readers": readers.get(addr_off, []),
                      "uses": sorted(uses.get(addr_off, ()))})

unresolved = [w for w in words if not w["resolved"] and w["class"] != "ram"]


def rows(dicts, keys):
    for d in dicts:
        yield {k: d[k] for k in keys}


split = [{"function": f["start"], "ranges": f["ranges"],
          "span_start": f["start"], "span_end": f["end"]}
         for f in functions if f["ranges"] > 1]

totals = {
    "functions": len(functions),
    "functions_seeded": sum(1 for f in functions if f["named"] == "seed"),
    "functions_ghidra": sum(1 for f in functions if f["named"] == "ghidra"),
    "data_items": len(data_items),
    "inline_items": len(inline_items),
    "pools": sum(1 for d in inline_items if d["kind"] == "pool"),
    "jumptables": sum(1 for d in inline_items if d["kind"] == "jumptable"),
    "gaps": len(gaps),
    "code_bytes": code_bytes,
    "data_bytes": data_bytes,
    "gap_bytes": gap_bytes,
    "padding_bytes": sum(d["end"] - d["start"] for d in data_items if d["class"] == "padding"),
    "string_bytes": sum(d["end"] - d["start"] for d in data_items if d["class"] == "string"),
    "typed_data_bytes": sum(d["end"] - d["start"] for d in data_items
                            if d["class"] not in ("padding", "untyped")),
    "untyped_data_bytes": sum(d["end"] - d["start"] for d in data_items
                              if d["class"] == "untyped")
                          + sum(d["end"] - d["start"] for d in inline_items),
    "split_functions": len(split),
    "image_bytes": APP_END - APP_BASE,
    "orphan_code_runs": len(orphan_code),
    "orphan_code_bytes": sum(o["end"] - o["start"] for o in orphan_code),
    "overlaps": len(overlaps),
    "uncovered_between_spans": len(uncovered),
    "svc_returns_fixed": svc_fixed,
    "branches_decoded": len(decoded),
}
if code_bytes + data_bytes + gap_bytes != APP_END - APP_BASE:
    raise RuntimeError("partition is not a cover: %d + %d + %d != %d"
                       % (code_bytes, data_bytes, gap_bytes, APP_END - APP_BASE))

emit(os.path.join(out_dir, "items.json"),
     "by abi/ghidra/export_partition.py, do not edit: the app image 0x27000..0xf117c"
     " partitioned by Ghidra's analysis; named=seed is a name this repo supplied,"
     " ghidra is FUN_/DAT_",
     [("totals", totals),
      ("functions", rows(functions, ["start", "end", "name", "named", "bytes", "ranges"])),
      ("data", rows(data_items, ["start", "end", "type", "class", "name"])),
      ("inline", rows(inline_items, ["start", "end", "kind", "function", "type"])),
      ("function_ranges", ({"start": a, "end": b, "function": f} for a, b, f in fn_ranges)),
      ("split_functions", split),
      ("array_rows", rows(array_rows, ["start", "end", "array"])),
      ("gaps", rows(gaps, ["start", "end"])),
      ("orphan_code", rows(orphan_code, ["start", "end"])),
      ("overlaps", rows(overlaps, ["a", "b", "start", "end"])),
      ("instruction_bytes", {"base": APP_BASE, "bits": "".join(
          "%02x" % sum(instruction_bytes[i + k] << k
                       for k in range(8) if i + k < len(instruction_bytes))
          for i in range(0, len(instruction_bytes), 8))})])

emit(os.path.join(out_dir, "references.json"),
     "by abi/ghidra/export_partition.py, do not edit: every reference the analysis"
     " recorded, plus the classification of each literal-pool and data word against"
     " the partition in items.json",
     [("counts", dict(counts, calls=len(calls), external_calls=len(ext_calls),
                      pool_words=len(pool_words), candidates=len(candidates),
                      pool_reads=len(pool_reads),
                      jump_tables=len(tables), unclaimed_jumps=len(unclaimed_jumps),
                      unresolved=len(unresolved))),
      ("calls", rows(calls, ["from", "to", "kind", "mnemonic", "width", "function",
                             "target_is_function", "external"])),
      ("external_calls", rows(ext_calls, ["from", "to", "kind", "mnemonic", "width",
                                          "function", "external"])),
      ("words", rows(words, ["addr", "value", "kind", "class", "item", "offset", "thumb", "resolved", "readers", "uses"])),
      ("pool_reads", rows(pool_reads, ["site", "target", "function", "mnemonic", "width", "use"])),
      ("jump_tables", rows(tables, ["site", "function", "mnemonic", "start", "end",
                                    "stride", "absolute", "entries"])),
      ("unclaimed_jumps", rows(unclaimed_jumps, ["site", "function", "mnemonic",
                                                 "width", "targets"])),
      ("unresolved", rows(unresolved, ["addr", "value", "kind", "class"]))])

with open(os.path.join(out_dir, "symbols.txt"), "w") as fh:
    fh.write("# Generated by abi/ghidra/export_partition.py; ImportSymbolsScript format.\n")
    for f in sorted(functions, key=lambda f: f["start"]):
        fh.write("%s 0x%08x\n" % (f["name"], f["start"]))
    for d in sorted(data_items, key=lambda d: d["start"]):
        if d["name"]:
            fh.write("%s 0x%08x\n" % (d["name"], d["start"]))

println("exported: %s" % totals)
