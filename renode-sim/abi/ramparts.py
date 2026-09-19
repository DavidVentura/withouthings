#!/usr/bin/env python3
"""Cut the app's static RAM into items, the way the partition cuts its flash.

    python3 abi/ramparts.py            # the partition, as a report

The flash side is cut by Ghidra's analysis; RAM has no bytes to analyse, so
what cuts it is what the image itself establishes about it:

  * the startup's own three statements -- the `.data` image is copied from
    flash to a RAM run, and two RAM runs are zeroed -- which say where the
    initialised and the zeroed RAM begin and end and which of them an item's
    bytes come from;
  * every RAM address a flash word names, which is a point the code takes the
    address of and therefore an item start;
  * every RAM address abi/symbols.yaml names, which is the same thing with a
    name on it;
  * a declared object's size, which is the one thing here that says where an
    item *ends* rather than where the next one starts.

Where nothing bounds an item it runs to the next named address, exactly as an
untyped data run does on the flash side. Nothing is guessed: RAM the startup
does not establish -- the SoftDevice's, and the retained block below the app's
own base -- is not partitioned at all, and stays at its address.
"""

import argparse
import collections
import json
import os
import re
import struct
import sys

import yaml

APP_BASE = 0x27000
APP_END = 0xF117C
RAM_BASE, RAM_END = 0x20000000, 0x20040000

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)

# `memset(&a, 0, (&b + 3 - &a) & ~3)` with the guard GCC emits when it cannot
# prove b >= a, which is how this compiler writes a zero fill between two
# linker-defined symbols. The two addresses are the pool words the two `ldr
# rN,[pc]` in front of it read. The idiom is matched as bytes rather than
# looked up in the export because it is the only statement in the image that
# says where the zeroed RAM stops, so it has to be read off the instructions
# that make it true.
ZERO_FILL = bytes.fromhex("da1c121ac11e22f00302994288bf00220021")

# The alignment the fill's own `& ~3` rounds over, and so the largest gap
# between two startup runs that is padding rather than a hole in the region.
REGION_JOIN = 4


class Fill(object):
    """One `memset(&a, 0, &b - &a)`: where it is and the two words it reads."""

    def __init__(self, site, low, high):
        self.site = site
        self.start, self.start_word = low
        self.end, self.end_word = high


class Run(object):
    """A RAM range the startup establishes, and what it does to it."""

    def __init__(self, start, end, kind, load, evidence, bounds=()):
        self.start, self.end, self.kind = start, end, kind
        self.name = kind
        self.load = load            # the flash address of its initialiser, or None
        self.evidence = evidence
        # The flash words the startup reads its bounds out of, by which bound.
        # They are the one reference to the run the linker has to answer with a
        # symbol rather than with the item's address: the startup copies and
        # zeroes whole runs, and a run is only whole if both its ends move
        # together.
        self.bounds = dict(bounds)

    def __repr__(self):
        return "Run(0x%x..0x%x, %s)" % (self.start, self.end, self.kind)


class Item(object):
    """One RAM object: a section of its own, at an address or wherever the linker puts it."""

    def __init__(self, start, end, kind, load, name, bounded):
        self.start, self.end, self.kind = start, end, kind
        self.load = load
        self.name = name
        self.bounded = bounded      # what says where it ends
        self.sym = name

    @property
    def size(self):
        return self.end - self.start

    def __contains__(self, addr):
        return self.start <= addr < self.end

    def __repr__(self):
        return "Item(%s, 0x%x..0x%x, %s)" % (self.name, self.start, self.end,
                                             self.kind)


def zero_fills(blob, refs):
    """Every `memset(&a, 0, &b - &a)` in the image, as (site, low, high).

    A site whose two bounds are not the two pool words immediately in front of
    it is refused rather than read at a guess: the length is half of what the
    fill claims, and a third pool read in the window means the instruction
    stream does not say which pair it is.
    """
    pool = dict((r["site"], r["target"]) for r in refs["pool_reads"])

    def held(addr):
        return struct.unpack_from("<I", blob, addr - APP_BASE)[0]

    found = []
    for m in re.finditer(re.escape(ZERO_FILL), blob):
        at = APP_BASE + m.start()
        sites = sorted(s for s in pool if at - 8 <= s < at)
        if len(sites) != 2:
            continue
        words = sorted((held(pool[s]), pool[s]) for s in sites)
        if not all(RAM_BASE <= v < RAM_END for v, _ in words):
            continue
        found.append(Fill(at, words[0], words[1]))
    return found


def copier_words(blob, refs, copy_word):
    """The RAM words in the pool of the function that reads `copy_word`."""
    reading = set(r["function"] for r in refs["pool_reads"]
                  if r["target"] == copy_word)
    out = {}
    for r in refs["pool_reads"]:
        if r["function"] not in reading:
            continue
        value = struct.unpack_from("<I", blob, r["target"] - APP_BASE)[0]
        if RAM_BASE <= value < RAM_END:
            out[r["target"]] = value
    return out


def startup_runs(words, fills, blob, refs):
    """The `.data` and `.bss` runs the startup establishes, in address order.

    The copy word is the anchor: it is the only thing in the image that names
    both a flash run and the RAM run it becomes, so `.data` is known exactly.
    A zero fill is part of the region when it abuts what is known already --
    the app's `.bss` is what the linker put either side of its `.data` -- and
    an inner fill is a statement about one object inside the region rather than
    about the region, so it bounds an item later and not a run here.

    Nothing extends the region across a gap wider than the fill's own
    alignment, so RAM the startup says nothing about is not swept in.
    """
    copies = [w for w in words if w.get("span")]
    if len(copies) != 1:
        raise SystemExit("the image holds %d RAM initialiser copies, not one:"
                         " which one is .data is not established" % len(copies))
    copy, = copies
    dest = int(copy["note"].rsplit(" ", 1)[-1], 0)
    bounds = {copy["addr"]: "__data_load__"}
    # The copy's own two RAM words. The loop reads three words out of one pool:
    # where the image is, where it goes and where it stops, and all three are
    # the linker's to answer once the run is sections. Which word is which is
    # decided by the value, since the run is already known from the copy.
    for word, value in copier_words(blob, refs, copy["addr"]).items():
        if value == dest:
            bounds[word] = "__data_start__"
        elif value == dest + copy["span"]:
            bounds[word] = "__data_end__"
    runs = [Run(dest, dest + copy["span"], "data", copy["value"],
                "copied from 0x%x by the initialiser loop at 0x%x"
                % (copy["value"], copy["addr"]), bounds)]
    pending = list(fills)
    n, grew = 0, True
    while grew:
        grew = False
        lo_now = min(r.start for r in runs)
        hi_now = max(r.end for r in runs)
        for fill in list(pending):
            if fill.start >= lo_now and fill.end <= hi_now:
                continue            # an object inside the region, not its edge
            if fill.end + REGION_JOIN < lo_now or fill.start > hi_now + REGION_JOIN:
                continue            # says nothing about the region's extent
            runs.append(Run(fill.start, fill.end, "bss", None,
                            "zeroed by the fill at 0x%x" % fill.site,
                            {fill.start_word: "__bss%d_start__" % n,
                             fill.end_word: "__bss%d_end__" % n}))
            pending.remove(fill)
            n, grew = n + 1, True
    runs.sort(key=lambda r: r.start)
    # The names go by address rather than by the order the region grew in, so
    # the fragment and the report read in one direction.
    for i, run in enumerate(r for r in runs if r.kind == "bss"):
        run.name = "bss%d" % i
        run.bounds = dict((w, re.sub(r"bss\d+", run.name, s))
                          for w, s in run.bounds.items())
    return runs


def inner_fills(runs, fills):
    """The zero fills that bound an object inside the region rather than the region."""
    lo, hi = runs[0].start, runs[-1].end
    return [f for f in fills if lo <= f.start and f.end <= hi]


def name_for(addr, names, kind):
    """What to call the item at `addr`: the map's name, or the address."""
    return names.get(addr) or ("%s_%08x" % (kind, addr))


def partition(runs, points, sizes, names, established):
    """Cut every run at its points; an item runs to the next one.

    `sizes` is the only source of an end that is not the next start: a declared
    object's own length. Where it stops short of the next point the bytes
    between the two are their own item, because nothing says they belong to
    either -- the same maximal-run rule the flash side's untyped gaps follow.

    The distinction the layout then rests on: a cut at the next named address
    says where a reference lands, not where the object above it stops. Two such
    items may be one array the code walks from its head, so they may be moved
    but not separated. A cut at a declared size, at a zero fill's own bounds or
    at a run's edge is a statement about the end, and `established` collects
    exactly those.
    """
    items = []
    for run in runs:
        established.add(run.start)
        established.add(run.end)
        cuts = sorted(set([run.start, run.end])
                      | set(p for p in points if run.start < p < run.end))
        for start, nxt in zip(cuts, cuts[1:]):
            end = min(start + sizes[start], nxt) if start in sizes else nxt
            bounded = ("declared size" if start in sizes and end < nxt
                       else "the next named address")
            load = None if run.load is None else run.load + (start - run.start)
            items.append(Item(start, end, run.kind, load,
                              name_for(start, names, run.kind), bounded))
            if end < nxt:
                established.add(end)
                items.append(Item(end, nxt, run.kind,
                                  None if run.load is None
                                  else run.load + (end - run.start),
                                  name_for(end, names, run.kind),
                                  "the next named address"))
    return items


class Ram(object):
    """The app's static RAM, cut into items, with the lookup a relocation needs."""

    def __init__(self, runs, items, established):
        self.runs, self.items = runs, items
        # The item starts something in the image says are also the end of what
        # is below them. Only there may a layout put a different item.
        self.established = established
        self.starts = [i.start for i in items]
        self.by_start = dict((i.start, i) for i in items)
        # The word the startup reads a bound out of -> the linker's name for
        # that bound.
        self.bounds = {}
        for run in runs:
            self.bounds.update(run.bounds)
        # A run's end is an address no item holds -- one past the last byte --
        # so a word holding it has nothing to relocate against but the bound
        # itself, wherever in the image the word sits.
        self.ends = dict((run.end, "__%s_end__" % run.name) for run in runs)

    @property
    def start(self):
        return self.runs[0].start

    @property
    def end(self):
        return self.runs[-1].end

    def at(self, addr):
        """The item holding `addr`, or None if the app's static RAM does not."""
        import bisect
        i = bisect.bisect_right(self.starts, addr)
        if not i:
            return None
        item = self.items[i - 1]
        return item if addr in item else None


def load(export=None, image=None, symbols_map=None, types=None):
    """The RAM partition, from the export, the image, the map and the headers."""
    export = export or os.path.join(HERE, "out", "ghidra")
    blob = open(image or os.path.join(SIM, "appl.bin"), "rb").read()
    refs = json.load(open(os.path.join(export, "references.json")))
    words = json.load(open(os.path.join(export, "words.json")))["words"]

    fills = zero_fills(blob, refs)
    runs = startup_runs(words, fills, blob, refs)
    lo, hi = runs[0].start, runs[-1].end
    # The RAM no layout may place into, with the argument for each in
    # facts.yaml. The check is that the startup's own reading agrees with it:
    # a run that reached into the SoftDevice's RAM or into the stack would be
    # the partition claiming bytes the image never said were the app's.
    facts = yaml.safe_load(open(os.path.join(HERE, "facts.yaml")))
    for fixed in facts["ram_fixed_points"]:
        if lo < fixed["end"] and fixed["start"] < hi:
            sys.exit("the app's static RAM (0x%x..0x%x) overlaps the fixed"
                     " point %s (0x%x..0x%x)"
                     % (lo, hi, fixed["name"], fixed["start"], fixed["end"]))

    names, sizes = {}, {}
    if symbols_map is not None:
        for addr, sym in symbols_map.by_address.items():
            if not lo <= addr < hi:
                continue
            names.setdefault(addr, sym.name)
            # abi/globals.py measures a RAM global's width off the accesses --
            # a settings cache word's load width, a struct's field offsets, a
            # table's stride and row count -- so the map carries the one thing
            # the startup cannot say: where an object stops. That is a bound in
            # the same sense a declared table's is.
            if sym.row.get("size"):
                sizes.setdefault(addr, sym.row["size"])
    if types is not None and symbols_map is not None:
        for table in types.typed_regions(symbols_map):
            if lo <= table.address < hi and table.end > table.address:
                sizes[table.address] = table.end - table.address

    points = set()
    for word in words:
        if lo <= word["value"] < hi:
            # Rounded down to the word, because a `.data` item's initialiser
            # carries relocations and a cut inside one of their four bytes
            # would split a slot. An address the map names is taken exactly:
            # a byte global at an odd address is that byte and not the word
            # around it.
            points.add(word["value"] & ~3)
    points |= set(names)
    # An inner fill zeroes one object, so both its ends are established: the
    # length is in the instruction stream, not inferred from what comes next.
    established = set()
    for fill in inner_fills(runs, fills):
        points.add(fill.start)
        points.add(fill.end)
        established.add(fill.start)
        established.add(fill.end)

    items = partition(runs, points, sizes, names, established)
    return Ram(runs, items, established)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    args = ap.parse_args()

    import shapes
    import symbols as symmap
    smap = symmap.load()
    ram = load(args.export, args.image, smap, shapes.load())
    for run in ram.runs:
        print("0x%08x..0x%08x  %-5s %s" % (run.start, run.end, run.kind,
                                           run.evidence))
    kinds = collections.Counter(i.kind for i in ram.items)
    bytes_ = collections.Counter()
    for i in ram.items:
        bytes_[i.kind] += i.size
    print("%d items: %s" % (len(ram.items),
                            ", ".join("%d %s (%d bytes)" % (n, k, bytes_[k])
                                      for k, n in sorted(kinds.items()))))
    bounded = collections.Counter(i.bounded for i in ram.items)
    for why, n in bounded.most_common():
        print("  %d bounded by %s" % (n, why))
    named = sum(1 for i in ram.items if not re.match(r"^(data|bss)_[0-9a-f]{8}$",
                                                     i.name))
    print("  %d carry a name from the map, %d are called after their address"
          % (named, len(ram.items) - named))


if __name__ == "__main__":
    main()
