#!/usr/bin/env python3
"""Map HWA10 image addresses to open-source symbols by instruction matching.

    python3 abi/match.py                     # the recorded variant set, rewrites matches.yaml
                                             # and the `match` class of abi/symbols.yaml
    python3 abi/match.py --check             # only score against the known symbols
    python3 abi/match.py --variants Os,O2    # restrict the reference builds used
    python3 abi/match.py --all-variants      # every build under ~/ref-build/build

Reference ELFs come from abi/refbuild.sh (~/ref-build/build/<variant>/ref.elf).
Both sides are disassembled with llvm-objdump so the mnemonic spelling agrees;
instructions are normalised (branch targets, pc-relative pool loads and absolute
addresses masked, register/immediate structure kept) and the image is searched
for each reference function's token sequence. Matches are then confirmed through
the call graph: a reference `bl` carries a relocation naming its callee, so the
branch target at the same index in the image must be the address matched to that
callee.

A score is only a score against a fixed set of bodies, so the variant set is
part of the record: matches.yaml carries the list it was measured against and a
bare re-run uses that list, not whatever ~/ref-build holds today. What the file
publishes is not the score but the verdict -- the recorded variant's body
compared against the image byte for byte with relocated fields masked, plus
agreement of every call the reference resolves inside its own link. The token
matcher only proposes candidates; a verdict is a fact about one body and one
build, so a reference added tomorrow can add candidates and cannot unsettle one.

matches.yaml is the measurement and abi/symbols.yaml is the map: the name and
the address go there, under the precedence the map enforces, and the score, the
variant and the links stay here.
"""

import argparse
import collections
import datetime
import os
import re
import subprocess
import sys

import body_check
import symbols as symmap

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
REF_ROOT = os.path.join(os.environ.get("ROOT",
                        os.path.expanduser("~/ref-build")), "build")
APP_BASE = 0x27000
KGRAM = 6
# A k-gram this common is a generic prologue; using it as an anchor only costs time.
MAX_ANCHOR_HITS = 400
# Alignment slack, in instructions, for inline decisions that differ.
BAND = 12

OBJDUMP = ["llvm-objdump", "-d", "--triple=thumbv7em-none-eabi", "--mattr=+vfp4"]

INSN = re.compile(r"^\s*([0-9a-f]+):\s+((?:[0-9a-f]{2,4} )+)\s*\t(\S+)\s*(.*)$")
RELOC = re.compile(r"^\s*[0-9a-f]+:\s+(R_ARM_\S+)\s+(\S+)")
SYMHDR = re.compile(r"^([0-9a-f]+) <([^>]+)>:")

PCREL = re.compile(r"\[pc, #-?0x[0-9a-f]+\]")
TARGET = re.compile(r"\b0x[0-9a-f]+\b")
BRANCH = ("b", "bl", "blx", "bx", "beq", "bne", "bcs", "bcc", "bmi", "bpl", "bvs",
          "bvc", "bhi", "bls", "bge", "blt", "bgt", "ble", "cbz", "cbnz")


def normalise(mnem, ops):
    """Drop everything that depends on where the code was linked."""
    ops = ops.split("@")[0].strip()
    ops = re.sub(r"<[^>]*>", "", ops)
    ops = PCREL.sub("[pc]", ops)
    base = mnem.split(".")[0].rstrip("s")
    if base in BRANCH or mnem.startswith(("b.", "bl.", "tbb", "tbh")):
        ops = TARGET.sub("T", ops)
    # An immediate this large is an address or a link-time constant, not structure.
    ops = re.sub(r"#0x[0-9a-f]{5,}", "#A", ops)
    return "%s %s" % (mnem, " ".join(ops.split()))


def disassemble(path, extra=()):
    out = subprocess.run(OBJDUMP + list(extra) + [path], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("llvm-objdump failed on %s:\n%s" % (path, out.stderr))
    return out.stdout


def parse_reference(elf):
    """-> {symbol: {'tokens': [...], 'calls': {index: callee}, 'size': bytes}}"""
    text = disassemble(elf, ["-r"])
    funcs, cur = {}, None
    for line in text.splitlines():
        h = SYMHDR.match(line)
        if h:
            cur = {"tokens": [], "calls": {}, "size": 0, "name": h.group(2)}
            funcs[h.group(2)] = cur
            continue
        if cur is None:
            continue
        r = RELOC.match(line)
        if r:
            # The relocation follows the instruction it applies to.
            if r.group(1) in ("R_ARM_THM_CALL", "R_ARM_THM_JUMP24") and cur["tokens"]:
                # -ffunction-sections makes a static callee relocate against its
                # own section, so the symbol arrives as ".text.<name>".
                cur["calls"][len(cur["tokens"]) - 1] = r.group(2).split("+")[0].replace(".text.", "")
            continue
        m = INSN.match(line)
        if not m:
            continue
        nbytes = len(m.group(2).split())
        mnem, ops = m.group(3), m.group(4)
        if mnem in (".word", ".short", ".byte"):
            continue  # literal pool, not code
        cur["tokens"].append(normalise(mnem, ops))
        cur["size"] = int(m.group(1), 16) + nbytes
    return {k: v for k, v in funcs.items() if v["tokens"]}


def parse_image(binpath, base):
    """Linear disassembly from `base` and from base+2, as two token streams.

    Two streams because a literal pool desynchronises the linear pass, and a
    function landing on the wrong phase would otherwise be invisible.
    """
    elf = os.path.join(SIM, "out", "match_image.elf")
    os.makedirs(os.path.dirname(elf), exist_ok=True)
    streams = []
    for skip in (0, 2):
        src = binpath
        if skip:
            src = os.path.join(SIM, "out", "match_image_%d.bin" % skip)
            with open(binpath, "rb") as f:
                data = f.read()[skip:]
            with open(src, "wb") as f:
                f.write(data)
        subprocess.run(["llvm-objcopy", "-I", "binary", "-O", "elf32-littlearm",
                        "--rename-section", ".data=.text,alloc,load,readonly,code",
                        src, elf], check=True)
        text = disassemble(elf, ["--adjust-vma=0x%x" % (base + skip)])
        addrs, tokens, targets = [], [], []
        for line in text.splitlines():
            m = INSN.match(line)
            if not m:
                continue
            mnem, ops = m.group(3), m.group(4)
            if mnem in (".word", ".short", ".byte"):
                continue
            addrs.append(int(m.group(1), 16))
            tokens.append(normalise(mnem, ops))
            t = TARGET.search(ops.split("@")[0])
            base_m = mnem.split(".")[0].rstrip("s")
            targets.append(int(t.group(0), 16) if (t and base_m in BRANCH) else None)
        streams.append({"addrs": addrs, "tokens": tokens, "targets": targets,
                        "index": {a: i for i, a in enumerate(addrs)}})
    # Every `bl` destination is a function entry. Restricting a match's start to
    # this set is what stops an alignment settling an instruction or two off.
    entries = set()
    for st in streams:
        for tok, tgt in zip(st["tokens"], st["targets"]):
            if tgt is not None and tok.startswith("bl"):
                entries.add(tgt)
    for st in streams:
        st["entries"] = entries
    return streams


def build_kgram_index(streams):
    idx = collections.defaultdict(list)
    for s, st in enumerate(streams):
        toks = st["tokens"]
        for i in range(len(toks) - KGRAM + 1):
            idx["\n".join(toks[i:i + KGRAM])].append((s, i))
    return idx


def candidates(fn, streams, idx):
    """Vote for image start positions from every k-gram of the function.

    Votes are bucketed loosely because an inserted or deleted instruction shifts
    the implied start; the alignment below sorts out the exact offset.
    """
    toks = fn["tokens"]
    k = min(KGRAM, len(toks))
    votes = collections.Counter()
    for off in range(len(toks) - k + 1):
        hits = idx.get("\n".join(toks[off:off + k]))
        if not hits or len(hits) > MAX_ANCHOR_HITS:
            continue
        for s, i in hits:
            if i - off >= 0:
                votes[(s, i - off)] += 1
    if not votes:
        return []
    # Collapse starts within BAND of each other onto their best-voted member.
    ordered = [c for c, _ in votes.most_common(40)]
    picked = []
    for c in ordered:
        if all(c[0] != p[0] or abs(c[1] - p[1]) > BAND for p in picked):
            picked.append(c)
        if len(picked) >= 8:
            break
    return picked


def align(ref, img, start, band=BAND):
    """Banded alignment of the reference tokens onto the image from `start`.

    Returns (matches, image index the reference's first token aligned to).
    A plain positional compare would be destroyed by a single extra instruction,
    which differing inline decisions produce all the time.
    """
    n = len(ref)
    width = 2 * band + 1
    # dp[d] = best match count with the image index running n_ref + d - band.
    NEG = -1 << 30
    dp = [NEG] * width
    origin = [None] * width
    if not (0 <= start < len(img)):
        return 0, None
    dp[band] = 0
    origin[band] = start
    for i in range(n):
        nd = [NEG] * width
        no = [None] * width
        for d in range(width):
            if dp[d] == NEG:
                continue
            j = start + i + d - band
            base, org = dp[d], origin[d]
            # consume both
            if 0 <= j < len(img):
                v = base + (1 if img[j] == ref[i] else 0)
                if v > nd[d]:
                    nd[d], no[d] = v, org
            # skip an image instruction (reference is missing it)
            if d + 1 < width and base > nd[d + 1]:
                nd[d + 1], no[d + 1] = base, org
            # skip a reference instruction (image is missing it)
            if d - 1 >= 0 and base > nd[d - 1]:
                nd[d - 1], no[d - 1] = base, org
        dp, origin = nd, no
    bestd = max(range(width), key=lambda d: dp[d])
    if dp[bestd] == NEG:
        return 0, None
    return dp[bestd], origin[bestd]


def score_at(fn, streams, addr):
    """Score the reference function against the image at a known address."""
    bestr = None
    for s, st in enumerate(streams):
        if addr not in st["index"]:
            continue
        r = score(fn, streams, (s, st["index"][addr]))
        if r and r["address"] == addr and (bestr is None or r["score"] > bestr["score"]):
            bestr = r
    return bestr


def score(fn, streams, cand):
    """Best alignment anchored on a position whose instruction is the prologue.

    The entry instruction has to match: a function's address is exactly where
    its first instruction is, and letting the alignment slide onto a neighbour
    is how a match lands two bytes off.
    """
    s, start = cand
    st = streams[s]
    toks, n = fn["tokens"], len(fn["tokens"])
    lo, hi = max(0, start - BAND), min(len(st["tokens"]), start + BAND + 1)
    window = list(range(lo, hi))
    known_entry = [j for j in window if st["addrs"][j] in st["entries"]]
    if known_entry:
        window = known_entry
    bestr, bestkey = None, None
    for j in window:
        hits, _ = align(toks, st["tokens"], j)
        sc = hits / n
        # A function's address is where its first instruction is, so on a near
        # tie prefer the start whose instruction is the reference prologue, and
        # the phase-0 stream over the +2 one.
        key = (round(sc, 2), st["tokens"][j] == toks[0], s == 0, sc)
        if bestkey is None or key > bestkey:
            bestkey = key
            bestr = {"score": sc, "stream": s, "pos": j,
                     "address": st["addrs"][j], "insns": n}
    return bestr


# Names the repo has already settled by bytes rather than by score. An autonames
# entry whose evidence is a reproduced body is a fact about that address; a token
# alignment that lands the same name elsewhere is a measurement, and the fact wins.
BYTE_EVIDENCE = re.compile(r"bytes of the built body reproduced at (0x[0-9a-f]+)")


def byte_confirmed(path):
    """-> {symbol: address} for every autonames entry a byte comparison settled."""
    out = {}
    if not os.path.exists(path):
        return out
    name = None
    for line in open(path):
        if line.startswith("  - name: "):
            name = line.split(": ", 1)[1].strip()
        elif name and line.startswith("    evidence:"):
            m = BYTE_EVIDENCE.search(line)
            if m:
                out[name] = int(m.group(1), 16)
    return out


def recorded_meta(path):
    """The variant list and threshold the file on disk was measured against.

    A re-run is only comparable against the same reference builds, and
    ~/ref-build grows one whenever anybody recovers a driver, so the set has to
    come from the record and not from what happens to be on disk today.
    """
    meta = {}
    if not os.path.exists(path):
        return meta
    for line in open(path):
        if line.startswith("functions:"):
            break
        for key in ("variants", "threshold"):
            if line.startswith("  %s:" % key):
                meta[key] = line.split(":", 1)[1].strip()
    return meta


def bl_target(blob, off, addr):
    """Decode the Thumb-2 BL/B.W at `off` in a body based at `addr`."""
    if off + 4 > len(blob):
        return None
    h1 = blob[off] | (blob[off + 1] << 8)
    h2 = blob[off + 2] | (blob[off + 3] << 8)
    if (h1 & 0xF800) != 0xF000 or (h2 & 0xC000) != 0xC000:
        return None
    s = (h1 >> 10) & 1
    imm = ((s << 24) | ((~((h2 >> 13) & 1) ^ s) & 1) << 23
           | ((~((h2 >> 11) & 1) ^ s) & 1) << 22
           | ((h1 & 0x3FF) << 12) | ((h2 & 0x7FF) << 1))
    if s:
        imm -= 1 << 25
    return addr + off + 4 + imm


class Verifier(object):
    """The byte-level verdict on a proposed match, per reference build.

    The token matcher proposes: it says this reference body and this image
    address have the same shape. That is a measurement, and a measurement moves
    when the reference set moves. Whether the bytes the compiler wrote are the
    bytes the image carries is a fact about one body and one build, so a
    reference added tomorrow can propose more candidates but cannot unsettle
    one. Relocated fields are masked because the link writes them, and a call
    the reference resolves inside its own partial link has to land on the
    address that callee was given or the body is not the body.
    """

    def __init__(self, image):
        with open(image, "rb") as fh:
            self.blob = fh.read()
        self.objects, self.relocs = {}, {}

    def object_of(self, variant):
        if variant not in self.objects:
            elf = os.path.join(REF_ROOT, variant, "ref.elf")
            try:
                self.objects[variant] = body_check.Object(elf)
            except (subprocess.CalledProcessError, OSError):
                self.objects[variant] = None
        return self.objects[variant]

    def calls_of(self, obj, section):
        """(offset, callee) for every call relocation of one .text section."""
        key = (id(obj), section)
        if key in self.relocs:
            return self.relocs[key]
        table, current = collections.defaultdict(list), None
        for line in obj.run(["objdump", "-r", obj.path]).splitlines():
            if line.startswith("RELOCATION RECORDS FOR ["):
                current = line.split("[")[1].split("]")[0]
                continue
            row = line.split()
            if current is None or len(row) < 3 or not row[1].startswith("R_ARM_THM"):
                continue
            if row[1] not in ("R_ARM_THM_CALL", "R_ARM_THM_JUMP24"):
                continue
            try:
                at = int(row[0], 16)
            except ValueError:
                continue
            table[current].append((at, row[2].split("+")[0].replace(".text.", "")))
        self.relocs.update(((id(obj), k), v) for k, v in table.items())
        self.relocs.setdefault(key, [])
        return self.relocs[key]

    def verdict(self, symbol, addr, variant, addr_of):
        """-> (verdict, masked byte count, why) for one proposed match."""
        obj = self.object_of(variant.split("+")[0])
        if obj is None or symbol not in obj.bodies:
            return "absent", 0, "%s defines no %s" % (variant, symbol)
        built, owned = obj.body(symbol)
        off = addr - APP_BASE
        there = self.blob[off:off + len(built)]
        if len(there) != len(built):
            return "differs", 0, "0x%x runs off the end of the image" % addr
        bad = [i for i in range(len(built)) if built[i] != there[i] and i not in owned]
        if bad:
            return "differs", len(owned), ("%d of %d bytes differ outside the %d"
                                           " a relocation owns, first at +0x%x"
                                           % (len(bad), len(built), len(owned), bad[0]))
        section = obj.bodies[symbol][0]
        for at, callee in self.calls_of(obj, section):
            want = addr_of.get(callee)
            if want is None or at not in owned:
                continue
            got = bl_target(there, at, addr)
            if got is not None and got != want:
                return "refuted", len(owned), ("+0x%x calls %s, which is 0x%x,"
                                               " but the image branches to 0x%x"
                                               % (at, callee, want, got))
        return ("exact" if built == there else "masked"), len(owned), ""

    def settle(self, symbol, addr, variant, addr_of, reach=8):
        """-> (address, verdict, masked, why), sliding the body if that settles it.

        The token alignment fixes the address from the first instruction whose
        normalised form matched, and a prologue GCC spelled differently on the
        two sides puts that a few instructions into the body: prvIdleTask was
        recorded six bytes past its `push`, where 67 of its 124 bytes differ,
        while at its real start every byte agrees. The byte comparison is the
        verdict this file publishes, so where it settles a nearby address it
        also decides the address.
        """
        verdict, masked, why = self.verdict(symbol, addr, variant, addr_of)
        if verdict in ("exact", "masked", "absent"):
            return addr, verdict, masked, why
        for delta in range(-reach, reach + 1, 2):
            if not delta:
                continue
            v, m, w = self.verdict(symbol, addr + delta, variant, addr_of)
            if v in ("exact", "masked"):
                return addr + delta, v, m, w
        return addr, verdict, masked, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="",
                    help="comma list; empty means the set recorded in the output file")
    ap.add_argument("--all-variants", action="store_true",
                    help="scan every build under ~/ref-build/build instead")
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--out", default=os.path.join(HERE, "matches.yaml"))
    ap.add_argument("--autonames", default=os.path.join(HERE, "autonames.yaml"))
    ap.add_argument("--meta", default=os.path.join(HERE, "matches.yaml"),
                    help="the record a run takes its variant set and threshold from,"
                         " and compares its result against")
    ap.add_argument("--threshold", type=float, default=None,
                    help="empty means the threshold recorded in the output file")
    ap.add_argument("--propagate-threshold", type=float, default=0.60,
                    help="score a call-graph-supplied address must still reach")
    ap.add_argument("--min-insns", type=int, default=4)
    ap.add_argument("--check", action="store_true", help="print accuracy only")
    ap.add_argument("--prototypes", action="store_true",
                    help="only refresh the declarations of the matches already"
                         " recorded, without re-running the matcher")
    args = ap.parse_args()

    if args.prototypes:
        return refresh_prototypes(args.out)

    meta = recorded_meta(args.meta)
    if args.threshold is None:
        args.threshold = float(meta.get("threshold", 0.90))
    if args.all_variants:
        # Whatever is in ~/ref-build/build with a ref.elf. abi/refbuild.sh
        # builds two, so this only means anything to somebody who has put a
        # candidate of their own next to them.
        args.variants = ",".join(sorted(d for d in os.listdir(REF_ROOT)
                                        if os.path.exists(os.path.join(REF_ROOT, d, "ref.elf"))))
    elif not args.variants:
        if "variants" not in meta:
            sys.exit("%s records no variant set; pass --variants or --all-variants"
                     % args.meta)
        args.variants = meta["variants"].strip("[]").replace(" ", "")
    variants = args.variants.split(",")
    order = {v: i for i, v in enumerate(variants)}
    streams = parse_image(args.image, APP_BASE)
    idx = build_kgram_index(streams)

    best = {}      # symbol -> best match record
    # Every proposal a variant made for a symbol, so that a tie between two
    # builds that put the same name at two addresses is reported rather than
    # decided by which directory os.listdir happened to hand over first.
    proposals = collections.defaultdict(dict)
    for variant in variants:
        elf = os.path.join(REF_ROOT, variant, "ref.elf")
        if not os.path.exists(elf):
            sys.exit("no reference build %s (run abi/refbuild.sh)" % elf)
        refs = parse_reference(elf)
        for name, fn in refs.items():
            if len(fn["tokens"]) < args.min_insns:
                continue
            for cand in candidates(fn, streams, idx):
                r = score(fn, streams, cand)
                if r is None:
                    continue
                seen = proposals[name].get(variant)
                if seen is None or r["score"] > seen["score"]:
                    proposals[name][variant] = dict(r, variant=variant)
                # The scan order is now the recorded variant order, so a tie
                # resolves the same way on every machine; it is still a tie and
                # still gets said out loud below.
                if name not in best or r["score"] > best[name]["score"]:
                    r.update(variant=variant, symbol=name, fn=fn)
                    best[name] = r

    ties = {}
    for name, rec in best.items():
        if rec["score"] < args.threshold:
            continue  # a tie between two rejections decides nothing
        rival = sorted(set("%s@0x%x" % (v, p["address"])
                           for v, p in proposals[name].items()
                           if round(p["score"], 3) == round(rec["score"], 3)
                           and p["address"] != rec["address"]))
        if rival:
            ties[name] = rival
    for name in sorted(ties):
        print("  TIE    %-28s 0x%-7x %.3f %s also at %s"
              % (name, best[name]["address"], best[name]["score"],
                 best[name]["variant"], ", ".join(ties[name])))

    accepted = {k: v for k, v in best.items() if v["score"] >= args.threshold}
    for v in accepted.values():
        v["how"] = "direct"

    # Call-graph propagation. A reference `bl` carries a relocation naming its
    # callee, so a confirmed caller hands us the callee's exact image address --
    # far stronger than searching for it, because the address is given, not
    # guessed, and only has to be corroborated by the instructions found there.
    all_refs = {v["symbol"]: v["fn"] for v in best.values()}
    for variant in variants:
        for name, fn in parse_reference(os.path.join(REF_ROOT, variant, "ref.elf")).items():
            all_refs.setdefault(name, fn)

    for _ in range(12):
        grew = False
        for v in list(accepted.values()):
            st = streams[v["stream"]]
            for i, callee in v["fn"]["calls"].items():
                if v["pos"] + i >= len(st["targets"]):
                    continue
                tgt = st["targets"][v["pos"] + i]
                if tgt is None or callee in accepted or callee not in all_refs:
                    continue
                r = score_at(all_refs[callee], streams, tgt)
                if r and r["score"] >= args.propagate_threshold:
                    r.update(variant=v["variant"].split("+")[0] + "+graph", symbol=callee,
                             fn=all_refs[callee], how="propagated from " + v["symbol"])
                    accepted[callee] = r
                    grew = True
        if not grew:
            break

    addr_of = {v["symbol"]: v["address"] for v in accepted.values()}
    for v in accepted.values():
        st = streams[v["stream"]]
        links, broken = [], 0
        for i, callee in v["fn"]["calls"].items():
            tgt = st["targets"][v["pos"] + i] if v["pos"] + i < len(st["targets"]) else None
            if tgt is None or callee not in addr_of:
                continue
            if addr_of[callee] == tgt:
                links.append("%s@0x%x" % (callee, tgt))
            else:
                broken += 1
        v["links"], v["broken"] = links, broken

    known = {
        "xQueueGenericSend": 0x73a90, "xQueueSemaphoreTake": 0x9e890,
        "xTaskNotifyWait": 0x7382c, "vTaskSwitchContext": 0x7333c,
        "vPortSuppressTicksAndSleep": 0x73ec8, "vPortEnableVFP": 0x73dfc,
        "PendSV_Handler": 0x27e50, "SVC_Handler": 0x27e20,
        "prvCopyDataToQueue": 0x9e590, "xTaskIncrementTick": 0x730bc,
        "vTaskStepTick": 0x730ac, "xTaskCreateStatic": 0x9e3dc,
        "xQueueGenericCreateStatic": 0x9e752, "nrfx_spim_init": 0x3aac4,
        "nrfx_saadc_init": 0x7dcc0, "nrfx_saadc_channel_init": 0x7de34,
        "nrfx_saadc_buffer_convert": 0x7df40, "nrfx_saadc_sample_convert": 0x7dfb8,
    }
    ok = wrong = missing = 0
    detail = []
    for name, addr in sorted(known.items(), key=lambda kv: kv[1]):
        m = accepted.get(name)
        if not m:
            missing += 1
            near = best.get(name)
            detail.append("  MISS   %-28s want 0x%-7x best %s"
                          % (name, addr, "0x%x score %.2f %s" % (near["address"], near["score"], near["variant"]) if near else "none"))
        elif m["address"] == addr:
            ok += 1
            detail.append("  ok     %-28s 0x%-7x %.2f %-12s links=%d %s"
                          % (name, addr, m["score"], m["variant"], len(m["links"]), m["how"]))
        else:
            wrong += 1
            detail.append("  WRONG  %-28s want 0x%-7x got 0x%x (%.2f %s)"
                          % (name, addr, m["address"], m["score"], m["variant"]))
    print("\n".join(detail))
    total_known = len(known)
    prec = ok / (ok + wrong) if (ok + wrong) else 0.0
    rec = ok / total_known
    print("\nknown set: %d correct, %d wrong, %d missing -> precision %.2f recall %.2f"
          % (ok, wrong, missing, prec, rec))
    print("accepted %d symbols (of %d reference functions considered)"
          % (len(accepted), len(best)))
    if args.check:
        return

    # One image address, one symbol. FreeRTOS is full of near-identical pairs
    # (xQueueReceive/xQueuePeek, the per-peripheral nrf_*_event_clear inlines),
    # and the manifest cannot carry two names for one address.
    by_addr = collections.defaultdict(list)
    for v in accepted.values():
        by_addr[v["address"]].append(v)
    resolved, ambiguous = {}, {}
    for addr, group in by_addr.items():
        group.sort(key=lambda v: (-v["score"], v["symbol"]))
        winner = group[0]
        resolved[winner["symbol"]] = winner
        if len(group) > 1:
            ambiguous[addr] = [v["symbol"] for v in group[1:]]
            winner["ambiguous_with"] = ambiguous[addr]

    # An autonames entry settled by bytes outranks an alignment. The matcher
    # cut __kernel_rem_pio2 at its 25th byte and read the name onto 0x2c320;
    # the built body reproduces at 0x2c308, and a reproduced body is where the
    # function is. Yield the name to the address rather than overriding it.
    confirmed = byte_confirmed(args.autonames)
    for name, addr in sorted(confirmed.items()):
        m = resolved.get(name)
        if m is None or m["address"] == addr:
            continue
        moved = score_at(m["fn"], streams, addr)
        was = m["address"]
        m.update(address=addr, how="byte-confirmed by autonames")
        if moved:
            m.update(score=moved["score"], stream=moved["stream"], pos=moved["pos"])
        print("  YIELD  %-28s 0x%-7x -> 0x%x (autonames reproduced the body there)"
              % (name, was, addr))

    addr_of = {v["symbol"]: v["address"] for v in resolved.values()}
    addr_of.update(confirmed)
    verifier = Verifier(args.image)
    counts = collections.Counter()
    for name, m in sorted(resolved.items()):
        settled, verdict, masked, why = verifier.settle(
            name, m["address"], m["variant"], addr_of)
        if settled != m["address"]:
            print("  SLIDE  %-28s 0x%-7x -> 0x%x (the byte verdict settles there)"
                  % (name, m["address"], settled))
            m["address"] = settled
            addr_of[name] = settled
        if verdict not in ("exact", "masked"):
            m["divergence"] = why
            verdict = "token_only" if verdict != "refuted" else "refuted"
        m["verdict"], m["masked_bytes"] = verdict, masked
        counts[verdict] += 1
    print("\nverdicts: " + ", ".join("%s %d" % kv for kv in sorted(counts.items())))
    for name, m in sorted(resolved.items()):
        if m["verdict"] != "exact" and m["verdict"] != "masked":
            print("  %-10s %-28s 0x%-7x %s"
                  % (m["verdict"], name, m["address"], m.get("divergence", "")))

    # A run that drops a name the record carries is a regression in the run, not
    # a correction of the record: the variant set is pinned precisely so that
    # adding a reference cannot take a match away.
    was = set(ln.split(": ", 1)[1].strip() for ln in open(args.meta)
              if ln.startswith("  - name: ")) if os.path.exists(args.meta) else set()
    for name in sorted(was - set(resolved)):
        print("  LOST   %-28s the record has it, this run does not" % name)
    for name in sorted(set(resolved) - was):
        print("  NEW    %-28s 0x%-7x %s %s"
              % (name, resolved[name]["address"], resolved[name]["verdict"],
                 resolved[name]["variant"]))

    protos = load_prototypes(set(resolved))
    write_matches(args.out, resolved, best, streams, protos, args.threshold,
                  {"ok": ok, "wrong": wrong, "missing": missing,
                   "precision": prec, "recall": rec},
                  variants, ties, counts)
    print("wrote %s" % args.out)

    # The record above is the measurement; the map is where a name goes. A
    # matched name the map already carries at another address, or an address it
    # already calls something else, is a refusal rather than a second entry:
    # this run and the map disagree and which of the two is wrong is not the
    # matcher's to decide.
    try:
        added = symmap.load().rewrite(map_entries(resolved), {MAP_CLASS})
    except symmap.Refusal as e:
        sys.exit("abi/match.py: abi/symbols.yaml: %s" % e)
    print("wrote %d %s entries to abi/symbols.yaml" % (added, MAP_CLASS))


# A declaration, with the attribute macros a header puts between the closing
# parenthesis and the semicolon left out of the capture: every kernel prototype
# in the SDK's FreeRTOS ends `) PRIVILEGED_FUNCTION;`, and a pattern that wants
# the semicolon next found none of them.
DECL = re.compile(r"([A-Za-z_][\w \t\*\(\)]*?\b(\w+)\s*\([^;{)]*\))"
                  r"(?:\s+[A-Z_][A-Z0-9_]*)*\s*;")


def load_prototypes(names):
    """Pull each matched symbol's declaration out of the SDK headers verbatim."""
    sdk = os.path.expanduser("~/ref-build/sdk/nRF5_SDK_17.1.0_ddde560")
    if not os.path.isdir(sdk):
        return {}
    wanted, protos = set(names), {}
    pat = re.compile(r"\b(%s)\s*\(" % "|".join(re.escape(n) for n in wanted)) if wanted else None
    for root, _, files in os.walk(sdk):
        if "examples" in root:
            continue
        for f in files:
            # The MPU wrapper header redeclares the whole kernel API; it is never
            # the header the caller should include.
            if not f.endswith(".h") or f == "mpu_prototypes.h":
                continue
            path = os.path.join(root, f)
            try:
                with open(path, errors="ignore") as fh:
                    src = fh.read()
            except OSError:
                continue
            if not pat or not pat.search(src):
                continue
            src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
            src = re.sub(r"//[^\n]*", " ", src)
            for m in DECL.finditer(src):
                name = m.group(2)
                if name in wanted and name not in protos:
                    decl = " ".join(m.group(1).split())
                    for kw in ("extern ", "__STATIC_INLINE ", "NRFX_STATIC_INLINE ",
                               "static inline ", "static "):
                        if decl.startswith(kw):
                            decl = decl[len(kw):]
                    protos[name] = {"proto": decl + ";",
                                    "header": os.path.relpath(path, sdk)}
    return protos


def refresh_prototypes(path):
    """Fill in the declaration of every match already recorded, in place.

    A prototype is a property of the name and not of the measurement, so it can
    be refreshed without re-running the matcher -- which matters, because a
    re-run is only comparable against the same set of reference builds and
    ~/ref-build grows one whenever anybody recovers a driver. Only the `header`
    and `proto` lines of each entry are rewritten; every address, score and link
    stays exactly as it was measured.
    """
    with open(path) as fh:
        lines = fh.read().splitlines()
    names = [ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("  - name: ")]
    protos = load_prototypes(set(names))
    out, i, added = [], 0, 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("  - name: "):
            out.append(line)
            i += 1
            continue
        name = line.split(":", 1)[1].strip()
        body = [line]
        i += 1
        while i < len(lines) and lines[i].startswith("    ") and \
                not lines[i].startswith(("    header:", "    proto:")):
            body.append(lines[i])
            i += 1
        while i < len(lines) and lines[i].startswith(("    header:", "    proto:")):
            i += 1
        p = protos.get(name)
        if p:
            body.append("    header: %s" % p["header"])
            body.append("    proto: \"%s\"" % p["proto"].replace('"', '\\"'))
            added += 1
        out += body
    with open(path, "w") as fh:
        fh.write("\n".join(out) + "\n")
    print("%s: %d of %d matches carry a declaration" % (path, added, len(names)))
    for name in sorted(set(names) - set(protos)):
        print("  no header declares %s" % name)
    return 0


# The class the matcher owns in abi/symbols.yaml, and the only one it rewrites.
MAP_CLASS = "match"


def map_entries(accepted):
    """Every settled match as an abi/symbols.yaml row.

    The map carries the name, the address and what established them; the score,
    the variant and the links stay in abi/matches.yaml, which is the
    measurement and not the map.
    """
    rows = []
    for name, m in sorted(accepted.items(), key=lambda kv: kv[1]["address"]):
        rows.append({"address": m["address"], "name": name, "kind": "function",
                     "class": MAP_CLASS,
                     "evidence": "%s, score %.2f over %d insns, %s"
                                 % (m["variant"], m["score"], m["insns"],
                                    m["how"])})
    return rows


def write_matches(path, accepted, best, streams, protos, threshold, acc,
                  variants, ties, verdicts):
    lines = [
        "# Generated by abi/match.py -- do not edit by hand.",
        "# Image addresses of open-source functions, recovered by matching the",
        "# normalised instruction sequences of a reference build (abi/refbuild.sh)",
        "# against a linear disassembly of appl.bin. `score` is the fraction of",
        "# instructions that matched; `links` are call-graph confirmations (a",
        "# reference bl's relocated callee found at the image branch target).",
        "",
        "meta:",
        "  image: appl.bin",
        "  app_base: 0x%x" % APP_BASE,
        "  sdk: nRF5_SDK_17.1.0_ddde560",
        "  toolchain: arm-gnu-toolchain-13.2.Rel1",
        "  saadc_toolchain: arm-gnu-toolchain-13.2.Rel1, nrfx 2.1.0 (abi/refbuild.sh)",
        "  libm_toolchain: arm-gnu-toolchain-13.2.Rel1 (newlib 4.3.0.20230120)",
        "  measured: %s" % datetime.date.today().isoformat(),
        "  threshold: %.2f" % threshold,
        "  # The reference builds this measurement is comparable against, and the",
        "  # set a re-run uses unless --variants or --all-variants says otherwise:",
        "  # ~/ref-build grows a variant whenever anybody recovers a driver, and a",
        "  # score is only a score against a fixed set of bodies.",
        "  variants: [%s]" % ", ".join(variants),
        "  # `verdict` is the byte comparison of the recorded variant's body",
        "  # against the image with relocated fields masked (abi/body_check.py),",
        "  # plus agreement of every call the reference resolves itself. A verdict",
        "  # is a fact about one body and one build, so adding references can add",
        "  # candidates but cannot unsettle one. token_only entries are carried by",
        "  # the alignment score alone and are not settled.",
        "  verdicts: {%s}" % ", ".join("%s: %d" % kv for kv in sorted(verdicts.items())),
        "  ties: %d" % len(ties),
        "  # Two recipes, because the image has two builds in it, and the hunt",
        "  # that found them is over: FreeRTOS and nrfx across four kernel",
        "  # versions, two nrfx trees, GCC 9/12.3/13.2/13.3/14.2, -Os/-O2/-O3 and",
        "  # three assert flavours; newlib across reent-small and single-thread;",
        "  # CMSIS-DSP 1.9.0/1.10.0/1.14.4 and KissFFT 131.1.0 against the",
        "  # hard-float blocks in SENSORS_SYNC and ECG, which reached one",
        "  # 17-instruction body eight of its own transform sizes share and",
        "  # nothing else. abi/refbuild.sh builds only the two that won; git",
        "  # history has the rest.",
        "  # accuracy against the addresses abi/symbols.yaml already establishes by hand",
        "  known_set: {correct: %d, wrong: %d, missing: %d, precision: %.2f, recall: %.2f}"
        % (acc["ok"], acc["wrong"], acc["missing"], acc["precision"], acc["recall"]),
        "",
        "functions:",
    ]
    for name, m in sorted(accepted.items(), key=lambda kv: kv[1]["address"]):
        lines.append("  - name: %s" % name)
        lines.append("    address: 0x%x" % m["address"])
        lines.append("    variant: %s" % m["variant"])
        lines.append("    verdict: %s" % m["verdict"])
        if m.get("masked_bytes"):
            lines.append("    masked_bytes: %d" % m["masked_bytes"])
        if m.get("divergence"):
            lines.append("    divergence: \"%s\"" % m["divergence"])
        lines.append("    score: %.3f" % m["score"])
        lines.append("    insns: %d" % m["insns"])
        lines.append("    how: %s" % m["how"])
        lines.append("    links: [%s]" % ", ".join(m["links"]))
        if m["broken"]:
            lines.append("    broken_links: %d" % m["broken"])
        if m.get("ambiguous_with"):
            lines.append("    ambiguous_with: [%s]" % ", ".join(m["ambiguous_with"]))
        p = protos.get(name)
        if p:
            lines.append("    header: %s" % p["header"])
            lines.append("    proto: \"%s\"" % p["proto"].replace('"', '\\"'))
    # Runs of matched functions bound the library-looking regions; a call target
    # sitting inside such a run that carries no symbol is a library function the
    # reference build did not cover (a driver Withings replaced, or a kernel
    # source not built here).
    matched = sorted(m["address"] for m in accepted.values())
    entries = sorted(streams[0]["entries"])
    runs, run = [], [matched[0]] if matched else []
    for a in matched[1:]:
        if a - run[-1] <= 0x800:
            run.append(a)
        else:
            runs.append((run[0], run[-1]))
            run = [a]
    if run:
        runs.append((run[0], run[-1]))
    lines += ["", "library_ranges:  # spans of the image the matches bracket"]
    for lo, hi in runs:
        lines.append("  - {start: 0x%x, end: 0x%x, matched: %d}"
                     % (lo, hi, sum(1 for a in matched if lo <= a <= hi)))
    lines += ["", "unmatched_image:  # call targets inside those spans with no symbol"]
    seen = set(matched)
    for lo, hi in runs:
        for a in entries:
            if lo <= a <= hi and a not in seen:
                lines.append("  - 0x%x" % a)

    lines += ["", "unmatched_reference:"]
    for name in sorted(set(best) - set(accepted)):
        lines.append("  - %s  # best %.2f" % (name, best[name]["score"]))
    lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
