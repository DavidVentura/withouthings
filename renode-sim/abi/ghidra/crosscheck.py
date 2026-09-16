#!/usr/bin/env python3
"""Check Ghidra's partition against what the repo already knows.

    python3 abi/ghidra/crosscheck.py [--out abi/out/ghidra]

Three independent checks, each reported as counts plus the exceptions:

  * every `bl` target in out/appl.dis is a Ghidra function start. A target that
    is not is either a Ghidra miss or a `bl` the linear sweep read out of data,
    and which one it is follows from where the target lands in the partition.
  * every named address in symbols.txt, hwa10.yaml, matches.yaml and
    autonames.yaml coincides with a function or item start.
  * abi/boundary.yaml's per-target call and tail-call counts equal the calls and
    jumps Ghidra recorded into the same targets.
"""

import argparse
import bisect
import collections
import json
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ABI = os.path.dirname(HERE)
SIM = os.path.dirname(ABI)
sys.path.insert(0, HERE)

import seed as seedmod

BL = re.compile(r"^\s*([0-9a-f]+):.*\tbl\t(0x[0-9a-f]+)")


def load(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ABI, "out", "ghidra"))
    ap.add_argument("--dis", default=os.path.join(SIM, "out", "appl.dis"))
    args = ap.parse_args()

    items = load(os.path.join(args.out, "items.yaml"))
    refs = load(os.path.join(args.out, "references.yaml"))

    starts = {f["start"]: f for f in items["functions"]}
    data_starts = {d["start"] for d in items["data"]}
    inline_starts = {d["start"] for d in (items["inline"] or [])}
    data_spans = sorted((d["start"], d["end"]) for d in items["data"] + (items["inline"] or []))
    orphan = sorted((o["start"], o["end"]) for o in (items["orphan_code"] or []))
    bodies = sorted((r["start"], r["end"]) for r in items["function_ranges"])
    gaps = sorted((g["start"], g["end"]) for g in (items["gaps"] or []))

    def inside(spans, v):
        i = bisect.bisect_right(spans, (v, 1 << 40))
        return spans[i - 1][0] if i and spans[i - 1][0] <= v < spans[i - 1][1] else None

    bl_targets = set()
    with open(args.dis) as fh:
        for line in fh:
            m = BL.match(line)
            if m:
                t = int(m.group(2), 16)
                if seedmod.in_app(t):
                    bl_targets.add(t)

    missed = sorted(t for t in bl_targets if t not in starts)
    range_starts = {r["start"] for r in items["function_ranges"]}
    where = collections.Counter()
    for t in missed:
        if t in range_starts:
            where["second body range of a function"] += 1
            continue
        if inside(bodies, t) is not None:
            where["inside a function"] += 1
        elif inside(orphan, t) is not None:
            where["in code with no function"] += 1
        elif inside(data_spans, t) is not None:
            where["in data"] += 1
        elif inside(gaps, t) is not None:
            where["in a gap"] += 1
        else:
            where["elsewhere"] += 1

    def placement(a):
        for spans, label in ((bodies, "interior of function range 0x%x"),
                             (orphan, "inside code with no function at 0x%x"),
                             (data_spans, "inside the data item at 0x%x"),
                             (gaps, "inside the gap at 0x%x")):
            hit = inside(spans, a)
            if hit is not None:
                return label % hit
        return "unclaimed"

    with open(os.path.join(args.out, "seed.json")) as fh:
        seed = json.load(fh)
    named_bad = []
    for group in ("functions", "labels", "data", "tables"):
        for e in seed[group]:
            a = e["address"]
            if a in starts or a in data_starts or a in inline_starts:
                continue
            named_bad.append((group, e["name"], a, placement(a)))

    boundary = load(os.path.join(ABI, "boundary.yaml"))
    # boundary.yaml counts app->library sites only; a reference whose source is
    # itself in a library range is the library calling itself.
    lib = sorted(tuple(r) for r in boundary["library_ranges"])
    calls = collections.Counter()
    jumps = collections.Counter()
    for c in refs["calls"] or []:
        if inside(lib, c["from"]) is None:
            (calls if c["kind"] == "call" else jumps)[c["to"]] += 1
    bnd_bad = []
    for e in boundary["app_to_lib"]:
        a = e["addr"]
        # Ghidra turns a tail call into a call reference, so only the total is
        # comparable with boundary.yaml's split of sites and tail sites.
        if calls[a] + jumps[a] != e["sites"] + e.get("tail_sites", 0):
            bnd_bad.append((e["symbol"], a, e["sites"] + e.get("tail_sites", 0),
                            calls[a] + jumps[a], e.get("tail_sites", 0), jumps[a]))
        if a not in starts:
            bnd_bad.append((e["symbol"], a, "not a Ghidra function start", "", "", ""))

    t = items["totals"]
    print("functions %d (seeded %d, FUN_ %d); bl targets %d, of which not a function start %d %s"
          % (t["functions"], t["functions_seeded"], t["functions_ghidra"],
             len(bl_targets), len(missed), dict(where)))
    print("bytes: code %d, data %d, gap %d of %d; overlaps %d, orphan code runs %d"
          % (t["code_bytes"], t["data_bytes"], t["gap_bytes"], t["image_bytes"],
             t["overlaps"], t["orphan_code_runs"]))
    print("named addresses not on an item start: %d" % len(named_bad))
    for row in named_bad[:40]:
        print("   %-10s %-32s 0x%x  %s" % row)
    print("boundary.yaml disagreements: %d" % len(bnd_bad))
    for row in bnd_bad[:40]:
        print("   %-28s 0x%x boundary %s vs ghidra %s (tail %s vs jumps %s)" % row)
    print("reference classes: %s" % {k: v for k, v in sorted(refs["counts"].items())})
    for t_ in missed[:40]:
        print("   bl target not a function start: 0x%x" % t_)


if __name__ == "__main__":
    main()
