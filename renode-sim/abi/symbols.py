#!/usr/bin/env python3
"""The address map: what is where in the app image.

    python3 abi/symbols.py                 # a count per kind and per class

abi/symbols.yaml holds one entry per known address. It says nothing about
shape: a prototype, a struct or a table's element type is C and lives in
abi/include/withings/*.h, which abi/types.py reads out of the compiler. The two
meet by name, and this is the only thing that reads the map.
"""

import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
APP_BASE = 0x27000
APP_END = 0xF117C

# A name a hand wrote, as against one a tool derived. The distinction is what
# the precedence fields are about: a derivation may not overwrite a hand entry
# and may not silently contradict one either.
HAND = "hand"


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
