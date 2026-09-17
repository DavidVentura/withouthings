#!/usr/bin/env python3
"""Decide, for every candidate word in the app, whether it holds an address.

    python3 abi/classify_words.py [--export abi/out/ghidra]

Source code contains no addresses, so every absolute address in the image sits
in a slot the compiler reserved for a linker relocation: a literal-pool word, a
word of an absolute jump table, or a word inside a data item. The candidates are
therefore every 32-bit word of the image that is not an instruction and whose
value lands in the app's flash range or the app's RAM range, which the partition
export already enumerates (references.json's `words`).

What is left is the classification, because a constant can look like an address:
0x493e0 is five minutes in milliseconds and also an address inside this image.
The signals, strongest first, are below in DECISIONS; every word records the one
that decided it, and what none of them decides goes on the review list rather
than being guessed at.

abi/words.yaml holds the hand-declared facts: words whose shape says nothing or
says the wrong thing, each with the argument for what it is. They are applied
first and the heuristics never overrule one.

The use signal comes from abi/ghidra/word_uses.py, which follows every value a
pc-relative load defines forward through the function's p-code and through the
callees it is handed to. Only its pointer half is applied: a value that is
branched to or dereferenced is an address, and measured against the structural
signals it never contradicts one. Its constant half is not applied, because in
this image 94 words the structure proves to be string and function pointers are
only ever compared -- a table sentinel test and an integer bound test are the
same instruction -- so "only compared" is not evidence of a number.

Writes abi/out/ghidra/words.json: one row per candidate with its class, its
target and addend, the deciding signal, and a summary with the counts per
bucket. abi/objectify.py turns the `pointer` rows into R_ARM_ABS32.
"""

import argparse
import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)

APP_BASE = 0x27000
APP_END = 0xF117C
RAM_BASE, RAM_END = 0x20000000, 0x20040000

def read_facts(path, blob, functions):
    """abi/words.yaml: the contracts by value and the overrides by address.

    Every override is checked against the image here, so an entry that the
    image no longer agrees with is refused rather than applied to whatever the
    word now holds, and a pointer's target is resolved to an address once.
    """
    spec = yaml.safe_load(open(path))
    reasons = spec["reasons"]
    contracts = {int(v): " ".join(str(why).split())
                 for v, why in spec["contracts"].items()}
    overrides = {}
    for entry in spec["overrides"]:
        addr = entry["address"]
        held = int.from_bytes(blob[addr - APP_BASE:addr - APP_BASE + 4], "little")
        if held != entry["value"]:
            sys.exit("abi/words.yaml claims 0x%x holds 0x%08x; the image holds"
                     " 0x%08x" % (addr, entry["value"], held))
        if entry["why"] not in reasons:
            sys.exit("abi/words.yaml gives 0x%x the reason %s, which it does not"
                     " state" % (addr, entry["why"]))
        if entry["class"] == "pointer":
            target = entry["target"]
            if not isinstance(target, int):
                named = [f["start"] for f in functions if f["name"] == target]
                if len(named) != 1:
                    sys.exit("abi/words.yaml points 0x%x at %s, which the"
                             " partition names %d times" % (addr, target, len(named)))
                target = named[0]
            entry = dict(entry, target=target + entry["addend"])
        elif entry["class"] != "constant":
            sys.exit("abi/words.yaml gives 0x%x the class %s, which is neither"
                     " constant nor pointer" % (addr, entry["class"]))
        if addr in overrides:
            sys.exit("abi/words.yaml declares 0x%x twice" % addr)
        overrides[addr] = entry
    return contracts, overrides


def word_shaped(start, end, blob):
    """Is this object an array of 32-bit words, so that a word in it is a slot?

    A 4-aligned window over a table of 6-byte records reads two halves of two
    fields as one address often enough to matter: this image's unit table at
    0xbe4d0 gave four of them, and relocating those corrupted the table. The
    test is classify_gaps.py's pointer-table test applied to the object rather
    than to the run: every word has to be zero or an address, which no record
    table of mixed integers satisfies.
    """
    if start % 4 or (end - start) % 4 or end - start < 8:
        return False
    for at in range(start, end, 4):
        v = int.from_bytes(blob[at - APP_BASE:at - APP_BASE + 4], "little")
        if v and not (APP_BASE <= (v & ~1) < APP_END or RAM_BASE <= v < RAM_END):
            return False
    return True


class Partition(object):
    """Where an address lands: the starts the export knows and the spans."""

    def __init__(self, items, words):
        self.functions = {f["start"]: f for f in items["functions"]}
        self.starts = {}
        for d in items["data"]:
            self.starts[d["start"]] = d.get("name") or "data"
        for d in items["inline"]:
            self.starts.setdefault(d["start"], d["kind"])
        for r in items["array_rows"]:
            self.starts.setdefault(r["start"], "array_row")
        self.code = sorted((r["start"], r["end"], r["function"])
                           for r in items["function_ranges"])
        self.items = sorted((d["start"], d["end"]) for d in
                            items["data"] + items["inline"])
        self.gaps = sorted((g["start"], g["end"]) for g in items["gaps"])
        self.slots = None
        # A word inside a function body that an instruction reads is data the
        # compiler put between two basic blocks; something pointing at it is a
        # pointer, not a stray constant that happens to land in code.
        # Every candidate inside a function body is data the compiler left
        # between two basic blocks, whether or not a literal load reads it;
        # something pointing at one is a pointer, not a stray constant.
        self.in_body_words = set(w["addr"] for w in words if self.in_code(w["addr"]))

    def is_slot(self, addr):
        return addr in self.slots

    def item_of(self, value):
        """The item `value` lands in, or None: strings are indexed from the
        middle often enough that an interior hit is an ordinary pointer."""
        lo, hi = 0, len(self.items)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.items[mid][0] <= value:
                lo = mid + 1
            else:
                hi = mid
        if lo and self.items[lo - 1][1] > value:
            return self.items[lo - 1][0]
        return None

    def in_code(self, value):
        lo, hi = 0, len(self.code)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.code[mid][0] <= value:
                lo = mid + 1
            else:
                hi = mid
        return lo and self.code[lo - 1][1] > value


# The operations that can only be applied to an address. `offset`, `compare`,
# `scaled`, `stored_value` and `returned` are recorded too, and none of them is
# evidence either way: pointers are compared, stored and returned constantly.
POINTER_USES = frozenset(("call", "load_base", "store_base"))

STRIDES = tuple(range(4, 68, 4))
MIN_RECORDS = 3


def pointer_fields(start, end, blob, plausible):
    """The positions of a record table that hold a pointer in every record.

    An untyped run is usually a table of records mixing integers and pointers,
    and a 4-aligned window over it reads two halves of two fields as often as it
    reads a field: the unit table at 0xbe4d0 has 6-byte records and gave four
    false addresses. What a real pointer field looks like is that every record
    has one at the same offset, so the stride and the offset are what has to be
    found, not the individual word. A field is only claimed when every one of at
    least three records holds either zero or something the partition can name.
    """
    slots = []
    for stride in STRIDES:
        if (end - start) < stride * MIN_RECORDS:
            break
        for offset in range(0, stride, 4):
            at = start + offset + (-(start + offset)) % 4
            positions = list(range(at, end - 3, stride))
            if len(positions) < MIN_RECORDS:
                continue
            values = [int.from_bytes(blob[p - APP_BASE:p - APP_BASE + 4], "little")
                      for p in positions]
            if all(v == 0 for v in values) or not all(plausible(v) for v in values):
                continue
            slots.extend(positions)
    return slots


def slot_objects(items, blob, part):
    """Every address at which a word may be read as a word.

    A literal pool and an absolute jump table are words by construction, so the
    export's own kinds carry them. What is left is the data items and the
    untyped runs, where a word is only a slot if the object is an array of them
    or if it is a pointer field of a record table.
    """

    def plausible(v):
        if v == 0 or RAM_BASE <= v < RAM_END:
            return True
        if v & 1 and (v & ~1) in part.functions:
            return True
        return v in part.starts

    slots = set()
    for d in items["data"]:
        if d["class"] == "string":
            continue
        # Ghidra types a word it resolved a reference from as a pointer, and a
        # word something read as a word as `undefined4`; either way the object
        # is one word and the analysis already said where it starts.
        if (d["type"].endswith("*") or d["type"] == "undefined4") \
                and d["start"] % 4 == 0 and d["end"] - d["start"] == 4:
            slots.add(d["start"])
        elif word_shaped(d["start"], d["end"], blob):
            slots.update(range(d["start"], d["end"] - 3, 4))
        else:
            slots.update(pointer_fields(d["start"], d["end"], blob, plausible))
    for g in items["gaps"]:
        if word_shaped(g["start"], g["end"], blob):
            slots.update(range(g["start"], g["end"] - 3, 4))
        else:
            slots.update(pointer_fields(g["start"], g["end"], blob, plausible))
    return slots


def decide(word, part, contracts, overrides):
    """(class, signal, note) for one candidate word.

    class is "pointer", "constant" or "review"; signal names the rule that
    fired, which is what a later argument about a wrong decision has to attack.
    """
    value, thumb = word["value"], word["thumb"]
    target = value & ~1 if thumb else value
    uses = set(word.get("uses") or ())
    dereferenced = uses & POINTER_USES

    if word["addr"] in overrides:
        # Hand-declared, so no rule below is consulted at all: an override is
        # someone's argument about this exact word, and a heuristic that
        # disagreed with it would only be re-running the shape test the
        # argument was made to answer.
        return overrides[word["addr"]]["class"], "override", \
            overrides[word["addr"]]["why"]
    if value in contracts:
        return "constant", "contract", contracts[value]
    if RAM_BASE <= value < RAM_END:
        # RAM does not move in this step. The word is still an address, and the
        # export carries it so that a later RAM move knows where they are.
        return "constant", "ram", "static or stack address; RAM is not moved yet"
    if not APP_BASE <= (target if thumb else value) < APP_END:
        return "constant", "out_of_range", ""
    if thumb and target in part.functions:
        # Landing exactly on a function entry with bit 0 set survives the
        # stride question: a window over two fields would have to reproduce a
        # function's whole address, not merely land somewhere in the code, and
        # the record tables this image fragments across several items and gaps
        # hold their handlers exactly this way.
        return "pointer", "thumb_function_start", part.functions[target]["name"]
    if word["kind"] in ("data", "gap") and not part.is_slot(word["addr"]):
        # The word is a window over an object whose stride is not four, so what
        # it reads is two halves of two fields, not one value the compiler put
        # there. Out of range it does not matter; in range it would relocate the
        # middle of a record.
        return "review", "not_a_word_slot", word["kind"]
    if thumb and part.in_code(target):
        # A case body, a label in a split function, a resume point: the Thumb
        # bit plus code is as certain as a function start, only the symbol is
        # interior.
        return "pointer", "thumb_interior_code", ""
    # Bit 0 only means Thumb on something that is code. An odd value that is an
    # ordinary address of a byte -- the middle of a string, a record field -- is
    # far more common in this image than a function pointer is, so the plain
    # reading is tried before the masked one is given up on.
    if value in part.starts:
        return "pointer", "item_start", part.starts[value]
    if part.item_of(value) is not None:
        return "pointer", "interior_item", ""
    if value in part.in_body_words:
        return "pointer", "in_body_word", "a pool word inside a function body"
    if dereferenced:
        # The code branches to this value or reads memory through it, so it is
        # an address whatever the bytes around it look like. This fires ahead of
        # the code-range test on purpose: a value inside a function body that is
        # a load base is data the disassembly over-covered, which is the one way
        # the Thumb argument below can be wrong.
        return "pointer", "use_pointer", ",".join(sorted(dereferenced))
    if part.in_code(value):
        # This image is Thumb throughout (9 movt, none building an address, and
        # no ARM code), so an even address is not a way to name an instruction.
        # A word holding one is a number that collided with the code's range.
        return "constant", "interior_code_without_thumb", ""
    if value in part.functions:
        return "review", "function_start_without_thumb", part.functions[value]["name"]

    # What is left lands in one of the untyped runs: a record table's own row,
    # or a number, and nothing the code did with it settles which.
    return "review", "lands_in_untyped_run", ",".join(sorted(uses))


def owning_item(part, target):
    """(symbol address, addend) for a pointer, against the item it lands in."""
    if target in part.starts or target in part.functions:
        return target, 0
    lo, hi = 0, len(part.code)
    while lo < hi:
        mid = (lo + hi) // 2
        if part.code[mid][0] <= target:
            lo = mid + 1
        else:
            hi = mid
    if lo and part.code[lo - 1][1] > target:
        return part.code[lo - 1][2], target - part.code[lo - 1][2]
    item = part.item_of(target)
    if item is not None:
        return item, target - item
    return target, 0


def classify(items, refs, blob, contracts, overrides):
    outside = sorted(set(overrides) - set(w["addr"] for w in refs["words"]))
    if outside:
        sys.exit("abi/words.yaml declares %d addresses the candidate set does"
                 " not offer, starting at 0x%x" % (len(outside), outside[0]))
    part = Partition(items, refs["words"])
    part.slots = slot_objects(items, blob, part)
    rows, buckets, review = [], {}, {}
    for word in refs["words"]:
        klass, signal, note = decide(word, part, contracts, overrides)
        # `thumb_*` are the only signals that read bit 0 as the Thumb bit; for
        # every other one the value is the address, odd or not.
        target = (overrides[word["addr"]]["target"] if signal == "override"
                  and klass == "pointer"
                  else word["value"] & ~1 if signal.startswith("thumb_")
                  else word["value"])
        item, addend = (owning_item(part, target) if klass == "pointer"
                        else (0, 0))
        rows.append({"addr": word["addr"], "value": word["value"],
                     "kind": word["kind"], "class": klass, "signal": signal,
                     "note": note, "target": target if klass == "pointer" else 0,
                     "item": item, "addend": addend, "thumb": word["thumb"],
                     "in_code": klass == "pointer" and part.in_code(target),
                     "thumb_target": signal.startswith("thumb_")
                                     or (klass == "pointer" and target % 2 == 1),
                     "uses": word.get("uses") or []})
        key = "%s:%s:%s" % (word["kind"], klass, signal)
        buckets[key] = buckets.get(key, 0) + 1
        if klass == "review":
            review.setdefault(signal, []).append(rows[-1])
    return rows, buckets, review


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--facts", default=os.path.join(HERE, "words.yaml"))
    args = ap.parse_args()
    with open(os.path.join(args.export, "items.json")) as fh:
        items = json.load(fh)
    with open(os.path.join(args.export, "references.json")) as fh:
        refs = json.load(fh)
    with open(os.path.join(args.export, "word_uses.json")) as fh:
        uses = {r["addr"]: sorted(r["uses"]) for r in json.load(fh)["uses"]}
    for word in refs["words"]:
        word["uses"] = uses.get(word["addr"], [])

    with open(args.image, "rb") as fh:
        blob = fh.read()
    contracts, overrides = read_facts(args.facts, blob, items["functions"])
    rows, buckets, review = classify(items, refs, blob, contracts, overrides)
    pointers = [r for r in rows if r["class"] == "pointer"]
    summary = {
        "candidates": len(rows),
        "pointers": len(pointers),
        "pointers_into_code": sum(1 for r in pointers if r["in_code"]),
        "constants": sum(1 for r in rows if r["class"] == "constant"),
        "review": sum(1 for r in rows if r["class"] == "review"),
        "overrides": len(overrides),
        "buckets": buckets,
        "review_buckets": {k: len(v) for k, v in sorted(review.items())},
    }
    path = os.path.join(args.export, "words.json")
    with open(path, "w") as fh:
        fh.write('{"_generated": %s,\n"summary": %s,\n"words": ['
                 % (json.dumps("by abi/classify_words.py, do not edit: pointer"
                               " versus constant for every candidate word"),
                    json.dumps(summary)))
        for i, row in enumerate(rows):
            fh.write("%s\n%s" % ("," if i else "", json.dumps(row)))
        fh.write("]}\n")

    print("%s: %d candidates, %d pointers (%d into code), %d constants, %d to"
          " review"
          % (path, summary["candidates"], summary["pointers"],
             summary["pointers_into_code"], summary["constants"], summary["review"]))
    for key in sorted(buckets):
        print("  %-48s %d" % (key, buckets[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
