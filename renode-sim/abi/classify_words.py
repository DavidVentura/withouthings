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

Writes abi/out/ghidra/words.json: one row per candidate with its class, its
target and addend, the deciding signal, and a summary with the counts per
bucket. abi/objectify.py turns the `pointer` rows into R_ARM_ABS32.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)

APP_BASE = 0x27000
APP_END = 0xF117C
RAM_BASE, RAM_END = 0x20000000, 0x20040000

# Values in the app's flash range that name something by contract rather than by
# the linker having put it there, so they are constants and a move must not
# touch them. Each is argued for where it is used, not assumed:
#   0x27000  the SoftDevice's application base. It is the size field of the
#            MBR/SoftDevice structure (FIRMWARE.md), the address the bootloader
#            copies the appl part to, and what the app hands
#            sd_softdevice_vector_table_base_set. The vector table is pinned
#            there anyway, so a word holding it cannot go stale either way.
#   0xf117c  the app image end, which is a length in disguise.
CONTRACT = {
    APP_BASE: "softdevice application base",
    APP_END: "app image end",
}


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
        # A word inside a function body that an instruction reads is data the
        # compiler put between two basic blocks; something pointing at it is a
        # pointer, not a stray constant that happens to land in code.
        # Every candidate inside a function body is data the compiler left
        # between two basic blocks, whether or not a literal load reads it;
        # something pointing at one is a pointer, not a stray constant.
        self.in_body_words = set(w["addr"] for w in words if self.in_code(w["addr"]))

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


def decide(word, part):
    """(class, signal, note) for one candidate word.

    class is "pointer", "constant" or "review"; signal names the rule that
    fired, which is what a later argument about a wrong decision has to attack.
    """
    value, thumb = word["value"], word["thumb"]
    target = value & ~1 if thumb else value
    uses = set(word.get("uses") or ())

    if value in CONTRACT:
        return "constant", "contract", CONTRACT[value]
    if RAM_BASE <= value < RAM_END:
        # RAM does not move in this step. The word is still an address, and the
        # export carries it so that a later RAM move knows where they are.
        return "constant", "ram", "static or stack address; RAM is not moved yet"
    if not APP_BASE <= (target if thumb else value) < APP_END:
        return "constant", "out_of_range", ""

    if thumb and target in part.functions:
        return "pointer", "thumb_function_start", part.functions[target]["name"]
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
    if part.in_code(value):
        if uses & {"call", "memory"}:
            # An even address inside a function body that the code dereferences
            # or branches to: data the compiler left between two basic blocks
            # and no instruction in the image reads with a literal load, so the
            # pool-word scan did not find it.
            return "pointer", "in_body_word", ",".join(sorted(uses))
        # This image is Thumb throughout (9 movt, none building an address, and
        # no ARM code), so an even address is not a way to name an instruction.
        # A word holding one is a number that collided with the code's range.
        return "constant", "interior_code_without_thumb", ""
    if value in part.functions:
        return "review", "function_start_without_thumb", part.functions[value]["name"]

    # What is left lands in one of the untyped runs: a record table's own row,
    # or a number. The use of the register decides it where there is one, and
    # only pool words have one.
    if "call" in uses or "memory" in uses:
        return "pointer", "register_use", ",".join(sorted(uses))
    if uses and uses <= {"compare"}:
        return "constant", "register_use", ",".join(sorted(uses))
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


def classify(items, refs):
    part = Partition(items, refs["words"])
    rows, buckets, review = [], {}, {}
    for word in refs["words"]:
        klass, signal, note = decide(word, part)
        # `thumb_*` are the only signals that read bit 0 as the Thumb bit; for
        # every other one the value is the address, odd or not.
        target = (word["value"] & ~1 if signal.startswith("thumb_")
                  else word["value"])
        item, addend = (owning_item(part, target) if klass == "pointer"
                        else (0, 0))
        rows.append({"addr": word["addr"], "value": word["value"],
                     "kind": word["kind"], "class": klass, "signal": signal,
                     "note": note, "target": target if klass == "pointer" else 0,
                     "item": item, "addend": addend, "thumb": word["thumb"],
                     "in_code": klass == "pointer" and part.in_code(target),
                     "thumb_target": signal.startswith("thumb_"),
                     "uses": word.get("uses") or []})
        key = "%s:%s:%s" % (word["kind"], klass, signal)
        buckets[key] = buckets.get(key, 0) + 1
        if klass == "review":
            review.setdefault(signal, []).append(rows[-1])
    return rows, buckets, review


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    args = ap.parse_args()
    with open(os.path.join(args.export, "items.json")) as fh:
        items = json.load(fh)
    with open(os.path.join(args.export, "references.json")) as fh:
        refs = json.load(fh)

    rows, buckets, review = classify(items, refs)
    pointers = [r for r in rows if r["class"] == "pointer"]
    summary = {
        "candidates": len(rows),
        "pointers": len(pointers),
        "pointers_into_code": sum(1 for r in pointers if r["in_code"]),
        "constants": sum(1 for r in rows if r["class"] == "constant"),
        "review": sum(1 for r in rows if r["class"] == "review"),
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

    print("%s: %d candidates, %d pointers (%d into code), %d constants, %d to review"
          % (path, summary["candidates"], summary["pointers"],
             summary["pointers_into_code"], summary["constants"], summary["review"]))
    for key in sorted(buckets):
        print("  %-48s %d" % (key, buckets[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
