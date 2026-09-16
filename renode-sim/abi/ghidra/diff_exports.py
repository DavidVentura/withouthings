#!/usr/bin/env python3
"""Compare two partition exports (abi/ghidra/analyze.sh runs).

    python3 abi/ghidra/diff_exports.py OLD_DIR NEW_DIR

Prints the per-section row counts side by side and the function starts that
only one side has, which is what a change to the closing or seeding scripts
must be judged by: a faster run that loses functions is not a speedup.
"""
import json
import os
import sys


def load(directory, name):
    with open(os.path.join(directory, name)) as fh:
        return json.load(fh)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    old_dir, new_dir = sys.argv[1:3]
    for name in ("items.json", "references.json"):
        old, new = load(old_dir, name), load(new_dir, name)
        print(name)
        for section in old:
            if section.startswith("_"):
                continue
            # An empty section is [] in the JSON export and null in a YAML one.
            o, n = old[section] or [], new.get(section) or []
            if isinstance(o, dict):
                for k in o:
                    if o[k] != n.get(k):
                        print("  %s.%s: %s -> %s" % (section, k, o[k], n.get(k)))
                continue
            if len(o) != len(n):
                print("  %s: %d -> %d rows" % (section, len(o), len(n)))
    old_fn = {f["start"]: f["name"] for f in load(old_dir, "items.json")["functions"]}
    new_fn = {f["start"]: f["name"] for f in load(new_dir, "items.json")["functions"]}
    for label, only in (("only old", sorted(set(old_fn) - set(new_fn))),
                        ("only new", sorted(set(new_fn) - set(old_fn)))):
        print("%s: %d functions" % (label, len(only)))
        for start in only[:20]:
            print("  0x%x %s" % (start, (old_fn if label == "only old" else new_fn)[start]))


if __name__ == "__main__":
    main()
