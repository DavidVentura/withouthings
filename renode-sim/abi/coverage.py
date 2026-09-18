#!/usr/bin/env python3
"""How much of the app the maps explain, by count and by bytes.

    python3 abi/coverage.py

Reads the partition export and abi/symbols.yaml and prints, per class, how
many functions and data items carry a name that means something and how many
bytes they cover, so progress on the reverse engineering is a number rather
than an impression. The shapes the map's globals and tables are declared with
are C, so they are counted out of abi/include/withings/*.h through
abi/shapes.py.
"""
import json
import os
import re

import shapes
import symbols as symmap

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out", "ghidra")

# The class a map entry carries, as the report names it. Everything else is a
# derivation and is reported under its own rule's name.
LABELS = {symmap.HAND: "hand (manifest)", "match": "library match"}


def main():
    items = json.load(open(os.path.join(OUT, "items.json")))
    smap = symmap.load()
    # A label is a name for an address, not a claim that a function starts
    # there, so it explains nothing about a function and is left out.
    named = {s.address: LABELS.get(s.klass, "derived: " + s.klass)
             for s in smap.of_kind("function")}

    kinds = {}
    for f in items["functions"]:
        # A split function's end is its last range's end; its bytes are its own.
        size = f["bytes"] if "bytes" in f else sum(b - a for a, b in f.get("ranges", [(f["start"], f["end"])]))
        k = named.get(f["start"])
        if k is None:
            k = ("unnamed" if re.match(r"^(FUN|LAB|block|caseD|thunk)_", f["name"])
                 else "other name")
        n, b = kinds.get(k, (0, 0))
        kinds[k] = (n + 1, b + size)
    total_n = sum(n for n, _ in kinds.values())
    total_b = sum(b for _, b in kinds.values())
    print("functions: %d, %d bytes" % (total_n, total_b))
    for k, (n, b) in sorted(kinds.items(), key=lambda kv: -kv[1][1]):
        print("  %-26s %5d fns %4.1f%%  %7d B %4.1f%%"
              % (k, n, 100.0 * n / total_n, b, 100.0 * b / total_b))

    data = items["data"]
    typed = [d for d in data if d.get("class") not in (None, "untyped", "padding")]
    untyped = [d for d in data if d.get("class") == "untyped"]
    gaps = items["gaps"]
    dbytes = lambda xs: sum(x["end"] - x["start"] for x in xs)
    print("data: typed %d items / %d B, untyped %d / %d B, gap runs %d / %d B"
          % (len(typed), dbytes(typed), len(untyped), dbytes(untyped), len(gaps), dbytes(gaps)))
    types = shapes.load()
    print("map: %d structs, %d tables, %d globals, %d enums"
          % (len(types.structs), len(smap.of_kind("table")),
             len(smap.of_kind("global")), len(types.enums)))


if __name__ == "__main__":
    main()
