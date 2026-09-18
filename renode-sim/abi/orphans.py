#!/usr/bin/env python3
"""The unnamed functions the section graph gives no caller."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import survey, symbols as symmap

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)

def orphans():
    items = json.load(open(os.path.join(HERE, "out", "ghidra", "items.json")))
    smap = symmap.load()
    fns = survey.load_partition(items, smap)
    graph = survey.load_graph(os.path.join(SIM, "out", "survey", "appl-blob.o"),
                              os.path.join(SIM, "out", "survey", "place.ld"), fns)
    return graph, [f for a, f in sorted(graph.fns.items())
                   if not f.named and not graph.pred.get(a)]

if __name__ == "__main__":
    g, o = orphans()
    print("%d unnamed functions with no caller, %d bytes" % (len(o), sum(f.size for f in o)))
    for f in o:
        print("0x%05x %5d %-8s %s" % (f.start, f.size, f.module or "-", f.label))
