#!/usr/bin/env python3
"""Put the word classification into the Ghidra project as bookmarks and comments.

    ~/ref-build/ghidra/venv/bin/python abi/ghidra/mark_review.py [--project DIR] [--words out/ghidra/words.json]

Every review word (a word the classifier could not call a pointer or a
constant) gets a "Review" bookmark with its signal and, where the value lands
on something the partition names, the target, plus an end-of-line comment;
every classified pointer gets a plain "Pointer" bookmark; and every function
the map has no meaningful name for (a FUN_/block_/caseD_ name) gets an
"Unnamed" bookmark whose category is Unnamed1..Unnamed5 by its distance up
the call graph to the nearest named function (UnnamedDeep beyond that) and
whose comment carries its size, module and named callers, so Window >
Bookmarks filtered on Unnamed and sorted by category is the naming worklist,
shallow end first. Names given in the GUI come back with
abi/ghidra/import_names.py. Run it after classify_words.py
against the project analyze.sh leaves behind, and re-run it after analyze.sh,
which rebuilds the project from scratch.
"""
import argparse
import bisect
import json
import os
import re

import pyghidra

HERE = os.path.dirname(os.path.abspath(__file__))
ABI = os.path.dirname(HERE)
ROOT = os.environ.get("ROOT", os.path.expanduser("~/ref-build"))


UNNAMED = re.compile(r"^(FUN|LAB|block|caseD|thunk|sliver)_")
MAX_DEPTH = 5


def unnamed_functions(items_path, modules_path, references_path):
    """Functions the map gives no meaningful name, with module and named callers."""
    items = json.load(open(items_path))
    modules = json.load(open(modules_path))
    refs = json.load(open(references_path))
    by_start = {f["start"]: f for f in items["functions"]}
    module_of = {}
    m = modules.get("functions") or modules.get("modules") or modules
    if isinstance(m, dict):
        for k, v in m.items():
            if isinstance(v, dict) and "module" in v:
                module_of[int(k, 0)] = v["module"]
            elif isinstance(v, list):
                for a in v:
                    module_of[int(a, 0) if isinstance(a, str) else a] = k
    starts = sorted(by_start)
    def owner(a):
        i = bisect.bisect_right(starts, a) - 1
        return by_start[starts[i]] if i >= 0 else None
    callers = {}
    for c in refs.get("calls", []):
        if c.get("kind") not in ("call", "jump"):
            continue
        src = owner(c["from"])
        if src is not None and c["to"] in by_start:
            callers.setdefault(c["to"], set()).add(src["start"])
    # Distance up the call graph to the nearest function with a real name:
    # 1 means a named function calls it directly. Breadth-first from the
    # named callers so the worklist can start at the shallow end; past
    # MAX_DEPTH the number stops meaning much and the bookmark says so.
    named = {a for a, f in by_start.items() if not UNNAMED.match(f["name"])}
    depth = {}
    frontier = set()
    for callee, srcs in callers.items():
        if callee not in named and srcs & named:
            depth[callee] = 1
            frontier.add(callee)
    level = 1
    while frontier and level < MAX_DEPTH:
        level += 1
        nxt = set()
        for callee, srcs in callers.items():
            if callee in named or callee in depth:
                continue
            if srcs & frontier:
                depth[callee] = level
                nxt.add(callee)
        frontier = nxt
    out = []
    for f in items["functions"]:
        if not UNNAMED.match(f["name"]):
            continue
        size = f["bytes"] if "bytes" in f else f["end"] - f["start"]
        named_callers = sorted(by_start[a]["name"] for a in callers.get(f["start"], ()) if a in named)[:6]
        out.append({"start": f["start"], "bytes": size,
                    "module": module_of.get(f["start"], "(none)"),
                    "depth": depth.get(f["start"]),
                    "callers": named_callers})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.path.join(ROOT, "ghidra-project"))
    ap.add_argument("--words", default=os.path.join(ABI, "out", "ghidra", "words.json"))
    ap.add_argument("--items", default=os.path.join(ABI, "out", "ghidra", "items.json"))
    ap.add_argument("--modules", default=os.path.join(ABI, "out", "ghidra", "modules.json"))
    ap.add_argument("--references", default=os.path.join(ABI, "out", "ghidra", "references.json"))
    ap.add_argument("--install", default=None,
                    help="Ghidra install dir; defaults to the release fetch.sh put under ROOT")
    args = ap.parse_args()
    words = json.load(open(args.words))
    rows = words["words"] if "words" in words else words

    install = args.install
    if install is None:
        import glob
        found = sorted(glob.glob(os.path.join(ROOT, "ghidra", "ghidra_*")))
        if not found:
            raise SystemExit("no Ghidra under %s; run abi/ghidra/fetch.sh" % os.path.join(ROOT, "ghidra"))
        install = found[-1]
    pyghidra.start(install_dir=install)
    from ghidra.program.model.listing import CodeUnit
    from java.awt import Color
    from resources import ResourceManager
    counts = {"review": 0, "pointer": 0, "unnamed": 0}
    unnamed = unnamed_functions(args.items, args.modules, args.references)
    project = pyghidra.open_project(args.project, "hwa10", create=False)
    try:
        # pyghidra hands back the framework project, whose files are domain
        # files under the root folder; the analysis imported the app as appl.bin.
        from ghidra.util.task import TaskMonitor
        domain_file = project.getProjectData().getRootFolder().getFile("appl.bin")
        if domain_file is None:
            raise SystemExit("no appl.bin in the project %s" % args.project)
        from java.lang import Object as JObject
        consumer = JObject()
        program = domain_file.getDomainObject(consumer, False, False, TaskMonitor.DUMMY)
        bookmarks = program.getBookmarkManager()
        listing = program.getListing()
        space = program.getAddressFactory().getDefaultAddressSpace()
        tx = program.startTransaction("word classification")
        try:
            # A word decided since the last run must lose its Review bookmark,
            # so both types are rebuilt from scratch rather than added to. The
            # project analyze.sh leaves behind has never held either type, and
            # removeBookmarks throws on a type the program has not defined, so
            # both are defined before they are cleared.
            for kind, image, color in (("Review", "images/warning.png", Color.RED),
                                       ("Pointer", "images/flag.png", Color.BLUE)):
                bookmarks.defineType(kind, ResourceManager.loadImage(image), color, 0)
                bookmarks.removeBookmarks(kind)
            for r in rows:
                if r["class"] not in counts:
                    continue
                addr = space.getAddress(r["addr"])
                if r["class"] == "review":
                    text = "%s value 0x%x" % (r["signal"], r["value"])
                    if r.get("target"):
                        text += " -> 0x%x+%d" % (r["target"], r.get("addend", 0))
                    bookmarks.setBookmark(addr, "Review", r["kind"], text)
                    listing.setComment(addr, CodeUnit.EOL_COMMENT, "review: " + text)
                else:
                    bookmarks.setBookmark(addr, "Pointer", r["kind"],
                                          "%s -> 0x%x" % (r["signal"], r["value"]))
                counts[r["class"]] += 1
            for f in unnamed:
                addr = space.getAddress(f["start"])
                category = ("Unnamed%d" % f["depth"]) if f["depth"] else "UnnamedDeep"
                text = "%d B, module %s, named callers: %s" % (
                    f["bytes"], f["module"], ", ".join(f["callers"]) or "none")
                bookmarks.setBookmark(addr, "Unnamed", category, text)
                counts["unnamed"] += 1
        finally:
            program.endTransaction(tx, True)
        program.save("word classification", TaskMonitor.DUMMY)
        program.release(consumer)
    finally:
        project.close()
        print("bookmarked %(review)d review words, %(pointer)d pointers and %(unnamed)d unnamed functions" % counts)


if __name__ == "__main__":
    main()
