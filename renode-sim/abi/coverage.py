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
import collections
import json
import os
import re

import accesses as accessindex
import peripherals
import shapes
import symbols as symmap

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out", "ghidra")

# The class a map entry carries, as the report names it. Everything else is a
# derivation and is reported under its own rule's name.
LABELS = {symmap.HAND: "hand (manifest)", "match": "library match"}

# A vendor component's entries are named and its interiors are not, and the
# difference is the whole point of the two buckets: the interior bytes are
# explained -- they belong to a library the firmware did not write and reaches
# only through the entries -- without any claim about what each body does.
VENDOR_ENTRY, VENDOR_INTERIOR = "vendor entry", "vendor interior"

# The placeholder names the partition gives a body nothing names, and the
# depth past which abi/ghidra/mark_review.py stops counting steps up the
# call graph; both are that script's, so the two reports agree.
UNNAMED = re.compile(r"^(FUN|LAB|block|caseD|thunk|sliver)_")
MAX_DEPTH = 5


def main():
    items = json.load(open(os.path.join(OUT, "items.json")))
    smap = symmap.load()
    # A label is a name for an address, not a claim that a function starts
    # there, so it explains nothing about a function and is left out.
    # An attribution places an address without naming it, so it explains
    # nothing: the body is unnamed here and stays on the worklist. It is
    # counted separately because knowing which module an unnamed body is in is
    # still a step, and the number says how much of the residue has taken it.
    attributed = {s.address for s in smap.attributions()}
    named = {}
    for s in smap.of_kind("function"):
        if s.klass == "vendor":
            named[s.address] = (VENDOR_INTERIOR if s.name.startswith(s.component)
                                else VENDOR_ENTRY)
        else:
            named[s.address] = LABELS.get(s.klass, "derived: " + s.klass)

    kinds = {}
    for f in items["functions"]:
        # A split function's end is its last range's end; its bytes are its own.
        size = f["bytes"] if "bytes" in f else sum(b - a for a, b in f.get("ranges", [(f["start"], f["end"])]))
        k = named.get(f["start"])
        if k is None:
            k = "unnamed" if UNNAMED.match(f["name"]) else "other name"
        n, b = kinds.get(k, (0, 0))
        kinds[k] = (n + 1, b + size)
    unnamed_attributed = sum(
        1 for f in items["functions"]
        if is_unnamed(f, named) and f["start"] in attributed)
    total_n = sum(n for n, _ in kinds.values())
    total_b = sum(b for _, b in kinds.values())
    print("functions: %d, %d bytes" % (total_n, total_b))
    for k, (n, b) in sorted(kinds.items(), key=lambda kv: -kv[1][1]):
        print("  %-26s %5d fns %4.1f%%  %7d B %4.1f%%"
              % (k, n, 100.0 * n / total_n, b, 100.0 * b / total_b))

    print("  of the unnamed, %d carry an attribution: a module and what put"
          " them in it, and no name" % unnamed_attributed)

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

    # Static RAM is where the names still run out, so it is counted on its
    # own: a word with a name and, of those, the bytes an extent or a
    # declaration establishes, which is what bounds a RAM item.
    ram = [x for x in smap.of_kind("global", "table")
           if 0x20000000 <= x.address < 0x20040000]
    print("ram: %d named entries, %d of them with an established extent,"
          " %d bytes" % (len(ram), sum(1 for x in ram if x.size),
                         sum(x.size or 0 for x in ram)))

    peripheral_line()
    index_line(items)
    per_module(items, named)
    worklist(items, named)


def peripheral_line():
    """How much of the chip's register map the image's own words reach.

    Two numbers: how many of the words that hold a peripheral address the SVD
    resolves to a register, and how many instructions read one of them. The
    second is the one to watch, because a word is only evidence where the code
    loads it.
    """
    chip = peripherals.load()
    words = json.load(open(os.path.join(OUT, "words.json")))["words"]
    refs = json.load(open(os.path.join(OUT, "references.json")))
    sites = collections.Counter(r["target"] for r in refs["pool_reads"])
    named = [w for w in words if w["signal"] == "peripheral"]
    blocks = set()
    for word in named:
        blocks.add(chip.at(word["value"]).peripheral.name)
    unnamed = [w for w in words if w["signal"] in ("out_of_range",
                                                   "float_constant")
               and sites[w["addr"]]
               and peripherals.in_peripheral_space(w["value"])]
    print("peripherals: %d of the %d read words in the peripheral ranges name a"
          " register, over %d blocks, from %d sites"
          % (len(named), len(named) + len(unnamed), len(blocks),
             sum(sites[w["addr"]] for w in named)))


def index_line(items):
    """How much of the image abi/accesses.py can say anything about.

    A function with a non-empty side is one whose globals and registers are
    known; a function with none holds no address of its own, which is either a
    leaf that works on its arguments or a body the flow lost.
    """
    sides, summary = accessindex.load_index()
    total = len(items["functions"])
    with_side = sum(1 for s in sides.values() if s.accesses)
    print("access index: %d of %d functions have a side, %d accesses over %d"
          " RAM objects and %d registers"
          % (with_side, total, summary["accesses"], summary["ram_objects"],
             summary["registers"]))
    if "runtime" in summary:
        print("  runtime: %d observed-only, %d confirmed, %d static-only"
              % (summary["runtime"]["observed_only"],
                 summary["runtime"]["confirmed"],
                 summary["runtime"]["static_only"]))


def per_module(items, named):
    """How much of each log-tag module still has no name.

    The partition into modules is abi/out/ghidra/modules.json, which
    abi/autonames.py writes from the tags the firmware's own log lines carry,
    so the unnamed count per module is what says where the next rule has to
    reach: a module that is 95 percent unnamed has no per-function log line
    and nothing the call graph can hang a name on.
    """
    modules = json.load(open(os.path.join(OUT, "modules.json")))
    by_start = {f["start"]: f for f in items["functions"]}
    rows = {}
    for address, module in module_of(modules).items():
        f = by_start.get(address)
        if f is None:
            continue
        total, un = rows.get(module, (0, 0))
        rows[module] = (total + 1, un + (1 if is_unnamed(f, named) else 0))
    print("modules: %d, %d functions in one"
          % (len(rows), sum(n for n, _ in rows.values())))
    for module, (total, un) in sorted(rows.items(), key=lambda kv: -kv[1][1])[:15]:
        print("  %-22s %4d fns, %4d unnamed %3.0f%%"
              % (module, total, un, 100.0 * un / total))


def module_of(modules):
    """address -> module, whichever shape modules.json was written in."""
    out = {}
    m = modules.get("functions") or modules.get("modules") or modules
    for key, value in m.items():
        if isinstance(value, dict) and "module" in value:
            out[int(key, 0)] = value["module"]
        elif isinstance(value, list):
            for a in value:
                out[int(a, 0) if isinstance(a, str) else a] = key
    return out


def is_unnamed(f, named):
    return f["start"] not in named and bool(UNNAMED.match(f["name"]))


def worklist(items, named):
    """The naming worklist abi/ghidra/mark_review.py bookmarks, as counts.

    Unnamed1 is a body a named function calls directly, which is the shallow
    end the bookmark list starts at; the number is the one to watch, because a
    rule that names a body promotes its own callees into it.
    """
    refs = json.load(open(os.path.join(OUT, "references.json")))
    by_start = {f["start"]: f for f in items["functions"]}
    callers = collections.defaultdict(set)
    for c in refs["calls"]:
        if c["kind"] not in ("call", "jump"):
            continue
        src, dst = c.get("function"), c["to"]
        if src in by_start and dst in by_start:
            callers[dst].add(src)
    has_name = {a for a, f in by_start.items() if not is_unnamed(f, named)}
    depth, frontier, level = {}, set(), 1
    for callee, srcs in callers.items():
        if callee not in has_name and srcs & has_name:
            depth[callee] = 1
            frontier.add(callee)
    while frontier and level < MAX_DEPTH:
        level += 1
        nxt = set()
        for callee, srcs in callers.items():
            if callee in has_name or callee in depth:
                continue
            if srcs & frontier:
                depth[callee] = level
                nxt.add(callee)
        frontier = nxt
    counts = collections.Counter(
        "Unnamed%d" % depth[a] if a in depth else "UnnamedDeep"
        for a in by_start if a not in has_name)
    print("worklist: %d unnamed, %s"
          % (sum(counts.values()),
             ", ".join("%s %d" % kv for kv in sorted(counts.items()))))


if __name__ == "__main__":
    main()
