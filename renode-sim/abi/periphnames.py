#!/usr/bin/env python3
"""Name the bodies whose whole job is one register.

    python3 abi/periphnames.py            # the names, as a report
    python3 abi/periphnames.py --apply    # write them into abi/symbols.yaml

A driver compiled at -Os leaves a tail of two- and three-instruction bodies
that do nothing but poke one register: load the peripheral's base, store a
constant at one offset, return. The call graph has nothing to say about them --
they are called from everywhere and call nothing -- and abi/provenance.py's
rule needs a global to name the body after, which these do not touch. What
does name them is the register itself, which abi/accesses.py now knows.

The rule is narrow on purpose. The body's whole peripheral side has to be one
register, which has to be one the SVD names as a task, an event or an
interrupt mask -- the three kinds of register whose name already is the verb --
and the access has to be a write, because reading a task register is not
triggering it. A body that also touches a global, or a second register, is
doing something the register's name does not say, and is refused.

The name is `<peripheral>_<register>_<verb>` in lower case:
`saadc_tasks_start_trigger`, `rtc1_intenset_set`, `gpiote_events_port_clear`.
"""

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accesses  # noqa: E402
import peripherals  # noqa: E402
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# Which registers name their own verb, and what that verb is. A task register
# has no state, so writing it is triggering it; an event register is cleared by
# writing zero to it; an interrupt mask is set or cleared by which of the two
# aliases the write lands on, which the register name already spells.
VERBS = [(re.compile(r"^TASKS_"), "trigger"),
         (re.compile(r"^EVENTS_"), "clear"),
         (re.compile(r"^INTENCLR$"), "clear"),
         (re.compile(r"^INTEN(SET)?$"), "set")]

UNNAMED = re.compile(r"^(FUN|LAB|block|caseD|thunk|sliver)_")

CLASS = "periph"


def verb_for(register):
    for pattern, verb in VERBS:
        if pattern.match(register.name):
            return verb
    return None


def derive(chip, sides, smap):
    """(address, name, evidence) for every body the rule takes."""
    named = {s.address: s for s in smap.symbols if s.name}
    out = []
    for start, side in sorted(sides.items()):
        if len(side.registers) != 1:
            continue
        access, = side.registers
        if access.kind != accesses.WRITE:
            continue
        located = chip.at(access.address)
        if located is None or located.register is None:
            continue
        verb = verb_for(located.register)
        if verb is None:
            continue
        existing = named.get(start)
        if existing is not None and not UNNAMED.match(existing.name):
            continue
        spelled = (located.register.name.replace("[", "").replace("]", "")
                   .replace(".", "_").lower())
        out.append((start, "%s_%s_%s" % (located.peripheral.name.lower(),
                                         spelled, verb),
                    "the body's whole side is one write to %s; %s"
                    % (access.object, access.evidence)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    smap = symmap.load()
    chip = peripherals.load()
    sides, _ = accesses.load_index()
    found = derive(chip, sides, smap)
    counts = collections.Counter(name.rsplit("_", 1)[1] for _, name, _ in found)
    print("%d bodies named from the one register they write (%s)"
          % (len(found),
             ", ".join("%d %s" % (n, k) for k, n in counts.most_common())
             or "none"))
    for start, name, why in found:
        print("  0x%08x %-34s %s" % (start, name, why))
    if not args.apply:
        return 0
    records = [{"address": start, "name": name, "kind": "function",
                "class": CLASS, "evidence": why} for start, name, why in found]
    print("abi/symbols.yaml: %d entries rewritten"
          % smap.rewrite(records, {CLASS}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
