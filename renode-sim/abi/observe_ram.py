#!/usr/bin/env python3
"""Who first writes into a static RAM item no flash reader names.

    python3 abi/observe_ram.py --runtime <scratch dir> --emit
    python3 abi/observe_ram.py --runtime <scratch dir> --run [--scenario NAME]
    python3 abi/observe_ram.py --runtime <scratch dir> --report

abi/globals.py names a RAM word from the instructions that dereference it, and
abi/kernel_objects.py names the buffers a create is handed. Neither reaches an
item the image only ever addresses through a handle or through a pointer it was
given: a task's stack, a queue's storage, the context a vendor algorithm is
handed once and keeps. Those items have no static reader at all, so nothing in
the image says whose they are.

Execution does. A store is a claim of ownership by whoever ran it, so the first
write into an item, with the PC that made it and the task that was running,
names the item's owner the way a create names a stack: the kernel's own copy
writes a queue's storage on behalf of a sender, a task's stack is written by
the create and then only ever by that task, and an algorithm's context is
written first by its init.

The observation is one write watchpoint per item on its first byte, one per
access width, and it records the first write only -- an item written in a loop would otherwise
cost an IronPython call per iteration, which is what abi/observe_words.py
measured the display run at ten minutes for. The running task comes from
pxCurrentTCB, which PendSV_Handler's own pool word establishes, and its name
string from the TCB's pcTaskName.

The scenarios are abi/algos.py's, because they are the ones that start the
algorithms this has no other way in to, plus the three driven paths algos has
no reason to carry: the watch face on its own, an ECG measurement and the
phone's ordinary traffic. This script only replaces the hook block.
"""

import argparse
import bisect
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import algos  # noqa: E402  (the scenarios, the scratch copy and the drivers)
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
RUNTIME = os.path.join(SIM, "out", "observe-ram", "rt")

# FreeRTOS's current task pointer and the name it carries. The pointer is the
# word PendSV_Handler reads through its own pool (0x27eb0) to save the outgoing
# stack into; the offset is this build's TCB layout, whose 0x60 bytes the
# static TCBs the creates are handed confirm.
PX_CURRENT_TCB = 0x200219FC
PC_TASK_NAME = 0x34
TASK_NAME_LEN = 16

# One watchpoint per width, because a watchpoint matches the access width it
# was registered for and the first write into an item is as likely to be a byte
# store from a memset as a word store from an initialiser.
WIDTHS = ("Byte", "Word", "DoubleWord")

# How many distinct (pc, lr) writers to record per item before the hook goes
# quiet. One is not enough: the first write into an object is often a generic
# clear its owner asks a library for, and the caller the clear was made from is
# the fact wanted. Beyond a handful the cost per write is what matters, and the
# owner is settled by then.
KEEP = 4

WATCH = """sysbus AddWatchpointHook 0x%(at)x %(width)s Write \"\"\"
import sys
if not hasattr(sys, 'obs_ram'): sys.obs_ram = dict()
bus = cpu.GetMachine().SystemBus
tcb = bus.ReadDoubleWord(0x%(tcb)x)
if tcb != 0 and sys.obs_ram.get(0x%(at)x, 0) < %(keep)d:
    pc = cpu.GetRegisterUnsafe(15).RawValue
    lr = cpu.GetRegisterUnsafe(14).RawValue & 0xfffffffe
    key = (0x%(at)x, pc, lr)
    if key not in sys.obs_ram:
        sys.obs_ram[key] = 1
        sys.obs_ram[0x%(at)x] = sys.obs_ram.get(0x%(at)x, 0) + 1
        s = ''
        try:
            i = 0
            while i < %(namelen)d:
                b = bus.ReadByte(tcb + 0x%(nameoff)x + i)
                if b == 0: break
                s += '%%02x' %% b
                i += 1
        except:
            s = ''
        f = open('%(out)s', 'a')
        f.write('%%x %%x %%x %%x %%s %%d\\n' %% (0x%(at)x, pc, lr, tcb, s or '-', cpu.ExecutedInstructions))
        f.close()
\"\"\"
"""


# abi/algos.py's scenarios are the ones that start the dormant algorithms;
# these three are the driven paths, which no algos scenario covers and which own
# a good part of the RAM: the watch face and its carousel, an ECG measurement,
# and the phone's ordinary traffic.
EXTRA = [
    algos.Scenario("display",
                   "the watch face, the carousel timer and everything the UI"
                   " polls, with no input at all",
                   body=['emulation RunFor "60.0"']),
    algos.Scenario("ecg", "an ECG measurement over the pipe",
                   setup=algos.WORN, client=["--ecg", "30"]),
    algos.Scenario("wpp", "the phone: the challenge and a time write",
                   client=["--set-time"]),
]

SCENARIOS = collections.OrderedDict(
    list(algos.SCENARIOS.items()) + [(s.name, s) for s in EXTRA])

# abi/algos.py derives a run's monitor port from a scenario's position in its
# own table, so the three extra ones have to be in it or they have no port and
# nothing can drive them. The order of its own entries is unchanged, so its
# scenarios keep the ports they had.
algos.SCENARIOS = SCENARIOS


def unnamed_items(place):
    """The RAM items the relink placed that carry no name, largest first.

    An item the partition could not name is a section called after its own
    address, which is exactly the set this is about.
    """
    out = []
    for row in place:
        leaf = row["section"].rsplit(".", 1)[-1]
        if leaf.startswith("bss_") or leaf.startswith("data_"):
            out.append((row["vma"], row["size"]))
    return sorted(out, key=lambda r: -r[1])


def targets(place, floor):
    return [(vma, size) for vma, size in unnamed_items(place) if size >= floor]


def hook_block(items, out_path):
    lines = []
    for vma, _ in items:
        for width in WIDTHS:
            lines.append(WATCH % {"at": vma, "width": width, "out": out_path,
                                  "tcb": PX_CURRENT_TCB, "keep": KEEP,
                                  "nameoff": PC_TASK_NAME,
                                  "namelen": TASK_NAME_LEN})
    return "".join(lines)


def out_path(runtime, name):
    return os.path.abspath(os.path.join(runtime, "out", "algos",
                                        "ram-%s.txt" % name))


def emit(runtime, items):
    algos.prepare(runtime)
    os.makedirs(os.path.join(runtime, "out", "algos"), exist_ok=True)
    written = []
    for scenario in SCENARIOS.values():
        hooks = hook_block(items, out_path(runtime, scenario.name))
        written.append(algos.write_scenario(scenario, runtime, hooks))
    return written


def read_observation(path):
    seen = {}
    if not os.path.exists(path):
        return seen
    for line in open(path):
        parts = line.split()
        if len(parts) != 6:
            continue
        at, pc, lr, tcb, name, count = parts
        at = int(at, 16)
        row = dict(pc=int(pc, 16), lr=int(lr, 16), tcb=int(tcb, 16),
                   count=int(count),
                   task=(bytes.fromhex(name).decode("latin1")
                         if name != "-" else None))
        seen.setdefault(at, []).append(row)
    for at in seen:
        seen[at].sort(key=lambda r: r["count"])
    return seen


def report(runtime, items):
    smap = symmap.load()
    fns = sorted(s.address for s in smap.symbols if s.kind == "function")
    names = {s.address: s.name for s in smap.symbols}

    def owner(pc):
        i = bisect.bisect_right(fns, pc) - 1
        return names.get(fns[i], "0x%x" % fns[i]) if i >= 0 else "?"

    per = collections.OrderedDict(
        (name, read_observation(out_path(runtime, name)))
        for name in SCENARIOS)
    first = {}
    for scenario, rows in per.items():
        for at, seen in rows.items():
            for row in seen:
                held = first.setdefault(at, [])
                if row["count"] < min([r["count"] for r in held] + [1 << 62]):
                    held.insert(0, dict(row, scenario=scenario))
                else:
                    held.append(dict(row, scenario=scenario))
    for at in first:
        first[at].sort(key=lambda r: r["count"])
    written = 0
    for at, size in items:
        seen = first.get(at)
        print("0x%08x %7d  %s" % (at, size, "" if seen else "never written"))
        if not seen:
            continue
        written += 1
        for row in seen[:KEEP]:
            print("    %-11s %-12s %-28s from %-28s"
                  % (row["scenario"], row["task"] or "-", owner(row["pc"]),
                     owner(row["lr"])))
    print("\n%d of %d items written after the scheduler started"
          % (written, len(items)))
    return first


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", default=RUNTIME)
    ap.add_argument("--place", default=os.path.join(SIM, "out",
                                                    "relink-place-ram.json"))
    ap.add_argument("--floor", type=int, default=256,
                    help="the smallest item to watch, in bytes")
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--scenario")
    args = ap.parse_args()
    runtime = os.path.abspath(args.runtime)
    with open(args.place) as fh:
        place = json.load(fh)
    items = targets(place, args.floor)
    written = emit(runtime, items) if (args.emit or args.run) else []
    if args.run:
        for (script, counts, dump), scenario in zip(written,
                                                    SCENARIOS.values()):
            if args.scenario and scenario.name != args.scenario:
                continue
            path = out_path(runtime, scenario.name)
            if os.path.exists(path):
                os.remove(path)
            took = algos.run_scenario(scenario, runtime, counts, dump)
            print("%s: %d items written in %.0f s"
                  % (scenario.name, len(read_observation(path)), took))
    if args.report:
        first = report(runtime, items)
        if args.json:
            with open(args.json, "w") as fh:
                json.dump({"0x%x" % k: v for k, v in first.items()}, fh,
                          indent=1)


if __name__ == "__main__":
    main()
