#!/usr/bin/env python3
"""How much of the app the maps explain, by count and by bytes.

    python3 abi/coverage.py

Reads the partition export, the manifest and the derived names and prints,
per class, how many functions and data items carry a name that means
something and how many bytes they cover, so progress on the reverse
engineering is a number rather than an impression.
"""
import json
import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out", "ghidra")


def main():
    items = json.load(open(os.path.join(OUT, "items.json")))
    manifest = yaml.safe_load(open(os.path.join(HERE, "hwa10.yaml")))
    derived = yaml.safe_load(open(os.path.join(HERE, "autonames.yaml")))
    matches = yaml.safe_load(open(os.path.join(HERE, "matches.yaml")))
    hand = {f["address"] & ~1 for f in manifest["functions"]}
    derived_by = {}
    for e in derived["functions"]:
        derived_by.setdefault(e["address"] & ~1, e["class"])
    matched = {m["address"] & ~1 for m in matches.get("functions", matches.get("matches", []))}

    kinds = {}
    for f in items["functions"]:
        # A split function's end is its last range's end; its bytes are its own.
        size = f["bytes"] if "bytes" in f else sum(b - a for a, b in f.get("ranges", [(f["start"], f["end"])]))
        a = f["start"]
        if a in hand:
            k = "hand (manifest)"
        elif a in matched:
            k = "library match"
        elif a in derived_by:
            k = "derived: " + derived_by[a]
        elif re.match(r"^(FUN|LAB|block|caseD|thunk)_", f["name"]):
            k = "unnamed"
        else:
            k = "other name"
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
    print("manifest: %d structs, %d tables, %d globals, %d enums"
          % (len(manifest.get("structs", [])) + len(manifest.get("table_structs", [])),
             len(manifest.get("tables", [])), len(manifest.get("globals", [])),
             len(manifest.get("enums", []))))


if __name__ == "__main__":
    main()
