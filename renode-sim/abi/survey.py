#!/usr/bin/env python3
"""The unnamed remainder, decomposed into components that stand on their own.

    python3 abi/survey.py [--top 30] [--json out/survey/survey.json]
    python3 abi/survey.py --component 0x78f1c    # one component, in full

Half the app carries no name. A name comes from something the image says about
itself -- a log line, a table row, a body that reproduces from a source build --
so the code that says nothing is the code that is not Withings': a vendor
library ships as a built object with its symbols stripped, it does not log
through wlog, it does not call the firmware's own subsystems, and the firmware
reaches it through a handful of entry points.

That shape is measurable. This reads the relocatable object abi/blobify.py
emits, lifts its section graph to a function graph, and cuts the unnamed
functions into components by domination: the virtual root enters every named
function, so an unnamed function that dominates a set of unnamed functions owns
exactly the code nothing else in the image can reach. A component's entry
surface is then its dominator's callers and nothing else, which is the property
a vendor drop has and a Withings module does not.

Each component is reported with what it costs (bytes), what it takes (entries),
what it needs (exit calls, bucketed into libc, libm, the kernel and Withings),
and the four things that separate a signal-processing drop from firmware: does
it log, how dense is its floating point, what strings does it reference, and
what constant tables does it read.
"""

import argparse
import bisect
import collections
import json
import os
import re
import struct
import sys

from elftools.elf.elffile import ELFFile

import symbols as symmap

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
OUT = os.path.join(SIM, "out", "survey")
APP_BASE = 0x27000

# The partition's placeholders: a name Ghidra invented, which explains nothing.
PLACEHOLDER = re.compile(r"^(FUN|LAB|block|caseD|thunk|switchD)_")

# Where a call leaves the component to. The ranges are abi/boundary.yaml's
# library spans; the rest is decided by the map's class.
LIBC_CLASSES = {"libc", "libm", "match", "svc", "syscall", "extlib"}

# The log ring's entry points. A component that calls one of these is
# firmware: it was written against Withings' own logging macros, which no
# outside library has.
WLOG_SINKS = {0x8F460: "wlog", 0x8F494: "wlog_fmt_r0", 0x5E7A8: "wlog_fmt_r0d",
              0x9B1C8: "wlog_fmt_r0c"}

# Printing through stdio instead says the opposite: a drop written against a
# FILE* diagnoses itself with fprintf and the firmware wires that onto its own
# ring afterwards. Counted, reported, and not a refusal.
STDIO_SINKS = {0x8F42C: "nrf_fprintf", 0x8F450: "nrf_fprintf_str"}

LOG_SINKS = {}
LOG_SINKS.update(WLOG_SINKS)
LOG_SINKS.update(STDIO_SINKS)

VFP = re.compile(r"\b(v(add|sub|mul|div|sqrt|ldr|str|mov|cvt|cmp|neg|abs|"
                 r"mla|mls|fma|fms|nmul|push|pop|ldm|stm|sel|max|min)[a-z0-9.]*)\b")


class Fn(object):
    """One function of the partition, with what the map calls it."""

    def __init__(self, start, end, size, label, symbol, seeded=False):
        self.start, self.end, self.size = start, end, size
        self.label = label
        # The export says where each of its names came from, and a name it
        # took from abi/symbols.yaml is the map's name echoed back rather than
        # a second reading of the image. Only the map is asked about those, so
        # a class this run rewrites cannot survive in the export and keep the
        # closure it was cut from from being cut again.
        self.seeded = seeded
        self.symbol = symbol
        self.name = symbol.name if symbol else label
        self.klass = symbol.klass if symbol else None
        self.module = symbol.module if symbol else None

    @property
    def named(self):
        if self.symbol is not None:
            return True
        return not self.seeded and not PLACEHOLDER.match(self.label)

    def __repr__(self):
        return "<%s @0x%x %dB>" % (self.name, self.start, self.size)


class Graph(object):
    """The app as a call graph of functions, lifted from the object's relocations.

    A relocation is an edge from the function containing its offset to the
    function the target symbol's value lands in. Data sections are transparent:
    a pointer table is not a caller, it is the medium through which the function
    that loads its address reaches the handlers, so an edge through one is the
    edge the image really has.
    """

    def __init__(self, fns, succ, pred, via_data):
        self.fns = fns
        self.starts = sorted(fns)
        self.succ, self.pred = succ, pred
        self.via_data = via_data

    def at(self, addr):
        i = bisect.bisect_right(self.starts, addr) - 1
        if i < 0:
            return None
        f = self.fns[self.starts[i]]
        return f.start if addr < f.end else None


def load_partition(items_path, smap, ignore=()):
    """Every function the partition knows, keyed by start, with its map entry.

    A class this run rewrites is dropped: a record this tool wrote last time is
    not evidence about the image, so leaving it in would make the closure shrink
    to nothing on the second run. An address already attributed to a component
    goes with it whatever class carries the name, because a hand that reads an
    interior and says what it does has not moved it out of the drop: leaving
    such a name in would cut it out of its own closure and then report the
    component as calling it, which is the leak test answering a question about
    this file instead of about the image.
    """
    named = {s.address: s for s in smap.of_kind("function")
             if s.klass not in ignore and not (ignore and s.component)}
    fns = {}
    for f in items_path["functions"]:
        size = (f["bytes"] if "bytes" in f else
                sum(b - a for a, b in f.get("ranges", [(f["start"], f["end"])])))
        fns[f["start"]] = Fn(f["start"], f["end"], size, f.get("name") or "",
                             named.get(f["start"]),
                             seeded=f.get("named") == "seed")
    return fns


def load_graph(obj, place, fns):
    """Lift the object's section relocations onto the partition's functions."""
    placed = {}
    for line in open(place):
        m = re.search(r"KEEP\(\*[\w.-]+\((\S+)\)\)\s+/\* 0x([0-9a-f]+)", line)
        if m:
            placed[m.group(1)] = int(m.group(2), 16)
    elf = ELFFile(open(obj, "rb"))
    secs = list(elf.iter_sections())
    index = {s.name: i for i, s in enumerate(secs)}
    addr_of = {}
    for i, s in enumerate(secs):
        if s.name in placed:
            addr_of[i] = placed[s.name]
    syms = list(elf.get_section_by_name(".symtab").iter_symbols())

    starts = sorted(fns)
    def owner(addr):
        i = bisect.bisect_right(starts, addr & ~1) - 1
        if i < 0:
            return None
        f = fns[starts[i]]
        return f.start if (addr & ~1) < f.end else None

    # Section -> the addresses its relocations point at, so a data section can
    # be walked through without being a node of the graph.
    out_of = collections.defaultdict(list)
    for s in secs:
        if not s.name.startswith(".rel.") or s.name[4:] not in index:
            continue
        src = index[s.name[4:]]
        if src not in addr_of:
            continue
        for r in s.iter_relocations():
            sym = syms[r["r_info_sym"]]
            shndx = sym.entry.st_shndx
            if not isinstance(shndx, int) or shndx not in addr_of:
                continue
            target = addr_of[shndx] + sym.entry.st_value
            out_of[src].append((addr_of[src] + r["r_offset"], target, shndx))

    code = set(i for i, s in enumerate(secs)
               if i in addr_of and s.name.startswith(".text."))

    resolved = {}
    def through(sec, seen):
        """Every code address a data section leads to, following data hops."""
        if sec in resolved:
            return resolved[sec]
        if sec in seen:
            return set()
        found = set()
        for _, target, shndx in out_of.get(sec, ()):
            if shndx in code:
                found.add(target)
            else:
                found |= through(shndx, seen | {sec})
        resolved[sec] = found
        return found

    succ = collections.defaultdict(set)
    via_data = collections.defaultdict(set)
    for sec in sorted(code):
        for site, target, shndx in out_of.get(sec, ()):
            src = owner(site)
            if src is None:
                continue
            if shndx in code:
                dsts = [(target, False)]
            else:
                dsts = [(t, True) for t in through(shndx, set())]
            for dst, indirect in dsts:
                dst = owner(dst)
                if dst is None or dst == src:
                    continue
                succ[src].add(dst)
                if indirect:
                    via_data[src].add(dst)
    pred = collections.defaultdict(set)
    for a, bs in succ.items():
        for b in bs:
            pred[b].add(a)
    return Graph(fns, succ, pred, via_data)


def dominators(nodes, succ, roots):
    """Iterative dominator sets, restricted to what the roots reach.

    The graph is a call graph and not a CFG, so the cheap Lengauer-Tarjan
    formulation does not apply cleanly; the node count here is five thousand and
    the fixpoint converges in a handful of passes, so the set formulation is
    what this uses and what it means is plain: idom[n] is the one function every
    path from a root to n passes through.
    """
    order, seen = [], set(roots)
    stack = list(roots)
    while stack:
        n = stack.pop()
        order.append(n)
        for m in succ.get(n, ()):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    # Breadth order from the roots makes the fixpoint converge fastest.
    depth, queue = dict((r, 0) for r in roots), collections.deque(roots)
    while queue:
        n = queue.popleft()
        for m in succ.get(n, ()):
            if m not in depth:
                depth[m] = depth[n] + 1
                queue.append(m)
    order = sorted(seen, key=lambda n: depth[n])

    ROOT = object()
    dom = {}
    for n in seen:
        dom[n] = None if n in roots else set(seen)
    for r in roots:
        dom[r] = {r}
    changed = True
    while changed:
        changed = False
        for n in order:
            if n in roots:
                continue
            new = None
            for p in pred_in(n, succ, seen):
                if dom[p] is None:
                    continue
                new = set(dom[p]) if new is None else (new & dom[p])
            if new is None:
                continue
            new.add(n)
            if new != dom[n]:
                dom[n] = new
                changed = True
    idom = {}
    for n in seen:
        if n in roots:
            continue
        strict = dom[n] - {n}
        if not strict:
            continue
        idom[n] = max(strict, key=lambda d: len(dom[d]))
    return dom, idom


_PRED_CACHE = {}


def pred_in(n, succ, seen):
    key = id(succ)
    if key not in _PRED_CACHE:
        pred = collections.defaultdict(set)
        for a, bs in succ.items():
            for b in bs:
                pred[b].add(a)
        _PRED_CACHE[key] = pred
    return [p for p in _PRED_CACHE[key][n] if p in seen]


def sccs(nodes, succ):
    """Tarjan, iterative: the mutual-recursion clusters of the call graph."""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(succ.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for m in it:
                if m not in nodes:
                    continue
                if m not in index:
                    index[m] = low[m] = counter[0]
                    counter[0] += 1
                    stack.append(m)
                    on.add(m)
                    work.append((m, iter(sorted(succ.get(m, ())))))
                    advanced = True
                    break
                if m in on:
                    low[node] = min(low[node], index[m])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                group = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    group.append(w)
                    if w == node:
                        break
                out.append(group)
    return out


class Disassembly(object):
    """out/appl.dis, indexed by address, for the two things bytes cannot say.

    VFP density says a body computes in floating point, and a literal-pool word
    that lands in a float table says it reads coefficients. Both are properties
    of the instruction stream and neither is in the object.
    """

    def __init__(self, path):
        self.insn = {}
        with open(path) as fh:
            for line in fh:
                m = re.match(r"\s*([0-9a-f]+):\s+[0-9a-f ]+\t(\S+)", line)
                if m:
                    self.insn[int(m.group(1), 16)] = m.group(2)

    def counts(self, start, end):
        total = vfp = 0
        for a in range(start, end, 2):
            op = self.insn.get(a)
            if op is None:
                continue
            total += 1
            if VFP.match(op):
                vfp += 1
        return total, vfp


class Constants(object):
    """The image itself, for the words a component's pools point at."""

    def __init__(self, path):
        self.image = open(path, "rb").read()

    def word(self, addr):
        off = addr - APP_BASE
        if not 0 <= off <= len(self.image) - 4:
            return None
        return struct.unpack_from("<I", self.image, off)[0]

    def string(self, addr):
        off = addr - APP_BASE
        if not 0 <= off < len(self.image):
            return None
        end = self.image.find(b"\0", off)
        if end < 0 or end - off > 200:
            return None
        raw = self.image[off:end]
        if len(raw) < 4 or not all(32 <= c < 127 or c in (9, 10) for c in raw):
            return None
        return raw.decode("latin1")

    def floats(self, start, end):
        """How many words of a run are plausible IEEE-754 singles.

        A weight table is the one kind of data whose bytes announce themselves:
        exponents cluster in the middle of the range and no word is a pointer.
        """
        good = 0
        n = 0
        for a in range(start, end - 3, 4):
            w = self.word(a)
            if w is None:
                continue
            n += 1
            exp = (w >> 23) & 0xFF
            if w == 0 or 0x40 <= exp <= 0xC0:
                good += 1
        return n, good


class Component(object):
    def __init__(self, entry, members, graph):
        self.entry = entry
        self.members = members
        self.graph = graph
        self.bytes = sum(graph.fns[m].size for m in members)

    @property
    def span(self):
        return (min(self.graph.fns[m].start for m in self.members),
                max(self.graph.fns[m].end for m in self.members))


def components(graph, entries_from_named=True):
    """Unnamed functions cut into sets nothing but their own entry reaches.

    Named functions are the roots: the image explains them, so anything a named
    function can reach without going through an unnamed one is not part of a
    component. What is left under an unnamed function is that function's private
    closure, and the largest such closures are the drops.
    """
    named = set(s for s, f in graph.fns.items() if f.named)
    roots = sorted(named | set(s for s in graph.fns
                               if not graph.pred.get(s) and s not in named))
    dom, idom = dominators(set(graph.fns), graph.succ, roots)

    children = collections.defaultdict(list)
    for n, d in idom.items():
        children[d].append(n)

    def subtree(n):
        out, stack = [], [n]
        while stack:
            u = stack.pop()
            out.append(u)
            stack.extend(children.get(u, ()))
        return out

    # A component's root is an unnamed function whose immediate dominator is
    # named: one step further out and the image explains the caller, so this is
    # exactly where the unnamed region begins.
    out = []
    for n in sorted(graph.fns):
        if n in named or idom.get(n) is None or idom[n] not in named:
            continue
        members = [m for m in subtree(n) if m not in named]
        if members:
            out.append(Component(n, members, graph))
    return out, idom


def closure(graph, entries):
    """The unnamed functions only the declared entries reach.

    Cut the entries out of the graph and walk everything else from the roots.
    What the walk does not find is what no path from anywhere in the image
    reaches without passing through an entry, which is the multi-entry form of
    domination and the precise sense in which a drop is self-contained.
    """
    cut = set(entries)
    roots = [s for s, f in graph.fns.items()
             if s not in cut and (f.named or not graph.pred.get(s))]
    seen, stack = set(roots), list(roots)
    while stack:
        for nxt in graph.succ.get(stack.pop(), ()):
            if nxt not in seen and nxt not in cut:
                seen.add(nxt)
                stack.append(nxt)
    reached, stack = set(), list(cut)
    while stack:
        for nxt in graph.succ.get(stack.pop(), ()):
            if nxt not in reached and nxt not in cut:
                reached.add(nxt)
                stack.append(nxt)
    return sorted(m for m in reached - seen if not graph.fns[m].named)


def bucket(fn, library_ranges):
    """Which world an exit call lands in."""
    if any(a <= fn.start <= b for a, b in library_ranges):
        return "kernel/nrfx"
    if fn.klass in LIBC_CLASSES:
        return {"libm": "libm"}.get(fn.klass, "libc")
    if fn.named:
        return "withings"
    return "unnamed"


def describe(comp, graph, dis, consts, library_ranges, pool_reads):
    members = set(comp.members)
    callers = set()
    for m in members:
        for p in graph.pred.get(m, ()):
            if p not in members:
                callers.add(p)
    entries = sorted(set(m for m in members
                         if any(p not in members for p in graph.pred.get(m, ()))))
    exits = collections.defaultdict(set)
    for m in members:
        for t in graph.succ.get(m, ()):
            if t in members:
                continue
            exits[bucket(graph.fns[t], library_ranges)].add(t)

    logs = sorted(set(LOG_SINKS[t] for m in members
                      for t in graph.succ.get(m, ()) if t in LOG_SINKS))
    total = vfp = 0
    for m in members:
        f = graph.fns[m]
        t, v = dis.counts(f.start, f.end)
        total += t
        vfp += v

    strings, tables = set(), collections.Counter()
    for m in members:
        for site, target in pool_reads.get(m, ()):
            w = consts.word(target)
            if w is None:
                continue
            s = consts.string(w)
            if s:
                strings.add(s)
                continue
            if APP_BASE <= w < APP_BASE + len(consts.image) and graph.at(w & ~1) is None:
                tables[w] += 1
    return {
        "entry": comp.entry,
        "name": graph.fns[comp.entry].label,
        "bytes": comp.bytes,
        "functions": len(comp.members),
        "span": list(comp.span),
        "entries": entries,
        "callers": sorted(callers),
        "caller_names": sorted(graph.fns[c].name for c in callers),
        "exits": dict((k, sorted(v)) for k, v in exits.items()),
        "exit_names": dict((k, sorted(graph.fns[t].name for t in v))
                           for k, v in exits.items()),
        "logs": logs,
        "insns": total,
        "vfp": vfp,
        "vfp_density": (100.0 * vfp / total) if total else 0.0,
        "strings": sorted(strings)[:12],
        "string_count": len(strings),
        "const_refs": [a for a, _ in tables.most_common(12)],
    }


def verdict(d):
    """The shape the numbers make, as a one-word reading."""
    if d["logs"]:
        return "withings (logs)"
    if d["exits"].get("withings"):
        return "withings (calls named code)"
    if d["vfp_density"] >= 8.0:
        return "vendor candidate (float)"
    return "candidate (no log, no withings callee)"


VENDOR = "vendor"


def vendor_records(graph, decls, library_ranges, smap):
    # The map as it is without this tool's own last run, which is not evidence
    # about the image and would make the second run name nothing.
    held_by_name = dict((sym.name, sym) for sym in smap.symbols
                        if sym.klass != VENDOR)
    """One map record per function of every declared component.

    The entries carry the name and the evidence the declaration gives them;
    the interiors carry the component and nothing else, because what they are
    is not established and the point of the record is that the coverage report
    stops calling them unnamed. A component whose closure calls named Withings
    code or reaches the log ring is refused: that is not a drop, and the
    declaration is what is wrong.
    """
    records, report = [], []
    for comp in decls["components"]:
        entries = [e["address"] for e in comp["entries"]]
        missing = [a for a in entries if a not in graph.fns]
        if missing:
            raise symmap.Refusal(
                "%s: no function starts at %s"
                % (comp["name"], ", ".join("0x%x" % a for a in missing)))
        members = closure(graph, entries)
        leaks, logs, stdio = set(), set(), set()
        for m in members + entries:
            for t in graph.succ.get(m, ()):
                if t in WLOG_SINKS:
                    logs.add(WLOG_SINKS[t])
                elif t in STDIO_SINKS:
                    stdio.add(STDIO_SINKS[t])
                elif t not in members and t not in entries:
                    if bucket(graph.fns[t], library_ranges) == "withings":
                        leaks.add(graph.fns[t].name)
        if logs:
            raise symmap.Refusal("%s reaches the log ring through %s, so it is"
                                 " firmware and not a drop"
                                 % (comp["name"], ", ".join(sorted(logs))))
        # An entry the image already explains keeps the name the image gave
        # it. The declaration still cuts the closure there -- that is what an
        # entry is for -- but a component is not a licence to rename what a
        # log line already named, so the record is only written where the map
        # has nothing.
        kept = 0
        for e in comp["entries"]:
            # The map holds an entry either at its address as a function or
            # under its name as anything at all -- picotls' entries are labels
            # a hand wrote prose against -- and in both cases the image already
            # explains it, so the declaration only cuts the closure there.
            held = graph.fns[e["address"]].symbol or held_by_name.get(e["name"])
            if held is not None:
                if held.name != e["name"] or held.address != e["address"]:
                    raise symmap.Refusal(
                        "0x%x is %s in %s and %s at 0x%x in the map"
                        % (e["address"], e["name"], comp["name"], held.name,
                           held.address))
                kept += 1
                continue
            records.append({"address": e["address"], "name": e["name"],
                            "kind": "function", "class": VENDOR,
                            "component": comp["name"],
                            "evidence": " ".join(e["evidence"].split())})
        for m in members:
            records.append({"address": m, "name": "%s__%x" % (comp["name"], m),
                            "kind": "function", "class": VENDOR,
                            "component": comp["name"],
                            "evidence": "interior: reached only through %s's"
                                        " entries" % comp["name"]})
        report.append((comp["name"], len(comp["entries"]) - kept, len(members),
                       sum(graph.fns[m].size for m in members + entries),
                       sorted(leaks), sorted(stdio)))
    return records, report


def load_pool_reads(path, graph):
    """Literal-pool loads, per function: where a body's constants come from."""
    refs = json.load(open(path))
    out = collections.defaultdict(list)
    for r in refs["pool_reads"]:
        fn = graph.at(r["function"]) or r["function"]
        out[fn].append((r["site"], r["target"]))
    return out


def main():
    import yaml
    ap = argparse.ArgumentParser()
    ap.add_argument("--object", default=os.path.join(SIM, "out", "survey", "appl-blob.o"))
    ap.add_argument("--place", default=os.path.join(SIM, "out", "survey", "place.ld"))
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--dis", default=os.path.join(SIM, "out", "appl.dis"))
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--component", type=lambda s: int(s, 0))
    ap.add_argument("--emit", action="store_true",
                    help="write abi/vendor.yaml's components into the map")
    ap.add_argument("--json", default=os.path.join(OUT, "survey.json"))
    args = ap.parse_args()

    items = json.load(open(os.path.join(args.export, "items.json")))
    smap = symmap.load()
    fns = load_partition(items, smap, ignore={VENDOR} if args.emit else ())
    graph = load_graph(args.object, args.place, fns)
    with open(os.path.join(HERE, "boundary.yaml")) as fh:
        library_ranges = [tuple(r) for r in yaml.safe_load(fh)["library_ranges"]]

    if args.emit:
        with open(os.path.join(HERE, "vendor.yaml")) as fh:
            decls = yaml.safe_load(fh)
        records, report = vendor_records(graph, decls, library_ranges, smap)
        for name, entries, interior, size, leaks, stdio in report:
            print("%-16s %2d entries named here, %3d interior bodies, %6d bytes%s%s"
                  % (name, entries, interior, size,
                     "; prints through %s" % ", ".join(stdio) if stdio else "",
                     "; calls %s" % ", ".join(leaks) if leaks else ""))
        print("%d records written" % smap.rewrite(records, {VENDOR}))
        return

    dis = Disassembly(args.dis)
    consts = Constants(os.path.join(SIM, "appl.bin"))
    pool_reads = load_pool_reads(os.path.join(args.export, "references.json"), graph)

    comps, idom = components(graph)
    rows = [describe(c, graph, dis, consts, library_ranges, pool_reads)
            for c in comps]
    for r in rows:
        r["verdict"] = verdict(r)
    rows.sort(key=lambda r: -r["bytes"])

    unnamed = [f for f in graph.fns.values() if not f.named]
    covered = sum(r["bytes"] for r in rows)
    print("unnamed: %d functions, %d bytes; components: %d covering %d bytes (%.0f%%)"
          % (len(unnamed), sum(f.size for f in unnamed), len(rows), covered,
             100.0 * covered / sum(f.size for f in unnamed)))

    clusters = [g for g in sccs(set(graph.fns), graph.succ) if len(g) > 1]
    print("strongly connected clusters larger than one: %d, largest %d functions"
          % (len(clusters), max([len(g) for g in clusters] or [0])))

    if args.component is not None:
        show = [r for r in rows if r["entry"] == args.component
                or r["span"][0] <= args.component < r["span"][1]]
        for r in show:
            detail(r, graph)
        return

    print("")
    print("%-22s %7s %5s %4s %4s  %-8s %s"
          % ("component", "bytes", "fns", "ent", "vfp%", "logs", "exits"))
    for r in rows[:args.top]:
        print("%-22s %7d %5d %4d %4.0f  %-8s %s"
              % (r["name"][:22], r["bytes"], r["functions"], len(r["entries"]),
                 r["vfp_density"], ",".join(r["logs"])[:8] or "-",
                 " ".join("%s:%d" % (k, len(v)) for k, v in sorted(r["exits"].items()))))

    os.makedirs(OUT, exist_ok=True)
    with open(args.json, "w") as fh:
        json.dump(rows, fh, indent=1)
    print("\n%s" % args.json)


def detail(r, graph):
    print("=" * 72)
    print("%s @0x%x: %d bytes, %d functions, 0x%x..0x%x"
          % (r["name"], r["entry"], r["bytes"], r["functions"],
             r["span"][0], r["span"][1]))
    print("  verdict: %s; vfp %d/%d (%.1f%%); logs: %s"
          % (r["verdict"], r["vfp"], r["insns"], r["vfp_density"],
             ", ".join(r["logs"]) or "none"))
    print("  entries: %s" % ", ".join("0x%x" % e for e in r["entries"]))
    print("  callers: %s" % ", ".join(r["caller_names"]))
    for k, v in sorted(r["exit_names"].items()):
        print("  exits %-12s %s" % (k, ", ".join(v[:20])))
    if r["strings"]:
        print("  strings: %s" % "; ".join(repr(s) for s in r["strings"]))
    if r["const_refs"]:
        print("  constants: %s" % ", ".join("0x%x" % a for a in r["const_refs"]))


if __name__ == "__main__":
    main()
