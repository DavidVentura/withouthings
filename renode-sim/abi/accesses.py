#!/usr/bin/env python3
"""What each function touches: the named globals and registers, and how.

    python3 abi/accesses.py                        # rebuild abi/out/accesses.json
    python3 abi/accesses.py --report saadc_read    # one function's side
    python3 abi/accesses.py --report NRF_SAADC     # one object's side
    python3 abi/accesses.py --vertical SAADC       # the vertical above and below it
    python3 abi/accesses.py --vertical-all         # every peripheral, as a table
    python3 abi/accesses.py --emit|--run|--observe # the runtime cross-check

The map says what a body is called and abi/ghidra/word_uses.py says what it does
with each word it loads, but nothing put the two together: asking "who writes
this RAM item" or "what does this function touch" meant reading three exports
by hand. This is that join, in both directions.

An access is derived from one pool word and the forward data flow out of the
instruction that loads it. The word's value says which object -- a RAM item
from abi/ramparts.py's partition, a register from abi/peripherals.py's map --
and the flow summary says whether the value was loaded through, stored through,
called, or only passed on. A peripheral register adds the half the flow cannot
see: the SVD states whether the hardware allows a read or a write at all, so a
write-only task register touched by a function that both loads and stores its
peripheral's base is a write.

The flow summary is a closure over the call graph -- a word handed to a callee
carries the callee's uses back -- so a function's side is what its own code and
the code it calls do with the addresses it holds. That is the reading the
plate comment wants: `saadc_configure` starting NRF_SAADC is the fact, whether
the store is in its body or one call down.

The static side is blind to an address the code computes or keeps in a struct
field, so abi/accesses.py --run reruns five scenarios with Renode logging every
access to the app's peripherals. Renode's log carries the PC of each access, so
each one is attributed to the function it happened in and compared with the
static index: what only the run saw is added with `observed` evidence, and what
only the index claims is kept and flagged.

Writes abi/out/accesses.json, which nothing commits: it is derived from the
export, the map and the SVD, all of which are in the repo.
"""

import argparse
import bisect
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import algos  # noqa: E402
import observe_calls as oc  # noqa: E402
import peripherals  # noqa: E402
import ramparts  # noqa: E402
import shapes  # noqa: E402
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
OUT = os.path.join(HERE, "out", "ghidra")
RUNTIME = os.path.join(SIM, "out", "accesses", "rt")

RAM_BASE, RAM_END = 0x20000000, 0x20040000


# How an address is used. The order is the order the evidence is read in: a
# branch through the value is the strongest thing to say about it, and "the
# flow lost it" is the weakest.
CALLED = "called"
READ_MODIFY_WRITE = "read-modify-write"
WRITE = "write"
READ = "read"
ADDRESS_TAKEN = "address-taken"
UNKNOWN = "unknown"

# The flow summary's names for the operations that decide the kind. The rest of
# the vocabulary (`compare`, `offset`, `stride:N`, `field+d:w`) says nothing
# about direction.
PASSED_ON = frozenset(("argument_opaque", "stored_value", "returned"))

FIELD_USE = re.compile(r"^field([+-]\d+):(\d+)$")
REGISTER_NAME = re.compile(r"^NRF_\w+->(\w+)")

# The verb each access kind is spelled with where the index is read out loud:
# a bookmark on a function, a coverage line, a derived name.
VERBS = {"read": "reads", "write": "writes",
         "read-modify-write": "reads and writes", "called": "calls through",
         "address-taken": "passes", "unknown": "holds"}


def kind_of(uses):
    """The access kind a word's forward-flow summary establishes."""
    if "call" in uses:
        return CALLED
    load, store = "load_base" in uses, "store_base" in uses
    if load and store:
        return READ_MODIFY_WRITE
    if store:
        return WRITE
    if load:
        return READ
    if uses & PASSED_ON:
        return ADDRESS_TAKEN
    return UNKNOWN


def narrow(kind, access):
    """A register's access kind, once the SVD says what the hardware allows.

    A driver holds one base and reaches every register through it, so the flow
    summary's read-and-write is about the block and not about the register. The
    SVD settles the ones the hardware only permits one way: a TASKS_ register
    is write-only and an EVENTS_ or a result register is read-only.
    """
    if kind in (CALLED, ADDRESS_TAKEN, UNKNOWN):
        return kind
    if access == "write-only":
        return WRITE
    if access == "read-only":
        return READ
    return kind


class Access(object):
    """One function touching one object, with how and on what evidence."""

    __slots__ = ("object", "address", "target", "kind", "evidence", "observed")

    def __init__(self, object_name, address, target, kind, evidence,
                 observed=False):
        self.object = object_name
        # Where the object starts, which is what the index is keyed by.
        self.address = address
        # The byte the access names, which is the register or the field.
        self.target = target
        self.kind = kind
        self.evidence = evidence
        self.observed = observed

    @property
    def phrase(self):
        """The access as a clause: "triggers NRF_SAADC->TASKS_START".

        The register's own name carries the verb, so it is read off the object
        rather than looked up again: writing a task register is triggering it
        and writing an event register is clearing it, and calling either of
        them a write says less than the register already does. It is written
        into the index because its consumers -- a Ghidra script in a venv with
        no repo modules in it, a coverage line -- should not have to rebuild
        the vocabulary to print a sentence.
        """
        matched = REGISTER_NAME.match(self.object)
        if matched and self.kind in (WRITE, READ_MODIFY_WRITE):
            if matched.group(1).startswith("TASKS_"):
                return "triggers %s" % self.object
            if matched.group(1).startswith("EVENTS_"):
                return "clears %s" % self.object
        return "%s %s" % (VERBS[self.kind], self.object)

    def row(self):
        return {"object": self.object, "address": self.address,
                "target": self.target, "kind": self.kind,
                "phrase": self.phrase,
                "evidence": self.evidence, "observed": self.observed}

    def __repr__(self):
        return "<%s %s %s>" % (self.kind, self.object, self.evidence)


class Side(object):
    """Everything one function touches."""

    __slots__ = ("start", "name", "accesses")

    def __init__(self, start, name, accesses):
        self.start = start
        self.name = name
        self.accesses = accesses

    @property
    def registers(self):
        return [a for a in self.accesses if a.address >= 0x40000000
                or 0x10000000 <= a.address < 0x10002000]

    @property
    def globals(self):
        return [a for a in self.accesses if RAM_BASE <= a.address < RAM_END]

    def row(self):
        return {"start": self.start, "name": self.name,
                "accesses": [a.row() for a in self.accesses]}


class Program(object):
    """The exports, the map and the chip, joined once."""

    def __init__(self, export=OUT):
        with open(os.path.join(export, "items.json")) as fh:
            self.items = json.load(fh)
        with open(os.path.join(export, "references.json")) as fh:
            self.refs = json.load(fh)
        with open(os.path.join(export, "words.json")) as fh:
            self.words = {w["addr"]: w for w in json.load(fh)["words"]}
        with open(os.path.join(export, "word_uses.json")) as fh:
            flow = json.load(fh)
        self.uses = {u["addr"]: frozenset(u["uses"]) for u in flow["uses"]}
        self.map = symmap.load()
        self.chip = peripherals.load()
        self.ram = ramparts.load(export, os.path.join(SIM, "appl.bin"),
                                 self.map, shapes.load())
        named = {s.address: s.name for s in self.map.symbols if s.name}
        self.functions = {}
        for f in self.items["functions"]:
            self.functions[f["start"]] = named.get(f["start"], f["name"])
        self.starts = sorted(self.functions)
        self.reads = collections.defaultdict(list)
        for read in self.refs["pool_reads"]:
            self.reads[read["function"]].append(read)

    def owner(self, pc):
        """The function an arbitrary pc is in, or None."""
        i = bisect.bisect_right(self.starts, pc) - 1
        if i < 0:
            return None
        start = self.starts[i]
        for span in self.items["function_ranges"]:
            if span["function"] == start and span["start"] <= pc < span["end"]:
                return start
        return start if pc < start + 0x4000 else None


def ram_accesses(program, word, uses, where):
    """The RAM item a word names, and every field of it the code reaches."""
    item = program.ram.at(word["value"])
    if item is None:
        return []
    kind = kind_of(uses)
    out = [Access(item.name, item.start, word["value"], kind, where)]
    for use in uses:
        matched = FIELD_USE.match(use)
        if not matched:
            continue
        at = word["value"] + int(matched.group(1))
        inner = program.ram.at(at)
        if inner is None or inner.start == item.start:
            continue
        out.append(Access(inner.name, inner.start, at, kind,
                          "%s, reached at %+d"
                          % (where, int(matched.group(1)))))
    return out


def register_accesses(program, word, uses, where):
    """The registers a word names: itself, or the ones its readers select."""
    located = program.chip.at(word["value"])
    if located is None:
        return []
    kind = kind_of(uses)
    if located.peripheral.base != word["value"]:
        return [Access(located.name, located.address, located.address,
                       narrow(kind, located.access), where)]
    out = []
    for use in uses:
        matched = FIELD_USE.match(use)
        if not matched:
            continue
        at = program.chip.register_after(word["value"], int(matched.group(1)))
        if at is None or at.register is None:
            continue
        out.append(Access(at.name, at.address, at.address,
                          narrow(kind, at.access),
                          "%s, at %+d" % (where, int(matched.group(1)))))
    if not out:
        # The block itself, with nothing saying which register: the word is
        # held by an interrupt handler that passes it on, or the flow lost it.
        out.append(Access("NRF_%s" % located.peripheral.name,
                          located.peripheral.base, located.peripheral.base,
                          kind, where))
    return out


def build(program):
    """The static index: one Side per function that touches anything."""
    sides = {}
    for start in sorted(program.reads):
        if start not in program.functions:
            continue
        found = {}
        for read in program.reads[start]:
            word = program.words.get(read["target"])
            if word is None:
                continue
            uses = program.uses.get(read["target"], frozenset())
            where = "pool word 0x%x read at 0x%x" % (read["target"],
                                                     read["site"])
            if RAM_BASE <= word["value"] < RAM_END:
                got = ram_accesses(program, word, uses, where)
            elif word["signal"] == "peripheral":
                got = register_accesses(program, word, uses, where)
            else:
                continue
            for access in got:
                # One function reaching the same object through two pool words
                # is one access; the stronger kind is the one that holds,
                # because a read and a write of the same object is both.
                key = (access.object, access.target)
                if key not in found or stronger(access.kind,
                                                found[key].kind):
                    found[key] = access
        if found:
            sides[start] = Side(start, program.functions[start],
                                sorted(found.values(),
                                       key=lambda a: (a.address, a.target)))
    return sides


ORDER = [UNKNOWN, ADDRESS_TAKEN, READ, WRITE, READ_MODIFY_WRITE, CALLED]


def stronger(kind, than):
    return ORDER.index(kind) > ORDER.index(than)


def load_index(path=None):
    """abi/out/accesses.json, for the consumers that only read it."""
    path = path or os.path.join(HERE, "out", "accesses.json")
    if not os.path.exists(path):
        sys.exit("%s does not exist; run python3 abi/accesses.py" % path)
    with open(path) as fh:
        found = json.load(fh)
    sides = {}
    for row in found["functions"]:
        sides[row["start"]] = Side(
            row["start"], row["name"],
            [Access(a["object"], a["address"], a["target"], a["kind"],
                    a["evidence"], a["observed"]) for a in row["accesses"]])
    return sides, found["summary"]


def by_object(sides):
    out = collections.defaultdict(list)
    for side in sides.values():
        for access in side.accesses:
            out[access.object].append((side.start, access))
    return out


# ---------------------------------------------------------------- the runtime

# Renode's own peripheral-access log, which carries the PC of every access:
#   saadc: [cpu: 0xA4E46] WriteUInt32 to 0x100 (unknown), value 0x0
# It costs nothing per access beyond the log line, where a watchpoint hook
# costs an IronPython call, and it is the only one of the two that sees an
# access the static side missed, because it is registered on the peripheral
# rather than on an address the index already knows.
LOG_LINE = re.compile(
    r"^\[[^\]]*\]\s+\[INFO\]\s+(\w+): \[cpu: 0x([0-9A-Fa-f]+)\] "
    r"(Read|Write)U?Int(\d+) (?:from|to) 0x([0-9A-Fa-f]+)")

REPL_INSTANCE = re.compile(
    r"^(\w+):\s*[\w.]+\s*@\s*sysbus\s*(0x[0-9A-Fa-f]+)", re.M)


class Instance(object):
    """One Renode peripheral instance: its name, where it is, and what it is."""

    __slots__ = ("name", "base", "peripheral")

    def __init__(self, name, base, peripheral):
        self.name = name
        self.base = base
        self.peripheral = peripheral


def instances(chip, repl=None):
    """hwa10.repl's sysbus peripherals, resolved against the register map."""
    text = open(repl or os.path.join(SIM, "hwa10.repl")).read()
    out = []
    for name, base in REPL_INSTANCE.findall(text):
        at = int(base, 0)
        try:
            located = chip.at(at)
        except peripherals.Ambiguous:
            continue
        if located is not None:
            out.append(Instance(name, at, located.peripheral))
    return out


SHELL = [("gpio", 3), ("rtc", 3), ("mcu_temp", 5), ("get_battery", 5),
         ("ecb", 5), ("trig", 10), ("quartz_calib", 30), ("xtal32m", 15),
         ("digital_crown", 5), ("calib_test test 1 1", 30)]

SCENARIOS = collections.OrderedDict(
    (s.name, s) for s in [
        algos.Scenario("display",
                       "the watch face and everything the UI polls: the"
                       " display's SPI, the crown, the battery sense and the"
                       " tick",
                       body=['emulation RunFor "60.0"']),
        algos.Scenario("ecg", "an ECG measurement over the pipe",
                       setup=algos.WORN, client=["--ecg", "30"]),
        algos.Scenario("hr", "a heart-rate measurement over the pipe",
                       setup=algos.WORN, client=["--ppg", "30"]),
        algos.Scenario("bodytemp",
                       "the core-body-temperature algorithm, which is the"
                       " I2C bus and the heat-flux bridge",
                       setup=algos.WORN + algos.SKIN + algos.OFF_CHARGER,
                       shell=[("body_temperature init", 5),
                              ("body_temperature start", 60),
                              ("body_temperature kickstart", 120),
                              ("body_temperature stop", 5)]),
        algos.Scenario("shell",
                       "the shell commands that drive a peripheral directly:"
                       " the GPIOs, the RTC, the die thermometer, the battery"
                       " sense, the crypto block, the trigger network and the"
                       " two clock calibrations",
                       setup=algos.WORN, shell=SHELL),
    ])

# The scenarios share abi/algos.py's port allocation, so they have to be in its
# table or they have no monitor port and nothing can drive them.
algos.SCENARIOS = SCENARIOS


def log_path(runtime, name):
    return os.path.abspath(os.path.join(runtime, "out", "accesses",
                                        "mmio-%s.log" % name))


def logging_block(watched, runtime, name):
    """The monitor lines that turn the access log on, and send it to a file."""
    lines = ['logFile @%s true' % log_path(runtime, name)]
    for instance in watched:
        lines.append("logLevel 0 sysbus.%s" % instance.name)
        lines.append("sysbus LogPeripheralAccess sysbus.%s true" % instance.name)
    return "\n".join(lines) + "\n"


def emit(runtime, watched):
    algos.prepare(runtime)
    os.makedirs(os.path.join(runtime, "out", "accesses"), exist_ok=True)
    return [algos.write_scenario(s, runtime,
                                 logging_block(watched, runtime, s.name))
            for s in SCENARIOS.values()]


class Observation(object):
    """One (peripheral register, pc, direction) a run saw."""

    __slots__ = ("address", "pc", "kind", "scenario", "count")

    def __init__(self, address, pc, kind, scenario, count):
        self.address = address
        self.pc = pc
        self.kind = kind
        self.scenario = scenario
        self.count = count


def read_log(path, scenario, by_name):
    """The distinct (register, pc, direction) triples one run's log holds."""
    seen = collections.Counter()
    if not os.path.exists(path):
        return []
    with open(path, errors="replace") as fh:
        for line in fh:
            matched = LOG_LINE.match(line)
            if not matched:
                continue
            name, pc, direction, _, offset = matched.groups()
            instance = by_name.get(name)
            if instance is None:
                continue
            seen[(instance.base + int(offset, 16), int(pc, 16),
                  READ if direction == "Read" else WRITE)] += 1
    return [Observation(at, pc, kind, scenario, count)
            for (at, pc, kind), count in seen.items()]


def observed_index(program, runtime, watched):
    """Every observation of every scenario, attributed to a function."""
    by_name = {i.name: i for i in watched}
    out = []
    for name in SCENARIOS:
        for seen in read_log(log_path(runtime, name), name, by_name):
            start = program.owner(seen.pc)
            if start is None:
                continue
            out.append((start, seen))
    return out


def cross_check(program, sides, observations, watched):
    """Fold the runs into the static index, and count both disagreements.

    An access only the run saw is added: the index derives an access from a
    pool word, and a driver that keeps its base in a struct field or computes
    it from an instance number holds no such word, so there was nothing for the
    static side to read. An access only the index claims is kept and flagged,
    because a scenario not exercising a path is not evidence the path is not
    there.
    """
    added, confirmed = 0, 0
    static = {}
    for start, side in sides.items():
        for access in side.accesses:
            static[(start, access.target)] = access
    for start, seen in observations:
        located = program.chip.at(seen.address)
        if located is None:
            continue
        known = static.get((start, seen.address))
        if known is not None:
            confirmed += 1
            continue
        access = Access(located.name, located.address, located.address,
                        narrow(seen.kind, located.access),
                        "observed in the %s run at pc 0x%x, %d times"
                        % (seen.scenario, seen.pc, seen.count), observed=True)
        static[(start, seen.address)] = access
        name = program.functions.get(start, "0x%x" % start)
        sides.setdefault(start, Side(start, name, []))
        sides[start].accesses.append(access)
        added += 1
    # Only the peripherals the runs watched can be counted the other way: a RAM
    # access no run saw is not a disagreement, because no run was looking.
    seen_blocks = set(i.peripheral.name for i in watched)
    ran = set((start, seen.address) for start, seen in observations)
    unseen = 0
    for start, side in sides.items():
        for access in side.accesses:
            if access.observed:
                continue
            located = program.chip.at(access.address)
            if located is None or located.peripheral.name not in seen_blocks:
                continue
            if (start, access.target) not in ran:
                unseen += 1
    return {"observed_only": added, "confirmed": confirmed,
            "static_only": unseen}


# ------------------------------------------------------------- the verticals

# Where a vertical starts. Each is a place something outside the app hands
# control in: the scheduler's tasks, the vector table, a dispatch row the
# protocol or the console indexes, and the view callbacks the UI runs.
ROOT_CLASSES = {"kernel": "task", "vector": "vector", "wppcmd": "wpp command",
                "shell": "shell command", "slot": "dispatch slot",
                "wuiview": "view callback", "bleevt": "ble event"}
ROOT_NAMES = re.compile(r"^(vector_\d+_handler|Reset_Handler|.*_IRQHandler)$")

# Where a vertical ends: a place the value leaves the app. A store writes it to
# flash, a vasistas send or a wpp encoder puts it on the air, a wui call puts
# it on the screen, and a log line puts it on the console.
SINK_CLASSES = {"store": "store", "vasistas": "vasistas", "codec": "wpp codec"}
SINK_NAMES = [(re.compile(r"^wpp_.*(send|reply|answer|notify)"), "wpp send"),
              (re.compile(r"^wui_.*(push|post|show|draw|refresh)"), "wui push")]
WLOG_ENTRIES = (0x8F460, 0x8F494, 0x9B1C8, 0x5E7A8, 0x50CA8)

UNNAMED = re.compile(r"^(FUN|LAB|block|caseD|thunk|sliver)_")


class Graph(object):
    """The call graph, plus what the map says a function is."""

    def __init__(self, program):
        self.program = program
        self.callers = collections.defaultdict(set)
        self.callees = collections.defaultdict(set)
        for call in program.refs["calls"]:
            if call.get("kind") not in ("call", "jump"):
                continue
            src, dst = call.get("function"), call["to"]
            if src in program.functions and dst in program.functions:
                if src == dst:
                    continue
                self.callers[dst].add(src)
                self.callees[src].add(dst)
        self.klass = {s.address: s.klass for s in program.map.symbols}
        self.attributed = set(s.address for s in program.map.attributions())
        self.logs = set()
        for call in program.refs["calls"]:
            if call["to"] in WLOG_ENTRIES and call.get("function") is not None:
                self.logs.add(call["function"])

    def root(self, start):
        """What kind of entry point `start` is, or None."""
        by_class = ROOT_CLASSES.get(self.klass.get(start))
        if by_class:
            return by_class
        name = self.program.functions.get(start, "")
        return "vector" if ROOT_NAMES.match(name) else None

    def sink(self, start):
        by_class = SINK_CLASSES.get(self.klass.get(start))
        if by_class:
            return by_class
        name = self.program.functions.get(start, "")
        for pattern, what in SINK_NAMES:
            if pattern.search(name):
                return what
        return "log line" if start in self.logs else None


def vertical(program, graph, sides, name):
    """Every function on one peripheral's vertical, with its roots and sinks.

    The seed is the functions whose side touches the peripheral; the vertical
    is that set closed upwards through the callers to the roots and downwards
    through the callees to the sinks, which is what "everything between the
    register and the outside world" means when the only map is a call graph.
    """
    wanted = "NRF_%s" % name
    seeds = set(start for start, side in sides.items()
                if any(a.object == wanted or a.object.startswith(wanted + "->")
                       for a in side.accesses))
    if not seeds:
        return None
    up, frontier = set(seeds), set(seeds)
    roots = {}
    while frontier:
        nxt = set()
        for start in frontier:
            what = graph.root(start)
            if what:
                roots[start] = what
                continue
            for caller in graph.callers[start]:
                if caller not in up:
                    up.add(caller)
                    nxt.add(caller)
        frontier = nxt
    down, frontier = set(seeds), set(seeds)
    sinks = {}
    while frontier:
        nxt = set()
        for start in frontier:
            what = graph.sink(start)
            if what and start not in seeds:
                sinks[start] = what
                continue
            for callee in graph.callees[start]:
                if callee not in down:
                    down.add(callee)
                    nxt.add(callee)
        frontier = nxt
    members = up | down
    unnamed = set(a for a in members if UNNAMED.match(program.functions[a]))
    return {"peripheral": name, "seeds": sorted(seeds),
            "members": sorted(members), "unnamed": sorted(unnamed),
            "attribution_only": sorted(unnamed & graph.attributed),
            "roots": roots, "sinks": sinks}


def print_vertical(program, found):
    print("NRF_%s: %d functions on the vertical, %d of them unnamed (%d of"
          " those carry only an attribution)"
          % (found["peripheral"], len(found["members"]), len(found["unnamed"]),
             len(found["attribution_only"])))
    seeds = set(found["seeds"])
    unnamed = set(found["unnamed"])
    only = set(found["attribution_only"])
    for start in found["members"]:
        mark = ("touches" if start in seeds else
                "root" if start in found["roots"] else
                "sink" if start in found["sinks"] else "")
        what = found["roots"].get(start) or found["sinks"].get(start) or ""
        print("  0x%08x %-40s %-8s %-14s%s"
              % (start, program.functions[start], mark, what,
                 " (attribution only)" if start in only else
                 " (unnamed)" if start in unnamed else ""))


# ------------------------------------------------------------------- reports

def report(program, sides, objects, what):
    by_name = {side.name: side for side in sides.values()}
    if what in by_name:
        side = by_name[what]
        print("%s at 0x%x touches %d objects"
              % (side.name, side.start, len(side.accesses)))
        for access in side.accesses:
            print("  %-18s %-34s %s"
                  % (access.kind, access.object, access.evidence))
        return 0
    if what in objects:
        rows = objects[what]
        print("%s is touched by %d functions" % (what, len(rows)))
        for start, access in sorted(rows, key=lambda r: program.functions[r[0]]):
            print("  %-18s %-40s %s"
                  % (access.kind, program.functions[start], access.evidence))
        return 0
    sys.exit("%s is neither a function the index knows nor an object it holds"
             % what)


def write(path, sides, objects, summary):
    with open(path, "w") as fh:
        json.dump({"_generated": "by abi/accesses.py, do not edit: what every"
                                 " function touches and who touches every"
                                 " object",
                   "summary": summary,
                   "functions": [sides[s].row() for s in sorted(sides)],
                   "objects": {name: sorted(set(start for start, _ in rows))
                               for name, rows in sorted(objects.items())}},
                  fh, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default=OUT)
    ap.add_argument("--out", default=os.path.join(HERE, "out", "accesses.json"))
    ap.add_argument("--runtime", default=RUNTIME)
    ap.add_argument("--report")
    ap.add_argument("--vertical")
    ap.add_argument("--vertical-all", action="store_true")
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--scenario")
    ap.add_argument("--observe", action="store_true",
                    help="fold the runs already on disk into the index")
    args = ap.parse_args()

    program = Program(args.export)
    sides = build(program)
    runtime = os.path.abspath(args.runtime)
    touched = set(program.chip.at(a.address).peripheral.name
                  for side in sides.values() for a in side.registers
                  if program.chip.at(a.address))
    watched = [i for i in instances(program.chip)
               if i.peripheral.name in touched]

    if args.emit or args.run:
        emit(runtime, watched)
    if args.run:
        for scenario in SCENARIOS.values():
            if args.scenario and scenario.name != args.scenario:
                continue
            counts = os.path.abspath(os.path.join(runtime, "out", "algos",
                                                  "counts-%s.txt" % scenario.name))
            dump = os.path.join(runtime, "dump-%s.py" % scenario.name)
            path = log_path(runtime, scenario.name)
            if os.path.exists(path):
                os.remove(path)
            took = algos.run_scenario(scenario, runtime, counts, dump)
            print("%s: %d distinct accesses in %.0f s"
                  % (scenario.name,
                     len(read_log(path, scenario.name,
                                  {i.name: i for i in watched})), took))

    crossed = None
    if args.observe or args.run:
        crossed = cross_check(program, sides,
                              observed_index(program, runtime, watched),
                              watched)

    objects = by_object(sides)
    summary = {
        "functions": len(sides),
        "accesses": sum(len(s.accesses) for s in sides.values()),
        "ram_objects": sum(1 for name in objects
                           if any(a.address >= RAM_BASE
                                  for _, a in objects[name])),
        "registers": sum(1 for name in objects if name.startswith("NRF_")),
        "peripherals": len(touched),
        "kinds": dict(collections.Counter(a.kind for s in sides.values()
                                          for a in s.accesses)),
    }
    if crossed:
        summary["runtime"] = crossed
    write(args.out, sides, objects, summary)

    if args.report:
        return report(program, sides, objects, args.report)
    if args.vertical or args.vertical_all:
        graph = Graph(program)
        if args.vertical:
            found = vertical(program, graph, sides, args.vertical)
            if found is None:
                sys.exit("no function's side touches NRF_%s" % args.vertical)
            print_vertical(program, found)
            return 0
        print("%-10s %9s %8s %12s %s"
              % ("peripheral", "functions", "unnamed", "attrib-only",
                 "roots / sinks"))
        for name in sorted(touched):
            found = vertical(program, graph, sides, name)
            if found is None:
                continue
            roots = collections.Counter(found["roots"].values())
            sinks = collections.Counter(found["sinks"].values())
            print("%-10s %9d %8d %12d  %s | %s"
                  % (name, len(found["members"]), len(found["unnamed"]),
                     len(found["attribution_only"]),
                     ", ".join("%d %s" % (n, k) for k, n in roots.most_common())
                     or "none",
                     ", ".join("%d %s" % (n, k) for k, n in sinks.most_common())
                     or "none"))
        return 0

    print("%s: %d functions, %d accesses over %d RAM objects and %d registers"
          % (args.out, summary["functions"], summary["accesses"],
             summary["ram_objects"], summary["registers"]))
    for kind, count in sorted(summary["kinds"].items()):
        print("  %-18s %d" % (kind, count))
    if crossed:
        print("  runtime: %d observed-only, %d confirmed, %d static-only"
              % (crossed["observed_only"], crossed["confirmed"],
                 crossed["static_only"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
