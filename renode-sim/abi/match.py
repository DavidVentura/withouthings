#!/usr/bin/env python3
"""Map HWA10 image addresses to open-source symbols by instruction matching.

    python3 abi/match.py                     # all variants, writes abi/matches.yaml
    python3 abi/match.py --check             # only score against the known symbols
    python3 abi/match.py --variants Os,O2    # restrict the reference builds used

Reference ELFs come from abi/refbuild.sh (~/ref-build/build/<variant>/ref.elf).
Both sides are disassembled with llvm-objdump so the mnemonic spelling agrees;
instructions are normalised (branch targets, pc-relative pool loads and absolute
addresses masked, register/immediate structure kept) and the image is searched
for each reference function's token sequence. Matches are then confirmed through
the call graph: a reference `bl` carries a relocation naming its callee, so the
branch target at the same index in the image must be the address matched to that
callee.
"""

import argparse
import collections
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
REF_ROOT = os.path.expanduser("~/ref-build/build")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="", help="comma list; empty means every build under ~/ref-build/build")
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--out", default=os.path.join(HERE, "matches.yaml"))
    ap.add_argument("--threshold", type=float, default=0.90)
    ap.add_argument("--propagate-threshold", type=float, default=0.60,
                    help="score a call-graph-supplied address must still reach")
    ap.add_argument("--min-insns", type=int, default=4)
    ap.add_argument("--check", action="store_true", help="print accuracy only")
    args = ap.parse_args()

    if not args.variants:
        # The mbedtls_* variants belong to abi/autonames.py's extlib class, not
        # to the kernel/driver boundary this file maps.
        args.variants = ",".join(sorted(d for d in os.listdir(REF_ROOT)
                                        if not d.startswith("mbedtls_")
                                        and os.path.exists(os.path.join(REF_ROOT, d, "ref.elf"))))
    streams = parse_image(args.image, APP_BASE)
    idx = build_kgram_index(streams)

    best = {}      # symbol -> best match record
    for variant in args.variants.split(","):
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
                if name not in best or r["score"] > best[name]["score"]:
                    r.update(variant=variant, symbol=name, fn=fn)
                    best[name] = r

    accepted = {k: v for k, v in best.items() if v["score"] >= args.threshold}
    for v in accepted.values():
        v["how"] = "direct"

    # Call-graph propagation. A reference `bl` carries a relocation naming its
    # callee, so a confirmed caller hands us the callee's exact image address --
    # far stronger than searching for it, because the address is given, not
    # guessed, and only has to be corroborated by the instructions found there.
    all_refs = {v["symbol"]: v["fn"] for v in best.values()}
    for variant in args.variants.split(","):
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

    protos = load_prototypes(set(resolved))
    write_matches(args.out, resolved, best, streams, protos, args.threshold,
                  {"ok": ok, "wrong": wrong, "missing": missing,
                   "precision": prec, "recall": rec})
    print("wrote %s" % args.out)


DECL = re.compile(r"(?:^|[;{}]|\n)\s*((?:[A-Za-z_][\w \t\*]*?)\b(%s)\s*\([^;{]*?\))\s*;", re.S)


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
            for m in re.finditer(r"([A-Za-z_][\w \t\*\(\)]*?\b(\w+)\s*\([^;{)]*\))\s*;", src):
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


def write_matches(path, accepted, best, streams, protos, threshold, acc):
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
        "  toolchain: gcc-arm-none-eabi-9-2020-q2-update",
        "  threshold: %.2f" % threshold,
        "  # accuracy against the addresses already established in symbols.txt",
        "  known_set: {correct: %d, wrong: %d, missing: %d, precision: %.2f, recall: %.2f}"
        % (acc["ok"], acc["wrong"], acc["missing"], acc["precision"], acc["recall"]),
        "",
        "functions:",
    ]
    for name, m in sorted(accepted.items(), key=lambda kv: kv[1]["address"]):
        lines.append("  - name: %s" % name)
        lines.append("    address: 0x%x" % m["address"])
        lines.append("    variant: %s" % m["variant"])
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
