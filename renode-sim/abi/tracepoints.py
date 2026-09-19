#!/usr/bin/env python3
"""The addresses a scenario trace hooks, with the name the map already gives them.

    python3 abi/tracepoints.py > out/tracepoints.txt

Three sets, and the reason for each is different. The 398 unnamed functions the
section graph gives no caller are the question: the algorithm suite is entered
through function-pointer tables installed in RAM, so nothing static reaches
them and only a run says which feature they belong to. The algorithm steps and
the manager around them are the ruler: a callee's count divided by its
algorithm's step count is the rate that fixes its role. The WUI slot bodies say
which screens the scenario actually put up, which is what separates a function
that ran because of the feature from one that ran because the watch face did.

The addresses a trace has already named stay in, because the rate is the
evidence the name rests on: naming one takes it out of the uncalled set, and a
hook set built from that set alone would silently stop measuring the very thing
the next run has to disagree with.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import orphans
import symbols as symmap

ALGO = re.compile(r"algo|_step$|hr_|ppg_|spo2_|sleep_wake|worn|motion_")


def main():
    graph, uncalled = orphans.orphans()
    smap = symmap.load()
    rows = [(f.start, "uncalled", f.label) for f in uncalled]
    for s in smap.of_kind("function"):
        if s.klass == "wuiview":
            rows.append((s.address, "wui", s.name))
        elif s.klass == "trace":
            rows.append((s.address, "trace", s.name))
        elif ALGO.search(s.name) and s.klass in ("hand", "sensor"):
            rows.append((s.address, "algo", s.name))
    seen = set()
    for address, why, name in sorted(rows):
        if address in seen:
            continue
        seen.add(address)
        print("%08x %-8s %s" % (address, why, name))


if __name__ == "__main__":
    main()
