#!/usr/bin/env python3
"""The address map: what is where in the app image.

    python3 abi/symbols.py                 # a count per kind and per class
    python3 abi/symbols.py --check         # every hand entry against the
                                           # measurements on disk

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

# `size` is how far into the object the image itself reaches, in bytes, where a
# measurement establishes it: a record length an accessor was handed, or the
# furthest field offset the code dereferences the address at. It says where the
# object ends and nothing about what is inside it, which is C and lives in
# abi/include/withings/*.h.
ORDER = ["address", "name", "aliases", "kind", "class", "module", "component",
         "size", "corrects", "supersedes", "note", "evidence"]

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
# A derivation never displaces a hand entry and is never silently dropped
# in favour of one: an address the two disagree on is a refusal until the
# map accounts for it. `abi/symbols.py --check` re-runs that over the
# committed file, so a hand entry cannot go stale unnoticed.

"""


def dump(rows, path=None):
    """abi/symbols.yaml, in address order."""
    path = path or os.path.join(HERE, "symbols.yaml")
    ordered = []
    for row in sorted(rows, key=lambda r: (r["address"], r.get("name") or "")):
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
# `kernel` sits with the other readings a table or a call carries: a FreeRTOS
# create is handed the name and the object, so the argument is the evidence.
# `global` is the same kind of reading one step weaker: a dblib persist carries
# the setting id, but the module and the shape of a word come from who touches
# it, which is the partition rather than an argument.
# `runtime` is a line a run printed, which is weaker than every static reading
# of the same address but stronger than a nickname off the call graph.
# `provenance` is a name off the object a body is about -- the global every
# caller points it at, or the one its own literal pool reaches -- plus the verb
# its loads and stores spell. It sits above the nicknames because it says what
# the body operates on, where `shared` and `helper` say only who calls it; a
# body named from a declared struct field plus a verb read off its own uses is
# the same reading `accessor` makes of a three-instruction body.
# `periph` sits with `accessor`: a body whose whole peripheral side is one write
# to a register the SVD names as a task or a mask is named by that register,
# which is a reading of what the body does and not of who calls it.
# `bymodule` is last because it is not a name at all: it attributes an address
# to a module and says nothing that could displace something called something.
RANK = ["match", "libc", "libm", "svc", "syscall", "extlib", "vendor", "string",
        "wppcmd", "codec", "wppobj", "wuiview", "vasistas", "store", "sensor",
        "kernel", "global", "trace", "runtime", "shell", "logtag", "logcb",
        "bleevt", "logline", "slot", "accessor", "periph", "provenance", "shared",
        "helper", "role", "wrapper", "prose", "bymodule"]


# The derivations that settle an address rather than read it: a byte verdict
# against a reference build, an archive body the call graph confirms, a `svc #N`
# whose number is the SoftDevice call, a dispatch-table row that names its own
# handler, a descriptor or a store reading the writer re-checks every run. Where
# one of these holds an address the hand list may not also carry it -- agreeing
# or not, because an agreeing duplicate is what hides which of the two claims
# the repo is standing on.
SETTLED = ("libc", "libm", "extlib", "svc", "syscall", "string",
           "wppcmd", "shell", "bleevt", "codec", "wuiview", "store",
           "vasistas", "sensor", "trace", "kernel", "slot")


# The kinds that name code. A pointer to Thumb code carries the low bit set, so
# an address a table or a call handed a measurement is one past where the thing
# starts; a byte of RAM is at the address it is at, and clearing that bit there
# would move every odd global onto its neighbour.
CODE_KINDS = ("function", "label")


def entry_address(record):
    return (record["address"] & ~1 if record["kind"] in CODE_KINDS
            else record["address"])


def outranks(klass, other):
    """Is `klass` stronger evidence for an address than `other`?"""
    if klass not in RANK or other not in RANK:
        return False
    return RANK.index(klass) < RANK.index(other)


class Symbol(object):
    def __init__(self, row):
        self.address = row["address"]
        # Absent where the entry attributes the address without naming it: a
        # rule may establish which module a body belongs to and what it calls
        # without establishing anything a linker could bind. Such an entry is
        # not a name, so nothing that asks the map what is named sees it.
        self.name = row.get("name")
        # Another name for the same address: the prose map named a table and
        # the hand map named it something else, and both are that table.
        self.aliases = row.get("aliases", [])
        self.kind = row["kind"]
        self.klass = row["class"]
        self.module = row.get("module")
        # Which vendor component owns the address, for the entries a
        # declaration names and for the interiors it only encloses.
        self.component = row.get("component")
        # How many bytes of the object the measurement reached; None where it
        # established no extent.
        self.size = row.get("size")
        self.evidence = row.get("evidence")
        self.corrects = row.get("corrects")
        self.supersedes = row.get("supersedes")
        # What a hand knew about the address that the tool that measures it does
        # not say. `evidence` is the measurement's and is rewritten with it;
        # this is not, and survives the rewrite.
        self.note = row.get("note")
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
            self.by_address[s.address] = s
            if s.name is None:
                continue
            if s.name in self.by_name:
                sys.exit("abi/symbols.yaml: %s is at both 0x%x and 0x%x"
                         % (s.name, self.by_name[s.name].address, s.address))
            self.by_name[s.name] = s
            for alias in s.aliases:
                self.by_name[alias] = s

    def of_kind(self, *kinds):
        """Every named entry of those kinds. An attribution has no name, so it
        is not one of them: a caller asking for the functions the map knows is
        asking what they are called."""
        return [s for s in self.symbols if s.kind in kinds and s.name]

    def attributions(self):
        """The entries that place an address without naming it."""
        return [s for s in self.symbols if s.name is None]

    def of_class(self, *classes):
        return [s for s in self.symbols if s.klass in classes]

    def address(self, name):
        return self.by_name[name].address

    def in_app(self):
        return [s for s in self.symbols if APP_BASE <= s.address < APP_END]

    def rewrite(self, records, owns, path=None, verified=()):
        """Replace every entry whose class is in `owns` with `records`.

        A measurement owns its own classes and nothing else, so re-running the
        tool that made them refreshes them in place and leaves the hand map and
        the other tools' classes untouched.

        `verified` is the subset of the records' addresses this run settled
        rather than read: a byte verdict against a reference build, an archive
        body found where the call graph says it is, a descriptor or a table row
        the run re-checked. The hand list holds what no tool establishes, so an
        address in it is one the hand list may not also carry -- agreeing or
        not. An agreeing duplicate is the worse of the two, because nothing
        says which of the two claims the repo is standing on.

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
            for name in ([s.name] if s.name else []) + s.aliases:
                by_name[name] = s
        by_address = dict((s.address, s) for s in kept)
        # A correction and a supersession are claims about an address, not
        # about the class of the row that carries them, so a row this run is
        # about to rewrite still makes them.
        corrects = dict((s.name, s.corrects) for s in self.symbols
                        if s.corrects is not None and s.name)
        supersedes = dict((s.supersedes, s.address) for s in self.symbols
                          if s.supersedes is not None)

        # An alias is a second name a hand gave the address, not part of the
        # measurement, so it survives the tool that rewrites the entry under it.
        # The same holds for a note, the module partition, and a correction a
        # hand made to where a measurement landed: none of them is the
        # measurement, and all of them outlive a re-run of it.
        carried = {}
        for s in self.symbols:
            if s.klass not in owns:
                continue
            keep = dict((k, s.row[k]) for k in
                        ("aliases", "note", "module", "corrects", "supersedes")
                        if s.row.get(k))
            if keep:
                carried[(s.address, s.name)] = keep

        rows = [s.row for s in kept]
        displaced = set()
        taken, named = {}, {}
        for record in records:
            address, name = entry_address(record), record.get("name")
            if record["class"] not in owns:
                raise Refusal("%s at 0x%x is class %s, which this run does not"
                              " own (%s)"
                              % (name, address, record["class"],
                                 ", ".join(sorted(owns))))
            if name is None:
                # An attribution claims no name, so it collides with nothing by
                # name and yields the address to anything that has one.
                if address in by_address or address in taken:
                    continue
                taken[address] = None
                rows.append(dict(record, address=address))
                continue
            if name in by_name:
                if corrects.get(name) == address:
                    continue
                held = by_name[name]
                if held.address != address:
                    raise Refusal("%s is at 0x%x here and 0x%x in the map"
                                  % (name, address, held.address))
                if held.klass == HAND and address in verified:
                    raise Refusal(
                        "0x%x is %s by hand and %s by this run, which settles"
                        " it -- %s.\n    Delete the hand entry; anything it"
                        " says that the record does not goes in `note`, which"
                        " survives the rewrite."
                        % (address, name, name,
                           record.get("evidence") or "no evidence recorded"))
                continue
            if supersedes.get(name) == address:
                continue
            if address in by_address:
                held = by_address[address]
                if held.klass == HAND:
                    raise Refusal(
                        "0x%x is %s (%s) in the map and %s here -- %s.\n"
                        "    A hand entry is neither displaced by a derivation"
                        " nor silently kept over one: either delete it, or give"
                        " it `supersedes: %s` and the reason it stands."
                        % (address, held.name, held.klass, name,
                           record.get("evidence") or "no evidence recorded",
                           name))
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
            for key, value in carried.get((address, name), {}).items():
                row.setdefault(key, value)
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


def measurements():
    """Every name a measurement on disk proposes, as (name, address) pairs.

    abi/matches.yaml and abi/autonames.yaml are the two measurements that write
    their records out as files; the rest (abi/stores.py, abi/wui_views.py,
    abi/protocol.py) re-derive theirs from the image on every run and refuse
    through `rewrite` as they go.
    """
    proposed = []
    for name in ("matches.yaml", "autonames.yaml"):
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            doc = yaml.safe_load(fh)
        for row in doc.get("functions") or []:
            if not row.get("name"):
                continue           # an attribution proposes no name to check
            settled = (row.get("verdict") in ("exact", "masked")
                       or row.get("class") in SETTLED)
            proposed.append((row["name"], int(row["address"]) & ~1, settled))
    return proposed


def check(m):
    """The refusal `rewrite` makes, re-run over the map as it was committed.

    A hand entry is neither displaced by a derivation nor silently kept over
    one, so every address where the two disagree has to be accounted for in the
    map: `supersedes` names the derivation the hand entry replaced, `corrects`
    the address a body match landed on. The claim is held to the measurement it
    was made against rather than standing on its own, so a derivation that
    moves to another name re-raises here instead of the stale claim winning.

    A `supersedes` the current run no longer proposes is not by itself a
    finding: several of the derivation rules read the map, so a hand entry
    changes what would have been derived at its own address and the name it
    replaced cannot be re-proposed while it stands.
    """
    at = collections.defaultdict(set)
    settled = {}
    for name, address, is_settled in measurements():
        at[address].add(name)
        if is_settled:
            settled[address] = name
    bad = []
    for s in m.symbols:
        if s.klass != HAND:
            continue
        if s.address in settled:
            bad.append("0x%x is %s by hand and %s by a measurement that"
                       " settles it. The hand list holds what no tool"
                       " establishes: delete the entry, and move anything it"
                       " says that the record does not into `note`."
                       % (s.address, s.name, settled[s.address]))
            continue
        others = at.get(s.address, set()) - {s.name}
        if others and s.supersedes not in others and s.corrects is None:
            bad.append("0x%x is %s in the map and %s in the measurement, which"
                       " the entry does not account for: it supersedes %s."
                       " Delete the entry, or give it `supersedes: %s` and the"
                       " reason it stands."
                       % (s.address, s.name, ", ".join(sorted(others)),
                          s.supersedes or "nothing", sorted(others)[0]))
    return bad


def main():
    import collections
    m = load()
    if "--check" in sys.argv:
        bad = check(m)
        for line in bad:
            print("abi/symbols.yaml: %s" % line)
        print("%d hand entries, %d measured names, %d unaccounted"
              % (len(m.of_class(HAND)), len(measurements()), len(bad)))
        return sys.exit(1 if bad else 0)
    print("%d symbols, %d in the app image" % (len(m.symbols), len(m.in_app())))
    for label, counter in (("kind", collections.Counter(s.kind for s in m.symbols)),
                           ("class", collections.Counter(s.klass for s in m.symbols))):
        print("  by %s: %s" % (label, ", ".join(
            "%s %d" % kv for kv in counter.most_common())))


if __name__ == "__main__":
    main()
