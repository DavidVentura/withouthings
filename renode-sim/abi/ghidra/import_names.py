#!/usr/bin/env python3
"""Bring function names given in the Ghidra GUI back into the map.

    ~/ref-build/ghidra/venv/bin/python abi/ghidra/import_names.py [--apply]

Walks every function in the project; where its name is not one of Ghidra's
own (FUN_/LAB_/...), not the name the map already carries at that address,
and the address holds no tool-established record, it is a hand naming made in
the GUI and becomes a `hand` entry in abi/symbols.yaml with that evidence.
A rename over a tool-established record is printed as a refusal: the map's
precedence says a verified name is not overridden by hand, only superseded
with a reason, which is a yaml edit. Without --apply it only prints. The
project is rebuilt by analyze.sh from the map, so a name is only durable
once it is here.
"""
import argparse
import datetime
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ABI = os.path.dirname(HERE)
ROOT = os.environ.get("ROOT", os.path.expanduser("~/ref-build"))
sys.path.insert(0, ABI)
import symbols  # noqa: E402

GHIDRA_OWN = re.compile(r"^(FUN|LAB|DAT|block|caseD|thunk|sliver|switchD|SUB)_")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.path.join(ROOT, "ghidra-project"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    import pyghidra
    found = sorted(glob.glob(os.path.join(ROOT, "ghidra", "ghidra_*")))
    if not found:
        raise SystemExit("no Ghidra under %s" % os.path.join(ROOT, "ghidra"))
    pyghidra.start(install_dir=found[-1])
    from ghidra.util.task import TaskMonitor
    from java.lang import Object as JObject
    smap = symbols.load()
    by_addr = {sym.address & ~1: sym for sym in smap.symbols}
    project = pyghidra.open_project(args.project, "hwa10", create=False)
    consumer = JObject()
    new, refused = [], []
    try:
        df = project.getProjectData().getRootFolder().getFile("appl.bin")
        program = df.getDomainObject(consumer, False, False, TaskMonitor.DUMMY)
        try:
            for fn in program.getFunctionManager().getFunctions(True):
                name = str(fn.getName())
                addr = int(fn.getEntryPoint().getOffset())
                if GHIDRA_OWN.match(name):
                    continue
                cur = by_addr.get(addr)
                if cur is not None and (cur.name == name or name in cur.aliases):
                    continue
                if cur is not None and cur.klass != "hand":
                    refused.append((addr, name, cur.name, cur.klass))
                    continue
                new.append((addr, name, cur.name if cur else None))
        finally:
            program.release(consumer)
    finally:
        project.close()
    for addr, name, old in new:
        print("hand   0x%x %s%s" % (addr, name, " (was %s)" % old if old else ""))
    for addr, name, old, cls in refused:
        print("REFUSE 0x%x %s: the map holds %s (%s); supersede it in symbols.yaml with a reason"
              % (addr, name, old, cls))
    if not args.apply or not new:
        print("%d names to import, %d refused%s" % (len(new), len(refused),
              "" if args.apply else " (dry run, pass --apply)"))
        return
    today = datetime.date.today().isoformat()
    renamed = {addr for addr, _, old in new if old}
    rows = [sym.row for sym in smap.symbols if (sym.address & ~1) not in renamed]
    for addr, name, old in new:
        row = {"address": addr | 1, "name": name, "kind": "function", "class": "hand",
               "evidence": "named in the Ghidra GUI on %s" % today}
        if old:
            row["note"] = "was %s" % old
        rows.append(row)
    symbols.dump(rows)
    symbols.load()
    print("imported %d names into abi/symbols.yaml" % len(new))


if __name__ == "__main__":
    main()
