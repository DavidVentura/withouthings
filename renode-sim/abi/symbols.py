#!/usr/bin/env python3
"""The address map: what is where in the app image.

    python3 abi/symbols.py                 # a count per kind and per class

abi/symbols.yaml holds one entry per known address. It says nothing about
shape: a prototype, a struct or a table's element type is C and lives in
abi/include/withings/*.h, which abi/shapes.py reads out of the compiler. The
two meet by name, and this is the only thing that reads or writes the map.
"""

import collections
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
APP_BASE = 0x27000
APP_END = 0xF117C


class Refusal(Exception):
    """A derived name the map will not take, and what it collides with."""


class Hex(int):
    """An address, written the way every other address in the repo is."""


yaml.add_representer(
    Hex, lambda d, v: d.represent_scalar("tag:yaml.org,2002:int", "0x%x" % v))
yaml.add_representer(
    collections.OrderedDict,
    lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v.items()))

ORDER = ["address", "name", "aliases", "kind", "class", "module", "corrects",
         "supersedes", "evidence"]

HEADER = """\
# HWA10 (ScanWatch 2) application firmware v3411 -- the address map.
#
# Where a thing is. What it is lives in abi/include/withings/*.h (C) and
# abi/facts.yaml (everything that is neither C nor an address).
#
# Every entry is edited here. `class: hand` and `class: prose` are what a
# hand wrote; the other classes are written by the tool that measured them
# -- abi/match.py (match) and abi/autonames.py (the derived rules) -- which
# rewrite their own classes and leave the rest of the file alone.
# A hand entry that overrides a derivation names what it overrides in
# `corrects` (the address a body match landed on) or `supersedes` (the
# name a derivation produced), so the claim only holds against the
# measurement it was made against and the refusal fires again when that
# measurement moves instead of the stale claim winning silently.

"""


def dump(rows, path=None):
    """abi/symbols.yaml, in address order."""
    path = path or os.path.join(HERE, "symbols.yaml")
    ordered = []
    for row in sorted(rows, key=lambda r: (r["address"], r["name"])):
        out = collections.OrderedDict()
        for key in ORDER:
            if key in row:
                out[key] = (Hex(row[key]) if key in ("address", "corrects")
                            else row[key])
        ordered.append(out)
    with open(path, "w") as fh:
        fh.write(HEADER)
        yaml.dump({"symbols": ordered}, fh, sort_keys=False, width=78,
                  default_flow_style=False)


# A name a hand wrote, as against one a tool derived. The distinction is what
# the precedence fields are about: a derivation may not overwrite a hand entry
# and may not silently contradict one either.
HAND = "hand"


# Class precedence among the derived classes. Where two derivations reach the
# same address the stronger evidence keeps it: a byte verdict against a
# reference build (match) over a name read out of an archive, and either over a
# nickname derived from the call graph (helper). Hand entries are not on this
# list and are never displaced; overriding one is what `corrects` and
# `supersedes` are for.
# `prose` is last: it names an address without saying what is there, so it is
# something to compare a derivation against and not something that stops one.
# `codec` sits above `wppobj` because they are two readings of the same
# bodies: the call-graph rule finds an encoder by its first two writes, while
# abi/protocol.py carries the type id, the byte count and the field layout it
# recovered, so where both reach an address the measured one keeps it.
RANK = ["match", "libc", "libm", "svc", "syscall", "extlib", "string",
        "wppcmd", "codec", "wppobj", "wuiview", "shell", "logtag", "logcb",
        "bleevt", "logline", "helper", "prose"]


def outranks(klass, other):
    """Is `klass` stronger evidence for an address than `other`?"""
    if klass not in RANK or other not in RANK:
        return False
    return RANK.index(klass) < RANK.index(other)


class Symbol(object):
    def __init__(self, row):
        self.address = row["address"]
        self.name = row["name"]
        # Another name for the same address: the prose map named a table and
        # the hand map named it something else, and both are that table.
        self.aliases = row.get("aliases", [])
        self.kind = row["kind"]
        self.klass = row["class"]
        self.module = row.get("module")
        self.evidence = row.get("evidence")
        self.corrects = row.get("corrects")
        self.supersedes = row.get("supersedes")
        self.row = row

    def __repr__(self):
        return "<%s %s @0x%x>" % (self.kind, self.name, self.address)


class Map(object):
    def __init__(self, symbols):
        self.symbols = symbols
        self.by_address = {}
        self.by_name = {}
        for s in symbols:
            if s.address in self.by_address:
                sys.exit("abi/symbols.yaml: 0x%x is both %s and %s"
                         % (s.address, self.by_address[s.address].name, s.name))
            if s.name in self.by_name:
                sys.exit("abi/symbols.yaml: %s is at both 0x%x and 0x%x"
                         % (s.name, self.by_name[s.name].address, s.address))
            self.by_address[s.address] = s
            self.by_name[s.name] = s
            for alias in s.aliases:
                self.by_name[alias] = s

    def of_kind(self, *kinds):
        return [s for s in self.symbols if s.kind in kinds]

    def of_class(self, *classes):
        return [s for s in self.symbols if s.klass in classes]

    def address(self, name):
        return self.by_name[name].address

    def in_app(self):
        return [s for s in self.symbols if APP_BASE <= s.address < APP_END]

    def rewrite(self, records, owns, path=None):
        """Replace every entry whose class is in `owns` with `records`.

        A measurement owns its own classes and nothing else, so re-running the
        tool that made them refreshes them in place and leaves the hand map and
        the other tools' classes untouched.

        A derivation may not contradict a hand entry and may not silently
        overwrite one. Where the map already carries the name or the address,
        the map wins and the record is dropped only if the map says what it is
        dropping: `corrects` names the address a body match landed on and
        `supersedes` names the derivation a hand entry replaced. A collision the
        map does not account for is a refusal, because one of the two is wrong
        and which one is not this code's to decide.
        """
        kept = [s for s in self.symbols if s.klass not in owns]
        by_name = {}
        for s in kept:
            for name in [s.name] + s.aliases:
                by_name[name] = s
        by_address = dict((s.address, s) for s in kept)
        corrects = dict((s.name, s.corrects) for s in kept
                        if s.corrects is not None)
        supersedes = dict((s.supersedes, s.address) for s in kept
                          if s.supersedes is not None)

        # An alias is a second name a hand gave the address, not part of the
        # measurement, so it survives the tool that rewrites the entry under it.
        aliases = dict(((s.address, s.name), s.aliases)
                       for s in self.symbols if s.klass in owns and s.aliases)

        rows = [s.row for s in kept]
        displaced = set()
        taken, named = {}, {}
        for record in records:
            address, name = record["address"] & ~1, record["name"]
            if record["class"] not in owns:
                raise Refusal("%s at 0x%x is class %s, which this run does not"
                              " own (%s)"
                              % (name, address, record["class"],
                                 ", ".join(sorted(owns))))
            if name in by_name:
                if corrects.get(name) == address:
                    continue
                mapped = by_name[name].address
                if mapped != address:
                    raise Refusal("%s is at 0x%x here and 0x%x in the map"
                                  % (name, address, mapped))
                continue
            if supersedes.get(name) == address:
                continue
            if address in by_address:
                held = by_address[address]
                if outranks(held.klass, record["class"]):
                    continue
                if not outranks(record["class"], held.klass):
                    raise Refusal("0x%x is %s here and %s in the map"
                                  % (address, name, held.name))
                displaced.add(id(held.row))
            if address in taken:
                raise Refusal("0x%x is %s and %s in the same run"
                              % (address, name, taken[address]))
            if name in named:
                raise Refusal("%s is at 0x%x and 0x%x in the same run"
                              % (name, named[name], address))
            taken[address], named[name] = name, address
            row = dict(record, address=address)
            second = aliases.get((address, name))
            if second:
                row["aliases"] = second
            rows.append(row)
        rows = [row for row in rows if id(row) not in displaced]
        # The map refuses a duplicate address or name on load, so building one
        # is what says the file about to be written can be read back.
        Map([Symbol(row) for row in rows])
        dump(rows, path)
        return len(rows) - len(kept) + len(displaced)

    # An address a hand entry declares it corrects is not a function start: the
    # body match anchored there and the hand entry says where the body really
    # begins, so seeding both would cut the prologue off its own function.
    def corrected(self):
        return set(s.corrects for s in self.symbols if s.corrects is not None)


def load(path=None):
    path = path or os.path.join(HERE, "symbols.yaml")
    with open(path) as fh:
        doc = yaml.safe_load(fh)
    return Map([Symbol(row) for row in doc["symbols"]])


def main():
    import collections
    m = load()
    print("%d symbols, %d in the app image" % (len(m.symbols), len(m.in_app())))
    for label, counter in (("kind", collections.Counter(s.kind for s in m.symbols)),
                           ("class", collections.Counter(s.klass for s in m.symbols))):
        print("  by %s: %s" % (label, ", ".join(
            "%s %d" % kv for kv in counter.most_common())))


if __name__ == "__main__":
    main()
