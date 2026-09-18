#!/usr/bin/env python3
"""Collect every name the repo already knows into one JSON seed for Ghidra.

    python3 abi/ghidra/seed.py [-o abi/out/ghidra/seed.json]

abi/ghidra/analyze.sh runs this before analyzeHeadless; the Ghidra pre-script
applies the result.

abi/symbols.yaml is where every name comes from and its `kind` is what each is
trusted to say: a function start, a typed global, a table, or -- for the prose
entries, which mix code, flash data and RAM in one list -- a bare label that
lets the analysis decide whether a function starts there. The shape a global or
a table's rows are laid out in is C, so it comes from the hand headers through
abi/shapes.py, which is also the only place a stride can come from: a stride is
`sizeof` the row struct.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ABI = os.path.dirname(HERE)
sys.path.insert(0, ABI)

import symbols as S      # noqa: E402
import shapes as T        # noqa: E402

APP_BASE = S.APP_BASE
APP_END = S.APP_END


def in_app(addr):
    return APP_BASE <= addr < APP_END


def shape(field):
    """One struct member as the seed carries it: a width and what it is.

    The seed is a map of addresses and widths, not of prototypes: a row field
    declared as a function-pointer typedef is the same four-byte code pointer
    the scripts downstream read as any other, so the C spelling is dropped here
    and only the shape survives.
    """
    return {"name": field.name, "offset": field.offset, "size": field.size,
            "kind": field.kind, "count": field.count, "unit": field.unit,
            "points_to": field.points_to}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.join(ABI, "out", "ghidra", "seed.json"))
    args = ap.parse_args()

    smap = S.load()
    types = T.load()
    functions, labels, data, tables = {}, {}, [], []
    corrected = smap.corrected()

    for sym in smap.symbols:
        if not in_app(sym.address) or sym.address in corrected:
            continue
        if sym.kind == "function":
            functions[sym.address] = {"name": sym.name, "source": sym.klass}
        elif sym.kind == "global":
            obj = types.object(sym.name)
            data.append({"address": sym.address, "name": sym.name,
                         "size": obj.size, "type": obj.ctype, "kind": obj.kind,
                         "element": obj.element, "count": obj.count,
                         "source": sym.klass})
        elif sym.kind == "table":
            obj = types.object(sym.name)
            row = types.struct(obj.element)
            tables.append({"address": sym.address, "name": sym.name,
                           "entry": obj.element, "stride": row.size,
                           "count": obj.count,
                           "fields": [shape(f) for f in row.fields],
                           "source": sym.klass})
    # A second name for an address is a label, and only where the address is
    # not already a function: Ghidra takes the function's own name from the
    # entry, and a label on top of it would only rename the same item.
    for sym in smap.symbols:
        if not in_app(sym.address) or sym.address in functions:
            continue
        for name in ([sym.name] if sym.kind == "label" else []) + sym.aliases:
            labels.setdefault(sym.address, {"name": name, "source": sym.klass})

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
