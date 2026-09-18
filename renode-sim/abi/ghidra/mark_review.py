#!/usr/bin/env python3
"""Put the word classification into the Ghidra project as bookmarks and comments.

    ~/ref-build/ghidra/venv/bin/python abi/ghidra/mark_review.py [--project DIR] [--words out/ghidra/words.json]

Every review word (a word the classifier could not call a pointer or a
constant) gets a "Review" bookmark with its signal and, where the value lands
on something the partition names, the target, plus an end-of-line comment;
every classified pointer gets a plain "Pointer" bookmark so the two are
filterable side by side in Window > Bookmarks. Run it after classify_words.py
against the project analyze.sh leaves behind, and re-run it after analyze.sh,
which rebuilds the project from scratch.
"""
import argparse
import json
import os

import pyghidra

HERE = os.path.dirname(os.path.abspath(__file__))
ABI = os.path.dirname(HERE)
ROOT = os.environ.get("ROOT", os.path.expanduser("~/ref-build"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.path.join(ROOT, "ghidra-project"))
    ap.add_argument("--words", default=os.path.join(ABI, "out", "ghidra", "words.json"))
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
    counts = {"review": 0, "pointer": 0}
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
        finally:
            program.endTransaction(tx, True)
        program.save("word classification", TaskMonitor.DUMMY)
        program.release(consumer)
    finally:
        project.close()
        print("bookmarked %(review)d review words and %(pointer)d pointers" % counts)


if __name__ == "__main__":
    main()
