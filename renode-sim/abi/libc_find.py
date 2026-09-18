#!/usr/bin/env python3
"""Find the library bodies the image carries, from the archive side.

    python3 abi/libc_find.py [--build newlib-nano-ll] [--emit out/libc-found.yaml]

abi/libc_check.py asks, of every body abi/autonames.py already named, whether a
source build reproduces it. That direction can only ever confirm names we have.
The relink needs the other direction: the blob keeps a copy of every newlib body
whose callers in the image are themselves unnamed, so the archive links its own
copy beside it and `REPLACE=newlib` links 24 KB to drop 10 KB. Those bodies are
invisible to the check because nothing named them.

So this walks every function the built archives define and searches the whole
image for its bytes. A body is compared with the fields a relocation owns masked
out, the same rule libc_check uses, because those bytes are the linker's and
cannot agree before a link. The search is anchored on the body's longest run of
bytes no relocation owns, which is a literal substring of the image if the body
is there at all, so the scan is a bytes.find rather than a sweep; a hit is then
confirmed by a masked comparison over the whole body. Starts are restricted to
what the partition calls a function start plus even addresses inside its gap
runs, since a body that begins mid-instruction is an accident of the anchor.

A hit is then held to the call graph and the partition before it may name
anything, because the bytes identify a shape and not a function: a call to a
symbol of the same object is one the archive would bring with the body, so its
image target has to be that symbol, and a body under 32 unrelocated bytes with
no instruction of its own beyond the prologue, the calls and the return is a
shape every wrapper in every library shares.

Bodies with no byte-exact hit get the second pass, which is match.py's: the
disassembly normalised so that branch displacements and pool loads carry no
address, scored against the image. That finds the bodies Withings built from
different source or different flags, and reports where they first diverge.
"""

import argparse
import collections
import json
import os
import re
import sys

import yaml

import libc_check
import match

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
APP_BASE = libc_check.APP_BASE

# Shorter than this and a body is a handful of instructions every compiler emits
# the same way; the anchor would hit everywhere and prove nothing.
MIN_BODY = 16
# The anchor only has to narrow the scan; the full masked comparison decides, so
# four bytes is enough and a body whose prologue is all pool loads still gets in.
MIN_ANCHOR = 4
# Bytes outside the relocated fields are the only evidence a hit carries; fewer
# than this and the body is a shape every short function shares.
MIN_SOLID = 12
# An anchor this common is a prologue, not a body; the masked comparison would
# reject every one of the hits and only the scan would have cost anything.
MAX_ANCHOR_HITS = 400
# The partition names a split function's tail after its head plus an index, and
# the helper class carries that spelling; such a name is derived, not a claim.
DERIVED = re.compile(r"__\d+$")


def positions(items):
    """Every image address a body is allowed to start at.

    The partition's function starts, the interior labels of its split functions,
    and the even addresses inside its gap runs -- a gap is exactly where an
    unnamed library body would be sitting.
    """
    starts = set()
    for f in items["functions"]:
        starts.add(f["start"])
    for r in items["function_ranges"]:
        starts.add(r["start"])
    for s in items.get("split_functions", []):
        starts.add(s["start"] if "start" in s else s.get("address", 0))
    gaps = set()
    for g in items["gaps"]:
        for a in range(g["start"] + (g["start"] & 1), g["end"], 2):
            gaps.add(a)
    starts.discard(0)
    return starts, gaps


_BODIES = {}


def body(label, ar, name):
    """libc_check's extractor, memoised: it runs two objdumps per call."""
    key = (label, name)
    if key not in _BODIES:
        _BODIES[key] = ar.body(name)
    return _BODIES[key]


def anchor_of(built, owned):
    """The longest run of bytes in the body that no relocation owns."""
    best, run = (0, 0), 0
    for i in range(len(built) + 1):
        if i < len(built) and i not in owned:
            run += 1
            continue
        if run > best[1]:
            best = (i - run, run)
        run = 0
    return best


def compare(built, owned, image, off):
    """exact, masked, or None: whether the image carries this body at `off`."""
    if off < 0 or off + len(built) > len(image):
        return None
    there = image[off:off + len(built)]
    diffs = [i for i in range(len(built)) if built[i] != there[i]]
    if not diffs:
        return "exact"
    if any(i not in owned for i in diffs):
        return None
    return "masked"


def load_archives(args):
    tools = os.path.join(args.root, "tc",
                         "arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi",
                         "bin", "arm-none-eabi-")
    newlib = os.path.join(args.root, "build", args.build, "arm-none-eabi", "newlib")
    libgcc = os.path.join(args.root, "tc",
                          "arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi", "lib",
                          "gcc", "arm-none-eabi", "13.2.1", "thumb", "v7e-m+fp",
                          "hard", "libgcc.a")
    want = [("libc.a", os.path.join(newlib, "libc.a")),
            ("libm.a", os.path.join(newlib, "libm.a")),
            ("libgcc.a", libgcc)]
    out = []
    for label, path in want:
        if not os.path.exists(path):
            sys.exit("%s does not exist; run abi/refbuild.sh" % path)
        ar = libc_check.Archive(tools, os.path.join(args.scratch, args.build, label))
        ar.add(path)
        out.append((label, path, ar))
    return out


def known_names():
    """What every address is called today, and by whom."""
    auto = yaml.safe_load(open(os.path.join(HERE, "autonames.yaml")))
    hand = yaml.safe_load(open(os.path.join(HERE, "hwa10.yaml")))
    matched = yaml.safe_load(open(os.path.join(HERE, "matches.yaml")))
    named = {}
    for f in auto["functions"]:
        named[f["address"]] = ("autonames:" + f["class"], f["name"])
    for f in matched.get("functions", []) or []:
        addr = f.get("address")
        if addr is not None:
            named.setdefault(addr, ("matches", f.get("name") or f.get("symbol")))
    for f in hand.get("functions", []) or []:
        named[f["address"]] = ("hand", f["name"])
    return named


def partition_names(items):
    """The name the analysis carries at each start, and whether it is derived."""
    out = {}
    for f in items["functions"]:
        out[f["start"]] = (f.get("name") or "", f.get("named") or "")
    return out


def raw_pass(archives, image, starts, gaps):
    """Every archive body, searched for in the image by its unrelocated bytes."""
    hits, skipped = [], []
    for label, _, ar in archives:
        for name in sorted(ar.bodies):
            obj, section, off, size, bind = ar.bodies[name]
            if size < MIN_BODY:
                skipped.append((label, name, "under %d bytes" % MIN_BODY))
                continue
            built, owned = body(label, ar, name)
            if len(built) != size:
                built = built[:size]
            aoff, alen = anchor_of(built, owned)
            if alen < MIN_ANCHOR:
                skipped.append((label, name, "no unrelocated run of %d bytes" % MIN_ANCHOR))
                continue
            needle = built[aoff:aoff + alen]
            found, at = [], image.find(needle)
            while at >= 0 and len(found) < MAX_ANCHOR_HITS:
                found.append(at - aoff)
                at = image.find(needle, at + 1)
            for off_img in found:
                addr = off_img + APP_BASE
                where = "start" if addr in starts else ("gap" if addr in gaps else None)
                if where is None:
                    continue
                verdict = compare(built, owned, image, off_img)
                if verdict is None:
                    continue
                hits.append({"archive": label, "symbol": name, "address": addr,
                             "verdict": verdict, "size": len(built), "bind": bind,
                             "where": where, "anchor": alen,
                             "solid": len(built) - len(owned),
                             "site": (obj, section, off)})
    return hits, skipped


def token_pass(archives, wanted, streams, idx, threshold, min_insns):
    """match.py's normalised scoring, for the bodies no raw hit explains."""
    out = []
    for label, path, _ in archives:
        refs = match.parse_reference(path)
        for name, fn in refs.items():
            if (label, name) not in wanted:
                continue
            if len(fn["tokens"]) < min_insns:
                continue
            best = None
            for cand in match.candidates(fn, streams, idx):
                r = match.score(fn, streams, cand)
                if r and (best is None or r["score"] > best["score"]):
                    best = r
            if best and best["score"] >= threshold:
                out.append({"archive": label, "symbol": name,
                            "address": best["address"], "score": best["score"],
                            "insns": best["insns"], "tokens": len(fn["tokens"])})
    return out


def divergence(archives, name, label, image, addr, limit=4):
    """The first bytes of the body that differ outside the relocated fields."""
    ar = dict((l, a) for l, _, a in archives)[label]
    built, owned = body(label, ar, name)
    off = addr - APP_BASE
    there = image[off:off + len(built)]
    bad = [i for i in range(min(len(built), len(there)))
           if built[i] != there[i] and i not in owned]
    return bad[:limit], len(bad), len(built)


ARCHIVE_CLASS = {"libc.a": "libc", "libgcc.a": "libc", "libm.a": "libm"}
# GCC gives a specialised clone a name with a dot in it, which is not an
# identifier the rest of the repo can carry; the base name is what it is named.
CLONE = re.compile(r"\.(isra|constprop|part|cold)\.\d+$")


_RESOLVED = {}


def resolve(args):
    """Every image address that carries an archive body, and the ties refused.

    The ties are the point of the return: a sixteen-byte reentrant wrapper is a
    body six symbols share, so a hit there identifies the shape and not the
    function, and only the call graph can finish it.
    """
    key = (args.build, args.export)
    if key in _RESOLVED:
        return _RESOLVED[key]
    with open(os.path.join(SIM, "appl.bin"), "rb") as fh:
        image = fh.read()
    items = json.load(open(os.path.join(args.export, "items.json")))
    starts, gaps = positions(items)
    archives = load_archives(args)
    hits, skipped = raw_pass(archives, image, starts, gaps)

    by_addr = collections.defaultdict(list)
    for h in hits:
        by_addr[h["address"]].append(h)

    resolved, ambiguous = {}, []
    for addr, group in sorted(by_addr.items()):
        sites = set(h["site"] for h in group)
        distinct = sorted(set(h["symbol"] for h in group))
        if len(sites) > 1 and len(set(h["size"] for h in group)) == 1 and len(distinct) > 1:
            ambiguous.append((addr, distinct))
            continue
        best = sorted(group, key=lambda h: (-h["size"], h["bind"] != "g",
                                            h["symbol"].startswith("__"),
                                            len(h["symbol"]), h["symbol"]))[0]
        if len(distinct) > 1:
            best = dict(best, aliases=[d for d in distinct if d != best["symbol"]])
        resolved[addr] = best

    twice = collections.Counter((h["archive"], h["symbol"]) for h in resolved.values())
    solid = {a: h for a, h in resolved.items()
             if h["solid"] >= MIN_SOLID and twice[(h["archive"], h["symbol"])] == 1}

    # The bytes say the body is here; the call graph and the partition say
    # whether it is this function. A hit that cannot survive both may not name
    # anything: the image's 0x9bd0c is a Withings lock/operate/unlock wrapper
    # whose ten unrelocated bytes are newlib's _tzset_r exactly, and the
    # relocated branches are where the two part company.
    claims = libc_check.name_index()
    for addr, h in solid.items():
        claims[addr].add(libc_check.base_name(h["symbol"]))
    rules = libc_check.Rules([a for _, _, a in archives],
                            libc_check.Image(args.export), claims, image)
    refused = {}
    for addr, h in sorted(solid.items()):
        why = refuse(rules, addr, h)
        if why:
            refused[addr] = (h, why)
    for addr in refused:
        del solid[addr]
    _RESOLVED[key] = (image, items, archives, solid, resolved, ambiguous,
                      skipped, refused, rules)
    return _RESOLVED[key]


def refuse(rules, addr, h):
    """Why this hit may not name the address, or None.

    A name is a claim about identity and a replacement is a claim about link
    equivalence, so this refuses less than abi/libc_check.py's verdict does. An
    address the instruction before falls through into is the second entry point
    of a section libgcc exports twice and the name there is right; a callee the
    repo already calls something else moves one call and leaves the body what it
    is. Only the partition, a callee the archive would bring with the body, and
    a shape too short to be anything settle what the address is called.
    """
    kind, reason = rules.entry(addr)
    if kind == "bad_entry":
        return reason
    if not rules.bytes_are(h["symbol"], addr):
        return None
    bad, _, _ = rules.audit(h["symbol"], addr)
    small = h["solid"] < libc_check.SMALL_BODY
    if bad:
        return "; ".join("+0x%x wants %s, %s" % b for b in bad)
    if small and not rules.distinguishing(h["symbol"]):
        return ("%d unrelocated bytes and no instruction beyond the prologue,"
                " the calls and the return" % h["solid"])
    return None


def entries(args, cls):
    """The autonames entries for one class, from the archives it owns."""
    _, _, _, solid, _, _, _, _, _ = resolve(args)
    out = []
    for addr, h in sorted(solid.items()):
        if ARCHIVE_CLASS[h["archive"]] != cls:
            continue
        name = CLONE.sub("", h["symbol"])
        ev = ("%s %s, %d bytes of the built body reproduced at 0x%x%s"
              % (args.build, h["archive"], h["size"], addr,
                 "" if h["verdict"] == "exact" else
                 " with the %d bytes a relocation owns masked"
                 % (h["size"] - h["solid"])))
        if h.get("aliases"):
            ev += "; the same body is also " + ", ".join(h["aliases"])
        out.append({"address": addr, "name": name, "class": cls, "evidence": ev})
    return out


def screen(args, found):
    """The autonames entries the call graph, the partition and the shape allow.

    abi/autonames.py names a library body two ways, and the matcher's way scores
    a normalised disassembly in which every branch has had its displacement
    taken off. That is the evidence a lock/operate/unlock wrapper cannot fail:
    0x9bd0c scored 1.00 over seven instructions against _tzset_r and is a
    Withings SPI-flash wrapper, and the branches the score threw away are the
    whole difference. So the same rules the byte search is held to are applied
    to every libc and libm entry before it may name anything.
    """
    rules = resolve(args)[8]
    kept, refused = [], []
    for e in found:
        ar = rules.archive_of(e["name"])
        if ar is None:
            kept.append(e)
            continue
        built, owned = ar.body(e["name"])
        why = refuse(rules, e["address"],
                     {"symbol": e["name"], "solid": len(built) - len(owned)})
        (refused if why else kept).append((e, why) if why else e)
    return kept, refused


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="newlib-nano-ll")
    ap.add_argument("--root", default=os.path.expanduser("~/ref-build"))
    ap.add_argument("--scratch", default="/tmp/libc_find")
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--threshold", type=float, default=0.80)
    ap.add_argument("--min-insns", type=int, default=8)
    ap.add_argument("--tokens", action="store_true",
                    help="also run the normalised-disassembly pass over the"
                         " bodies no byte-exact hit explains")
    ap.add_argument("--emit", help="write every hit as YAML")
    ap.add_argument("--list", type=int, default=40)
    args = ap.parse_args()

    (image, items, archives, solid, resolved, ambiguous, skipped, refused,
     _) = resolve(args)
    named, part = known_names(), partition_names(items)
    named_at = {}
    for addr, (who, cur) in named.items():
        named_at.setdefault(cur, addr)

    new, agree, conflict, weak = [], [], [], []
    for addr, h in sorted(resolved.items()):
        if addr not in solid:
            weak.append(h)
            continue
        who, cur = named.get(addr, (None, None))
        pname, _ = part.get(addr, ("", ""))
        names = set([h["symbol"]] + list(h.get("aliases", [])))
        names |= set(CLONE.sub("", n) for n in names)
        if who is None:
            new.append((h, pname))
        elif cur in names:
            agree.append((h, who))
        elif DERIVED.search(cur or "") or (cur or "").startswith("FUN_"):
            new.append((h, cur))
        else:
            conflict.append((h, who, cur))

    # A name whose body starts somewhere else is the matcher's alignment caught:
    # the seed would otherwise cut the body in two at the address it carries.
    moved = [(h, named_at[h["symbol"]]) for h in solid.values()
             if named_at.get(h["symbol"], h["address"]) != h["address"]]

    print("%s against appl.bin" % args.build)
    for label, _, ar in archives:
        print("  %-10s %5d bodies defined" % (label, len(ar.bodies)))
    print("  %d bodies too small or too relocated to search" % len(skipped))
    print("  %d image addresses carry an archive body byte for byte"
          % len(resolved))
    print("     %d agree with the name already there" % len(agree))
    print("     %d conflict with a name already there" % len(conflict))
    print("     %d are unnamed or derived, so they are new names" % len(new))
    print("     %d addresses are a tie between equal-sized bodies" % len(ambiguous))
    print("     %d carry fewer than %d unrelocated bytes, or land twice"
          % (len(weak), MIN_SOLID))
    print("     %d carry the bytes and are refused by the call graph, the"
          " partition or the shape" % len(refused))
    for addr, (h, why) in sorted(refused.items()):
        print("    refused  0x%05x  %-24s %s" % (addr, h["symbol"], why))
    perarc = collections.Counter(h["archive"] for h, _ in new)
    print("  new names per archive: %s"
          % ", ".join("%s %d" % kv for kv in sorted(perarc.items())))
    wherec = collections.Counter(h["where"] for h, _ in new)
    print("  new names by position: %s"
          % ", ".join("%s %d" % kv for kv in sorted(wherec.items())))
    verc = collections.Counter(h["verdict"] for h, _ in new)
    print("  new names by verdict: %s"
          % ", ".join("%s %d" % kv for kv in sorted(verc.items())))

    print("  %d names sit at an address the body does not start at" % len(moved))
    for h, was in moved:
        print("    moved   %-24s named at 0x%05x, body starts 0x%05x (%+d)"
              % (h["symbol"], was, h["address"], h["address"] - was))
    for h, who, cur in conflict[:args.list]:
        print("    conflict 0x%05x  %s says %s, %s has %s"
              % (h["address"], who, cur, h["archive"], h["symbol"]))

    tokens = []
    if args.tokens:
        hit_syms = set((h["archive"], h["symbol"]) for h in resolved.values())
        wanted = set()
        for label, _, ar in archives:
            for name in ar.bodies:
                if (label, name) not in hit_syms:
                    wanted.add((label, name))
        streams = match.parse_image(os.path.join(SIM, "appl.bin"), APP_BASE)
        idx = match.build_kgram_index(streams)
        tokens = token_pass(archives, wanted, streams, idx,
                            args.threshold, args.min_insns)
        print("  %d further bodies score >= %.2f on the normalised disassembly"
              " without reproducing the bytes" % (len(tokens), args.threshold))
        for t in sorted(tokens, key=lambda t: -t["score"])[:args.list]:
            bad, nbad, size = divergence(archives, t["symbol"], t["archive"],
                                         image, t["address"])
            print("    differs 0x%05x  %-24s %-9s score %.2f, %d of %d bytes,"
                  " first at +%s"
                  % (t["address"], t["symbol"], t["archive"], t["score"],
                     nbad, size, ",".join("0x%x" % b for b in bad)))

    if args.emit:
        os.makedirs(os.path.dirname(os.path.abspath(args.emit)), exist_ok=True)
        with open(args.emit, "w") as fh:
            fh.write("# Generated by abi/libc_find.py, do not edit: every image\n"
                     "# address that carries a body one of the built archives\n"
                     "# defines, found from the archive side.\n")
            fh.write("build: %s\n" % args.build)
            fh.write("found:\n")
            for addr, h in sorted(resolved.items()):
                if addr not in solid:
                    continue
                who, cur = named.get(addr, (None, None))
                fh.write("  - address: 0x%x\n    symbol: %s\n    archive: %s\n"
                         "    verdict: %s\n    size: %d\n    solid: %d\n    where: %s\n"
                         "    bind: %s\n    named: %s\n"
                         % (addr, h["symbol"], h["archive"], h["verdict"],
                            h["size"], h["solid"], h["where"], h["bind"],
                            ("%s %s" % (who, cur)) if who else "null"))
            fh.write("ambiguous:\n")
            for addr, distinct in ambiguous:
                fh.write("  - address: 0x%x\n    symbols: [%s]\n"
                         % (addr, ", ".join(distinct)))
            if tokens:
                fh.write("differs:\n")
                for t in sorted(tokens, key=lambda t: -t["score"]):
                    bad, nbad, size = divergence(archives, t["symbol"],
                                                 t["archive"], image, t["address"])
                    fh.write("  - address: 0x%x\n    symbol: %s\n    archive: %s\n"
                             "    score: %.2f\n    bad_bytes: %d\n    size: %d\n"
                             "    first_divergence: [%s]\n"
                             % (t["address"], t["symbol"], t["archive"],
                                t["score"], nbad, size,
                                ", ".join("0x%x" % b for b in bad)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
