#!/usr/bin/env python3
"""Collect every name the repo already knows into one JSON seed for Ghidra.

    python3 abi/ghidra/seed.py [-o abi/out/ghidra/seed.json]

abi/ghidra/analyze.sh runs this before analyzeHeadless; the Ghidra pre-script
applies the result. Sources, and what each is trusted to say:

  abi/hwa10.yaml     functions (prototype known), globals (typed) and tables
                     (entry struct and stride), the only place strides come from
  abi/matches.yaml   library bodies recovered by abi/match.py: function starts
  abi/autonames.yaml mechanically derived names: function starts
  symbols.txt        the prose map: names only, no kind, because it mixes code,
                     flash data and RAM addresses in one list

Nothing here guesses a kind: a symbols.txt address becomes a label and lets the
analysis decide whether a function starts there.
"""

import argparse
import json
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ABI = os.path.dirname(HERE)
SIM = os.path.dirname(ABI)
APP_BASE = 0x27000
APP_END = 0xF117C

SYM_LINE = re.compile(r"^(0x[0-9a-fA-F]+)\s+([A-Za-z_][A-Za-z0-9_]*)")


def in_app(addr):
    return APP_BASE <= addr < APP_END


def load(name):
    with open(os.path.join(ABI, name)) as fh:
        return yaml.safe_load(fh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.join(ABI, "out", "ghidra", "seed.json"))
    args = ap.parse_args()

    functions, labels, data, tables = {}, {}, [], []

    manifest = load("hwa10.yaml")
    strides = {t["name"]: t for t in manifest.get("table_structs", [])}
    # The seed is a map of addresses and widths, not of prototypes: a row field
    # declared as one of the manifest's function-pointer typedefs is the same
    # four-byte code pointer the scripts downstream read as `void *`, and only
    # abi/gen.py has any use for the prototype.
    typedefs = {t["name"] for t in manifest.get("typedefs", [])}

    def erase(fields):
        return [["void *" if str(t) in typedefs else t, n] for t, n in fields]

    for fn in manifest["functions"]:
        addr = fn["address"] & ~1
        if in_app(addr):
            functions[addr] = {"name": fn["name"], "source": "hwa10"}
    for g in manifest["globals"]:
        if in_app(g["address"]):
            data.append({"address": g["address"], "name": g["name"],
                         "type": g["type"], "source": "hwa10"})
    for t in manifest["tables"]:
        if not in_app(t["address"]):
            continue
        entry = strides.get(t["entry"])
        tables.append({"address": t["address"], "name": t["name"],
                       "entry": t["entry"], "stride": t["stride"],
                       "count": t["count"],
                       "fields": erase(entry["fields"]) if entry else None,
                       "source": "hwa10"})

    # An address a manifest entry declares it corrects is not a function start,
    # so the generated map must not seed one there as well: two entries six
    # bytes apart would make the body's own prologue a separate item.
    corrected = set(fn["corrects"] & ~1 for fn in manifest["functions"]
                    if "corrects" in fn)
    for src in ("matches.yaml", "autonames.yaml"):
        doc = load(src)
        for fn in doc.get("functions", []):
            addr = fn["address"] & ~1
            if not in_app(addr) or addr in functions or addr in corrected:
                continue
            functions[addr] = {"name": fn["name"], "source": src.split(".")[0]}

    with open(os.path.join(SIM, "symbols.txt")) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            m = SYM_LINE.match(line.strip())
            if not m:
                continue
            addr = int(m.group(1), 16) & ~1
            if not in_app(addr) or addr in functions:
                continue
            labels.setdefault(addr, {"name": m.group(2), "source": "symbols"})

    out = {"app_base": APP_BASE, "app_end": APP_END,
           "functions": [dict(address=a, **v) for a, v in sorted(functions.items())],
           "labels": [dict(address=a, **v) for a, v in sorted(labels.items())],
           "data": sorted(data, key=lambda d: d["address"]),
           "tables": sorted(tables, key=lambda t: t["address"])}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print("seed: %d functions, %d labels, %d globals, %d tables -> %s"
          % (len(out["functions"]), len(out["labels"]), len(out["data"]),
             len(out["tables"]), args.out), file=sys.stderr)


if __name__ == "__main__":
    main()
