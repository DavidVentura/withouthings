# Ghidra post-script: export the analysed app as a partition plus its references.
#
# Run by abi/ghidra/analyze.sh, which passes the output directory as the one
# argument. Writes items.yaml, references.yaml and symbols.txt there.
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
    """YAML with one flow mapping per line: diffable, and yaml.safe_load reads it."""
    with open(path, "w") as fh:
        fh.write(header)
        for name, rows in sections:
            if isinstance(rows, dict):
                fh.write("%s:\n" % name)
                for k, v in rows.items():
                    fh.write("  %s: %s\n" % (k, json.dumps(v) if isinstance(v, (dict, list)) else v))
                continue
            fh.write("%s:\n" % name)
            for row in rows:
                if isinstance(row, dict):
                    row = [(k, "0x%x" % v if isinstance(v, int) else v) for k, v in row.items()]
                fh.write("  - {%s}\n" % ", ".join("%s: %s" % kv for kv in row))


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
pool_words, pool_owner, jump_owner = set(), {}, {}
for instr in listing.getInstructions(app_set, True):
    owner = fm.getFunctionContaining(instr.getMinAddress())
    owner = owner.getEntryPoint().getOffset() if owner is not None else None
    mnem = instr.getMnemonicString().lower()
    is_load = mnem.startswith(("ldr", "vldr"))
    is_jump = instr.getFlowType().isJump() and instr.getFlowType().isComputed()
    if not (is_load or is_jump):
        continue
    for ref in instr.getReferencesFrom():
        to = ref.getToAddress()
        off = to.getOffset()
        if not in_app(off) or listing.getInstructionAt(to) is not None:
            continue
        if is_load and off % 4 == 0:
            pool_words.add(off)
            pool_owner.setdefault(off, owner)
        if is_jump:
            jump_owner.setdefault(off, owner)


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

for cu in listing.getCodeUnits(app_set, True):
    start = cu.getMinAddress().getOffset()
    length = cu.getLength()
    fn = fm.getFunctionContaining(cu.getMinAddress())
    if isinstance(cu, Instruction):
        code_bytes += length
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
# The relocation a site needs follows from the instruction, not from Ghidra's
# reference type: Ghidra records a `b.w` tail call as a call like any other.
calls, ext_calls = [], []
for src in rm.getReferenceSourceIterator(app_set, True):
    instr = listing.getInstructionAt(src)
    if instr is None:
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

table_words = []
for d in data_items:
    for off in range(d["start"] + (-d["start"]) % 4, d["end"] - 3, 4):
        if off not in pool_words:
            table_words.append(off)


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


words, counts = [], {}
for addr_off, kind in [(a, "pool") for a in sorted(pool_words)] + \
                      [(a, "data") for a in table_words]:
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
                      "thumb": thumb, "resolved": resolved})

unresolved = [w for w in words if not w["resolved"] and w["class"] != "ram"]


def rows(dicts, keys):
    for d in dicts:
        yield [(k, json.dumps(d[k]) if isinstance(d[k], (str, bool))
                else hexs(d[k]) if k not in ("bytes", "ranges", "offset")
                else d[k]) for k in keys]


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
}
if code_bytes + data_bytes + gap_bytes != APP_END - APP_BASE:
    raise RuntimeError("partition is not a cover: %d + %d + %d != %d"
                       % (code_bytes, data_bytes, gap_bytes, APP_END - APP_BASE))

emit(os.path.join(out_dir, "items.yaml"),
     "# Generated by abi/ghidra/export_partition.py -- do not edit by hand.\n"
     "# The app image 0x27000..0xf117c partitioned by Ghidra's analysis.\n"
     "# `named: seed` is a name this repo supplied, `ghidra` is FUN_/DAT_.\n",
     [("totals", totals),
      ("functions", rows(functions, ["start", "end", "name", "named", "bytes", "ranges"])),
      ("data", rows(data_items, ["start", "end", "type", "class", "name"])),
      ("inline", rows(inline_items, ["start", "end", "kind", "function", "type"])),
      ("function_ranges", ({"start": a, "end": b, "function": f} for a, b, f in fn_ranges)),
      ("split_functions", split),
      ("array_rows", rows(array_rows, ["start", "end", "array"])),
      ("gaps", rows(gaps, ["start", "end"])),
      ("orphan_code", rows(orphan_code, ["start", "end"])),
      ("overlaps", rows(overlaps, ["a", "b", "start", "end"]))])

emit(os.path.join(out_dir, "references.yaml"),
     "# Generated by abi/ghidra/export_partition.py -- do not edit by hand.\n"
     "# Every reference the analysis recorded, plus the classification of each\n"
     "# literal-pool and data word against the partition in items.yaml.\n",
     [("counts", dict(counts, calls=len(calls), external_calls=len(ext_calls),
                      pool_words=len(pool_words), data_words=len(table_words),
                      unresolved=len(unresolved))),
      ("calls", rows(calls, ["from", "to", "kind", "mnemonic", "width", "function",
                             "target_is_function", "external"])),
      ("external_calls", rows(ext_calls, ["from", "to", "kind", "mnemonic", "width",
                                          "function", "external"])),
      ("words", rows(words, ["addr", "value", "kind", "class", "item", "offset", "thumb", "resolved"])),
      ("unresolved", rows(unresolved, ["addr", "value", "kind", "class"]))])

with open(os.path.join(out_dir, "symbols.txt"), "w") as fh:
    fh.write("# Generated by abi/ghidra/export_partition.py; ImportSymbolsScript format.\n")
    for f in sorted(functions, key=lambda f: f["start"]):
        fh.write("%s 0x%08x\n" % (f["name"], f["start"]))
    for d in sorted(data_items, key=lambda d: d["start"]):
        if d["name"]:
            fh.write("%s 0x%08x\n" % (d["name"], d["start"]))

println("exported: %s" % totals)
