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

The three run on a fixpoint: a pointer field decided in one round is a
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


def addressish(value):
    """Could this word be an address at all: zero, RAM, or inside the app."""
    return (value == 0 or RAM_BASE <= value < RAM_END
            or APP_BASE <= value < APP_END
            or (value & 1 and APP_BASE <= (value & ~1) < APP_END))


def analyse(items, refs, rows, blob):
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

    seeds = []
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

    decided = {}
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
            if len(refs) < MIN_REFERENCES:
                continue
            for base, stride, limit in segments(refs, end):
                count = (limit - base) // stride
                if count < MIN_RECORDS:
                    continue
                fields = {}
                for at in range(0, stride, 4):
                    column = [word_at(base + k * stride + at)
                              for k in range(count)]
                    fields[at] = (any(column)
                                  and all(addressish(v) for v in column))
                for r in review:
                    if not base <= r["addr"] < base + count * stride:
                        continue
                    if r["addr"] in decided or (r["addr"] - base) % 4:
                        continue
                    at = (r["addr"] - base) % stride
                    note = "0x%x, stride %d, field +%d" % (base, stride, at)
                    if fields[at]:
                        decided[r["addr"]] = ("pointer", "table_field_pointer",
                                              note)
                        trusted.add(r["value"])
                    else:
                        decided[r["addr"]] = ("constant", "table_field_integer",
                                              note)
                    grew += 1
        if not grew:
            break
    return decided
