#!/usr/bin/env python3
"""Decide the words inside the image's untyped runs, from what names the run.

abi/classify_words.py decides a word from its own shape: what its value lands
on, and what the code does with the register it is loaded into. That leaves the
164 KB of untyped gap, where the word is not a literal pool but a cell of a
record table the analysis never typed, and the shape test can only say "the
object this sits in is not an array of words", which is not a decision.

The other end of the evidence is the run's own references: which addresses
inside it anything else in the image holds. Three facts follow from them, and
each is a decision with a name:

  unreachable_run    Nothing reaches the run. The closure below starts at the
                     words the code loads and at the pointers the shape test
                     already proved, and follows every word of every run it
                     reaches whatever that word's class is, so it over-states
                     what is reachable; a run outside it is one no execution
                     can address. Whether a word in such a run holds an address
                     is unobservable, and calling it a constant is what stops it
                     pinning the section it names.

  byte_addressed_run Some address inside the run that the image holds is not
                     word aligned. A pointer into an array of 32-bit words is
                     word aligned by construction, so an unaligned one proves
                     the object is indexed by the byte, and a 4-aligned window
                     over it reads two halves of two fields. This is the unit
                     table at 0xbe4d0 generalised: there the false addresses
                     came from 6-byte records, here from tables of bytes.

  table_field_*      The aligned references into a run are its record starts,
                     so the distance between them is the stride, and a column
                     of that grid either holds an address or zero in every
                     record -- in which case it is a pointer field -- or it
                     does not, in which case no record's word there is a slot.
                     Where a run has too few references to space out a grid,
                     the stride comes from the reader instead:
                     abi/ghidra/word_uses.py records the constant an index is
                     multiplied by before it is added to the address a pool
                     word holds, which is the same number.

  declared_table    abi/hwa10.yaml names the tables whose shape is known from
                    somewhere other than the image's own references -- the WPP
                    and shell dispatch tables, which no literal-pool word names
                    at all -- and gives each one a struct. A word of such a
                    table is typed by the declaration, and the declaration is
                    also a root for the reachability walk, because a table
                    nothing names is exactly what that walk would otherwise
                    call unreachable.

  ordered_column    Where nothing names a run often enough to space out a grid,
                    the grid is read off the run itself: a column whose every
                    entry is an address inside the app and strictly above the
                    entry before it, over at least six records, is a pointer
                    field. A table of pointers to successive objects climbs
                    like that by construction and a table of integers does not,
                    and the smallest stride that produces such a column is the
                    record size, since every multiple of it produces the same
                    column. The 16-byte records at 0xb6628 are the case that
                    matters: their +12 field holds the unaligned byte pointers
                    that prove the 976-byte table at 0xc2450 is byte addressed.

The rules run on a fixpoint: a pointer field decided in one round is a
reference in the next, which is how a table of byte pointers proves that the
blob it indexes is byte addressed.

Only references count that the image really holds: a word already decided a
pointer, a literal-pool or jump-table word (the code loads it, so its value is
an operand), the address an `adr` forms in an instruction, and the pointer
fields this module derives. A word of an undecided run is used for
reachability, where over-stating is safe, and nowhere else.
"""

import bisect
import collections
import math

APP_BASE = 0x27000
APP_END = 0xF117C
RAM_BASE, RAM_END = 0x20000000, 0x20040000

# A record smaller than two words is not a record, and the largest structure
# this image indexes is well under 256 bytes; a stride outside that is the
# accident of two unrelated references, not a table.
MIN_STRIDE, MAX_STRIDE = 8, 256
MIN_REFERENCES = 3
MIN_RECORDS = 3
# Six is where a climb stops being something a table of small integers does by
# accident: the candidate columns tried are a few thousand and six independent
# in-range ascending words is far rarer than that.
MIN_CLIMB = 6
ROUNDS = 8


class Runs(object):
    """The image's untyped runs, indexed for "which run is this address in"."""

    def __init__(self, items):
        self.spans = sorted([(g["start"], g["end"]) for g in items["gaps"]]
                            + [(d["start"], d["end"]) for d in items["data"]])
        self.starts = [s for s, _ in self.spans]

    def of(self, addr):
        i = bisect.bisect_right(self.starts, addr) - 1
        return i if i >= 0 and self.spans[i][1] > addr else None


def reachable(runs, in_run, seeds, code=None, in_code=None):
    """The runs a chain of words can address, over-approximated.

    Every word of a run the chain reaches is followed, whatever it holds, so a
    run this leaves out is one nothing in the image can name, directly or
    through any number of tables.
    """
    live, work, seen = set(), [], set()
    pending = list(seeds)
    while pending:
        value = pending.pop()
        i = runs.of(value)
        if i is not None and i not in live:
            live.add(i)
            work.append(i)
        j = code.of(value) if code is not None else None
        if j is not None and j not in seen:
            seen.add(j)
            for word in in_code[j]:
                pending.append(word["value"])
                pending.append(word["value"] & ~1)
        while work:
            i = work.pop()
            for word in in_run[i]:
                pending.append(word["value"])
                pending.append(word["value"] & ~1)
    return live


def segments(refs, end):
    """Cut a run's references into the stretches that share a stride.

    A gap run is several tables end to end as often as it is one, and a single
    greatest common divisor over all of its references collapses to 4, which
    would make every word its own record and every column a decision. Each
    stretch is the longest prefix of the remaining references whose spacing
    stays a multiple of at least MIN_STRIDE.
    """
    found, k = [], 0
    while k < len(refs):
        j, stride = k + 1, 0
        while j < len(refs):
            step = math.gcd(stride, refs[j] - refs[k])
            if step < MIN_STRIDE or step % 4 or step > MAX_STRIDE:
                break
            stride, j = step, j + 1
        if j - k >= MIN_REFERENCES:
            found.append([refs[k], stride, None])
            k = j
        else:
            k += 1
    for a, b in zip(found, found[1:]):
        a[2] = b[0]
    if found:
        found[-1][2] = end
    return [(base, stride, limit) for base, stride, limit in found]


def reader_strides(rows, runs):
    """{run: {base: strides}} from the address arithmetic of the code that reads it.

    A pool word that names a record table is loaded and then indexed, and the
    walk in abi/ghidra/word_uses.py recorded the multiplier as `stride:N`. That
    is the layout the table's own references would have given if there had been
    three of them.
    """
    found = collections.defaultdict(lambda: collections.defaultdict(set))
    for row in rows:
        if row["class"] != "pointer":
            continue
        base = row["value"] & ~1 if row["signal"].startswith("thumb_") else row["value"]
        i = runs.of(base)
        if i is None or base % 4:
            continue
        for use in row.get("uses") or ():
            if use.startswith("stride:"):
                stride = int(use.split(":")[1])
                if MIN_STRIDE <= stride <= MAX_STRIDE and not stride % 4:
                    found[i][base].add(stride)
    return found


def climbing_grid(start, end, word_at):
    """(stride, {offset: is_pointer}, records) read off a run's own words.

    The smallest stride at which some column climbs through the app's address
    range for MIN_CLIMB records; every multiple of that stride shows the same
    column, which is why the smallest one is the record size.
    """
    base = start + (-start) % 4
    found = []
    for stride in range(MIN_STRIDE, MAX_STRIDE + 4, 4):
        if (end - base) // stride < MIN_CLIMB:
            break
        for at in range(0, stride, 4):
            count, last = 0, -1
            while base + count * stride + at + 4 <= end:
                value = word_at(base + count * stride + at)
                if not APP_BASE <= value < APP_END or value <= last:
                    break
                last, count = value, count + 1
            if count >= MIN_CLIMB:
                found.append((stride, at, count))
        if found:
            break
    if not found:
        return None
    stride = found[0][0]
    records = max(count for _, _, count in found)
    fields = {}
    for at in range(0, stride, 4):
        column = [word_at(base + k * stride + at) for k in range(records)]
        fields[at] = (any(c == at for _, c, _ in found)
                      or (any(column) and all(addressish(v) for v in column)))
    return base, stride, fields, records


def addressish(value):
    """Could this word be an address at all: zero, RAM, or inside the app."""
    return (value == 0 or RAM_BASE <= value < RAM_END
            or APP_BASE <= value < APP_END
            or (value & 1 and APP_BASE <= (value & ~1) < APP_END))


WIDTHS = {"u8": 1, "u16": 2, "u32": 4, "i8": 1, "i16": 2, "i32": 4}


def field_map(fields):
    """{offset: is a 4-byte pointer} for one hwa10.yaml struct."""
    out, at = {}, 0
    for kind, _ in fields:
        kind = str(kind)
        if kind.endswith("*"):
            width, pointer = 4, True
        elif "[" in kind:
            base, _, count = kind.partition("[")
            width, pointer = WIDTHS[base] * int(count.rstrip("]")), False
        else:
            width, pointer = WIDTHS[kind], False
        out[at] = pointer
        at += width
    return out, at


def declared_tables(manifest):
    """(start, stride, count, {offset: is a pointer}) for every declared table."""
    structs = dict((s["name"], s["fields"]) for s in manifest["table_structs"])
    out = []
    for table in manifest["tables"]:
        fields, size = field_map(structs[table["entry"]])
        if size != table["stride"]:
            raise SystemExit("abi/hwa10.yaml: %s is %d bytes but %s has a"
                             " stride of %d" % (table["entry"], size,
                                                table["name"], table["stride"]))
        out.append((table["address"], table["stride"], table["count"], fields,
                    table["name"]))
    return out


def analyse(items, refs, rows, blob, manifest):
    """{word address: (class, signal, note)} for the review words it decides."""
    runs = Runs(items)
    in_run = collections.defaultdict(list)
    for row in rows:
        i = runs.of(row["addr"])
        if i is not None:
            in_run[i].append(row)

    # For reachability only, the words the compiler left between two basic
    # blocks count as well: they are inside a function body, so they are in no
    # run, and a table named only from one would look unreachable.
    code = Runs({"gaps": items["function_ranges"], "data": []})
    in_code = collections.defaultdict(list)
    for row in rows:
        i = code.of(row["addr"])
        if i is not None:
            in_code[i].append(row)

    def word_at(addr):
        return int.from_bytes(blob[addr - APP_BASE:addr - APP_BASE + 4], "little")

    declared = declared_tables(manifest)
    seeds = [start for start, _, _, _, _ in declared]
    for row in rows:
        if row["class"] == "pointer" or row["kind"] in ("pool", "jumptable"):
            seeds.append(row["value"])
            seeds.append(row["value"] & ~1)
    for row in refs["pc_addresses"]:
        seeds.append(row["target"])
    live = reachable(runs, in_run, seeds, code, in_code)

    trusted = set(row["target"] for row in refs["pc_addresses"])
    for row in rows:
        if row["class"] == "pointer" or (row["kind"] in ("pool", "jumptable")
                                         and APP_BASE <= row["value"] < APP_END):
            trusted.add(row["value"])

    hinted = reader_strides(rows, runs)
    decided = {}
    by_addr = dict((row["addr"], row) for row in rows)
    for start, stride, count, fields, name in declared:
        for at in range(start, start + stride * count, 4):
            row = by_addr.get(at)
            if row is None or row["class"] != "review":
                continue
            note = "%s at 0x%x, stride %d, field +%d" % (name, start, stride,
                                                         (at - start) % stride)
            if fields.get((at - start) % stride):
                decided[at] = ("pointer", "declared_table_pointer", note)
                trusted.add(row["value"])
            else:
                decided[at] = ("constant", "declared_table_integer", note)
    for _ in range(ROUNDS):
        held = collections.defaultdict(set)
        for value in trusted:
            i = runs.of(value)
            if i is not None:
                held[i].add(value)
        grew = 0
        for i, (start, end) in enumerate(runs.spans):
            review = [r for r in in_run[i]
                      if r["class"] == "review" and r["addr"] not in decided]
            if not review:
                continue
            if i not in live:
                for r in review:
                    decided[r["addr"]] = ("constant", "unreachable_run", "")
                grew += len(review)
                continue
            refs = sorted(held.get(i, ()))
            unaligned = [v for v in refs if v % 4]
            if unaligned:
                note = ("%d of the %d addresses inside it that the image holds"
                        " are not word aligned, the first 0x%x"
                        % (len(unaligned), len(refs), unaligned[0]))
                for r in review:
                    decided[r["addr"]] = ("constant", "byte_addressed_run", note)
                grew += len(review)
                continue
            grid = [(base, stride, end)
                    for base, strides in hinted.get(i, {}).items()
                    for stride in strides if len(strides) == 1]
            if len(refs) >= MIN_REFERENCES:
                grid = segments(refs, end) or grid
            tables = []
            for base, stride, limit in grid:
                count = (limit - base) // stride
                if count < MIN_RECORDS:
                    continue
                fields = {}
                for at in range(0, stride, 4):
                    column = [word_at(base + k * stride + at)
                              for k in range(count)]
                    fields[at] = (any(column)
                                  and all(addressish(v) for v in column))
                tables.append(("table", base, stride, fields, count))
            if not tables:
                climbed = climbing_grid(start, end, word_at)
                if climbed is not None:
                    tables.append(("ordered",) + climbed)
            for how, base, stride, fields, count in tables:
                for r in review:
                    if not base <= r["addr"] < base + count * stride:
                        continue
                    if r["addr"] in decided or (r["addr"] - base) % 4:
                        continue
                    at = (r["addr"] - base) % stride
                    note = "0x%x, stride %d, field +%d, %d records" % (
                        base, stride, at, count)
                    signal = ("table_field" if how == "table"
                              else "ordered_column")
                    if fields[at]:
                        decided[r["addr"]] = ("pointer", signal + "_pointer",
                                              note)
                        trusted.add(r["value"])
                    else:
                        decided[r["addr"]] = ("constant", signal + "_integer",
                                              note)
                    grew += 1
        if not grew:
            break
    return decided
