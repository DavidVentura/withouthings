#!/usr/bin/env python3
"""Derive HWA10 image symbol names that follow mechanically from evidence.

    python3 abi/autonames.py                  # every class, writes abi/autonames.yaml
    python3 abi/autonames.py --classes svc    # one class

Five classes, in order of certainty:

  svc      a `svc #N; bx lr` body is the SoftDevice call whose SVC number is N,
           and the S140 headers give the number and the exact prototype.
  libc     newlib / libgcc bodies, matched the way abi/match.py matches the SDK:
           the toolchain's own archives are disassembled member by member and
           their normalised instruction sequences searched for in the image.
  extlib   the same against the other open-source libraries the image contains
           (mbedTLS built by abi/refbuild.sh, the SDK's prebuilt crypto archives).
  string   a function that passes a bare snake_case identifier as the first
           vararg of a wlog call whose format holds a %s is naming itself; the
           image's `[MODULE][%s] ...` lines are __func__ logging.
  wppcmd   the WPP dispatch table stores {id, handler, name} per row and the
           name is a C identifier, so each handler is named by its own row.
  shell    the UART debug shell's command table, which abuts the WPP one and has
           the same shape, names each command's handler the same way.

Nothing here guesses: every entry carries the evidence that produced it, and
abi/gen.py refuses any name that contradicts hwa10.yaml or matches.yaml.
"""

import argparse
import bisect
import collections
import os
import re
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import match  # noqa: E402  (same directory; the normaliser and matcher live there)

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
ROOT = os.path.expanduser("~/ref-build")
SDK = os.path.join(ROOT, "sdk", "nRF5_SDK_17.1.0_ddde560")
GCC = os.path.join(ROOT, "gcc-arm-none-eabi-9-2020-q2-update")
SD_HEADERS = os.path.join(SDK, "components/softdevice/s140/headers")
APP_BASE = match.APP_BASE

# The libc reference is not the SDK's compiler: the image carries Withings' own
# toolchain, whose newlib names itself in three source paths in the flash
# (/home/mbouillot/dev/pro/cortex-toolchain/src/newlib-4.3.0.20230120/newlib/
# libc/stdlib/{dtoa,mprec,gdtoa-gethex}.c). Arm GNU Toolchain 13.2.Rel1 ships
# exactly that newlib, and its code generation matches the image better than
# 12.3.Rel1's, which ships the same newlib source -- so the difference is the
# compiler, not the library. The archive that matches is libc_nano.a: the full
# libc.a reaches four bodies where the nano one reaches 73, which says Withings
# configured newlib-nano (reent-small, nano-malloc, nano formatted io).
LIBC_TC = os.path.join(ROOT, "tc", "arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi")
LIBC_TC_LABEL = "arm-gnu-toolchain-13.2.Rel1 (newlib 4.3.0.20230120)"
# The app runs with the VFP on and single precision only; +fp/hard beats
# +dp/hard, +fp/softfp and nofp by 9, 12 and 13 matched bodies.
MULTILIB = "thumb/v7e-m+fp/hard"
LIBDIR = os.path.join(LIBC_TC, "arm-none-eabi/lib", MULTILIB)
LIBGCC_GLOB = os.path.join(LIBC_TC, "lib/gcc/arm-none-eabi")
LIBC_ARCHIVES = ["libc_nano.a", "libc.a", "libm.a"]

# Prebuilt, so they can be matched without building anything.
EXT_ARCHIVES = [
    os.path.join(SDK, "external/nrf_cc310/lib/cortex-m4/hard-float/libnrf_cc310_0.9.13.a"),
    os.path.join(SDK, "external/nrf_oberon/lib/cortex-m4/hard-float/liboberon_3.0.8.a"),
    os.path.join(SDK, "external/nrf_oberon/lib/cortex-m4/hard-float/liboberon_mbedtls_3.0.8.a"),
]
# Built from source by abi/refbuild.sh, one directory per variant.
EXT_VARIANTS = ["mbedtls_Os", "mbedtls_O2"]

INSN = re.compile(r"^\s*([0-9a-f]+):\s+((?:[0-9a-f]{2,4} )+)\s*\t(\S+)\s*(.*)$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A function name Withings would have written: snake_case, at least two words.
FUNC_IDENT = re.compile(rb"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")

# The wlog entry points, from symbols.txt; all take the format string in r0.
WLOG_R0 = (0x8F460, 0x8F494, 0x9B1C8, 0x5E7A8)


class Image:
    """The application image plus the parts of its disassembly the classes need."""

    def __init__(self, binpath, dispath):
        with open(binpath, "rb") as f:
            self.data = f.read()
        self.base = APP_BASE
        self.end = self.base + len(self.data)
        self.insns = []          # (address, mnemonic, operands)
        callees = set()
        for line in open(dispath):
            m = INSN.match(line)
            if not m:
                continue
            addr, mnem, ops = int(m.group(1), 16), m.group(3), m.group(4)
            self.insns.append((addr, mnem, ops))
            if mnem in ("bl", "bl.w"):
                t = re.search(r"0x([0-9a-f]+)", ops.split("@")[0])
                if t:
                    callees.add(int(t.group(1), 16))
        self.entries = sorted(a for a in callees if self.base <= a < self.end)

    def owner(self, addr):
        """The `bl` target the address belongs to, i.e. the enclosing function."""
        i = bisect.bisect_right(self.entries, addr) - 1
        return self.entries[i] if i >= 0 else None

    def word(self, va):
        o = va - self.base
        if not 0 <= o <= len(self.data) - 4:
            return None
        return struct.unpack("<I", self.data[o:o + 4])[0]

    def string(self, va, limit=200):
        o = va - self.base
        if not 0 <= o < len(self.data):
            return None
        e = self.data.find(b"\0", o)
        if e < 0 or e - o > limit or e - o < 2:
            return None
        s = self.data[o:e]
        return s if all(32 <= c < 127 or c in (9, 10, 13) for c in s) else None

    def pool_string(self, ops):
        """The string a `ldr rX, [pc, #N]` loads a pointer to, or None.

        llvm-objdump annotates the load with the pool address, but prints it
        without the --adjust-vma offset, so the base has to be added back.
        """
        if "[pc" not in ops:
            return None
        m = re.search(r"@ 0x([0-9a-f]+)", ops)
        if not m:
            return None
        w = self.word(int(m.group(1), 16) + self.base)
        if w is None or not self.base <= w < self.end:
            return None
        return self.string(w)


# ---------------------------------------------------------------- class: svc

SVCALL = re.compile(r"SVCALL\(\s*(\w+)\s*,\s*(.+?)\s*,\s*(\w+)\s*\((.*?)\)\s*\)\s*;", re.S)


def softdevice_svcs():
    """-> {svc number: (symbol, prototype, header)} from the S140 headers.

    The SVC number is an enum constant, so the only honest way to read it is to
    let the compiler evaluate the enum: the constants are emitted into an array
    and read back out of the object file.
    """
    calls, headers = [], []
    for root, _, files in os.walk(SD_HEADERS):
        for f in sorted(files):
            if not f.endswith(".h"):
                continue
            src = open(os.path.join(root, f), errors="ignore").read()
            found = list(SVCALL.finditer(src))
            if found:
                headers.append(f)
            for m in found:
                proto = "%s %s(%s);" % (m.group(2), m.group(3),
                                        " ".join(m.group(4).split()))
                calls.append((m.group(1), m.group(3), proto, f))
    if not calls:
        sys.exit("autonames: no SVCALL declarations under %s" % SD_HEADERS)

    work = os.path.join(SIM, "out", "autonames")
    os.makedirs(work, exist_ok=True)
    csrc = os.path.join(work, "svcnums.c")
    with open(csrc, "w") as f:
        f.write("#define SVCALL(number, return_type, signature)\n")
        for h in headers:
            f.write('#include "%s"\n' % h)
        f.write("const unsigned _svcnums[] = {%s};\n" % ",".join(c[0] for c in calls))
    obj = os.path.join(work, "svcnums.o")
    inc = ["-I" + SD_HEADERS, "-I" + os.path.join(SD_HEADERS, "nrf52"),
           "-I" + os.path.join(SDK, "components/toolchain/cmsis/include"),
           "-I" + os.path.join(SDK, "modules/nrfx/mdk")]
    cc = os.path.join(GCC, "bin/arm-none-eabi-gcc")
    subprocess.run([cc, "-mcpu=cortex-m4", "-mthumb", "-DNRF52840_XXAA", "-DS140",
                    "-DSOFTDEVICE_PRESENT"] + inc + ["-c", csrc, "-o", obj], check=True)
    raw = os.path.join(work, "svcnums.bin")
    subprocess.run([os.path.join(GCC, "bin/arm-none-eabi-objcopy"), "-O", "binary",
                    "--only-section=.rodata", obj, raw], check=True)
    with open(raw, "rb") as f:
        blob = f.read()
    nums = struct.unpack("<%dI" % (len(blob) // 4), blob)
    if len(nums) != len(calls):
        sys.exit("autonames: %d SVC constants for %d declarations" % (len(nums), len(calls)))

    table = {}
    for n, (_, name, proto, header) in zip(nums, calls):
        if n in table and table[n][0] != name:
            sys.exit("autonames: SVC 0x%x is both %s and %s" % (n, table[n][0], name))
        table[n] = (name, proto, header)
    return table


def find_svc_wrappers(img, svcs):
    """Every `svc #N; bx lr` body, named from the SVC number."""
    out = []
    for i, (addr, mnem, ops) in enumerate(img.insns):
        if mnem != "svc" or i + 1 >= len(img.insns):
            continue
        naddr, nmnem, nops = img.insns[i + 1]
        if naddr != addr + 2 or nmnem != "bx" or nops.strip() != "lr":
            continue
        num = int(ops.strip().lstrip("#"), 0)
        if num not in svcs:
            continue
        name, proto, header = svcs[num]
        out.append({"address": addr, "name": name, "class": "svc",
                    "proto": proto, "header": "s140/headers/" + header,
                    "evidence": "svc #0x%x; bx lr at 0x%x" % (num, addr)})
    return out


# --------------------------------------------------- classes: libc / extlib

def match_reference(elf, streams, idx, threshold, propagate_threshold, min_insns):
    """Best image address per reference symbol, direct hits then call-graph hits.

    The propagation is match.py's: a reference `bl` carries a relocation naming
    its callee, so a confirmed caller hands over the callee's exact address and
    the instructions there only have to corroborate it.
    """
    refs = match.parse_reference(elf)
    best = {}
    for name, fn in refs.items():
        if len(fn["tokens"]) < min_insns or not IDENT.match(name):
            continue
        for cand in match.candidates(fn, streams, idx):
            r = match.score(fn, streams, cand)
            if r and (name not in best or r["score"] > best[name]["score"]):
                r.update(symbol=name, fn=fn, how="direct")
                best[name] = r
    accepted = {k: v for k, v in best.items() if v["score"] >= threshold}
    for _ in range(12):
        grew = False
        for v in list(accepted.values()):
            st = streams[v["stream"]]
            for i, callee in v["fn"]["calls"].items():
                tgt = st["targets"][v["pos"] + i] if v["pos"] + i < len(st["targets"]) else None
                if tgt is None or callee in accepted or callee not in refs:
                    continue
                if not IDENT.match(callee) or len(refs[callee]["tokens"]) < min_insns:
                    continue
                r = match.score_at(refs[callee], streams, tgt)
                if r and r["score"] >= propagate_threshold:
                    r.update(symbol=callee, fn=refs[callee],
                             how="propagated from " + v["symbol"])
                    accepted[callee] = r
                    grew = True
        if not grew:
            break
    return accepted


def library_names(img, sources, cls, threshold, propagate_threshold, min_insns):
    """Run the matcher over a list of (label, elf-or-archive) reference files."""
    streams = match.parse_image(os.path.join(SIM, "appl.bin"), APP_BASE)
    idx = match.build_kgram_index(streams)
    best = {}
    for label, path in sources:
        if not os.path.exists(path):
            print("  absent, skipped: %s" % path, file=sys.stderr)
            continue
        for name, r in match_reference(path, streams, idx, threshold,
                                       propagate_threshold, min_insns).items():
            if name not in best or r["score"] > best[name]["score"]:
                r["source"] = label
                best[name] = r
        print("  %-28s %d symbols" % (label, len(best)), file=sys.stderr)

    # One image address, one name: near-identical bodies (memcpy vs __aeabi_memcpy,
    # the printf family's shared cores) otherwise fight over the same entry.
    by_addr = collections.defaultdict(list)
    for r in best.values():
        by_addr[r["address"]].append(r)
    out = []
    for addr, group in sorted(by_addr.items()):
        group.sort(key=lambda r: (-r["score"], r["symbol"]))
        w = group[0]
        if len(group) > 1 and group[1]["score"] == w["score"]:
            # The reent syscall wrappers, and a few float helpers, have identical
            # bodies; a tie says the body does not pick between them, so there is
            # no name here to take.
            continue
        entry = {"address": addr, "name": w["symbol"], "class": cls,
                 "evidence": "%s, score %.2f over %d insns, %s"
                             % (w["source"], w["score"], w["insns"], w["how"])}
        if len(group) > 1:
            entry["also_matched"] = [r["symbol"] for r in group[1:]]
        out.append(entry)
    return out


DECL = re.compile(r"([A-Za-z_][\w \t\*\(\)]*?\b(\w+)\s*\([^;{)]*\))\s*;")


def prototypes(names, roots):
    """Each name's declaration, verbatim, from the headers that declare it."""
    wanted, protos = set(names), {}
    if not wanted:
        return protos
    pat = re.compile(r"\b(%s)\s*\(" % "|".join(re.escape(n) for n in wanted))
    for label, root in roots:
        for dirpath, _, files in os.walk(root):
            for f in sorted(files):
                if not f.endswith(".h"):
                    continue
                path = os.path.join(dirpath, f)
                try:
                    src = open(path, errors="ignore").read()
                except OSError:
                    continue
                if not pat.search(src):
                    continue
                src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
                src = re.sub(r"//[^\n]*", " ", src)
                for m in DECL.finditer(src):
                    name = m.group(2)
                    if name not in wanted or name in protos:
                        continue
                    decl = " ".join(m.group(1).split())
                    for kw in ("extern ", "static inline ", "static ", "_LIBC_ "):
                        if decl.startswith(kw):
                            decl = decl[len(kw):]
                    if "(" not in decl:
                        continue
                    protos[name] = {"proto": decl + ";",
                                    "header": "%s/%s" % (label, os.path.relpath(path, root))}
    return protos


# ------------------------------------------------------------- class: string

def string_names(img):
    """Functions that log their own name through wlog's first vararg.

    The image's logging convention is `wlog("[MODULE][%s] ...", __func__)`, so a
    bare snake_case identifier arriving in r1 at a wlog call is the name of the
    function making the call. Only identifiers that exactly one function uses
    this way are taken: a shared string is a value, not a name.
    """
    sites = []
    for i, (addr, mnem, ops) in enumerate(img.insns):
        if mnem not in ("bl", "bl.w"):
            continue
        t = re.search(r"0x([0-9a-f]+)", ops.split("@")[0])
        if not t or int(t.group(1), 16) not in WLOG_R0:
            continue
        # Walk back over the straight-line run that sets up the call.
        regs = {}
        for j in range(i - 1, max(-1, i - 14), -1):
            _, m2, o2 = img.insns[j]
            if m2 in ("bl", "bl.w", "b", "b.w", "bx", "pop"):
                break
            r = re.match(r"^(r\d+),", o2)
            if m2.startswith("ldr") and r and r.group(1) not in regs:
                s = img.pool_string(o2)
                if s is not None:
                    regs[r.group(1)] = s
        fmt, arg = regs.get("r0"), regs.get("r1")
        if fmt and arg and b"%s" in fmt and FUNC_IDENT.match(arg):
            sites.append((img.owner(addr), arg, fmt, addr))

    by_fn, by_ident = collections.defaultdict(set), collections.defaultdict(set)
    for fn, arg, _, _ in sites:
        by_fn[fn].add(arg)
        by_ident[arg].add(fn)
    out = []
    for fn, args in sorted(by_fn.items()):
        if fn is None or len(args) != 1:
            continue
        arg = next(iter(args))
        if len(by_ident[arg]) != 1:
            continue
        fmt, site = next((f, a) for o, g, f, a in sites if o == fn and g == arg)
        out.append({"address": fn, "name": arg.decode(), "class": "string",
                    "evidence": "wlog(%r, %r) at 0x%x"
                                % (fmt.decode().rstrip("\n"), arg.decode(), site)})
    return out


# ---------------------------------------------------- classes: wppcmd, shell

# The anchor hwa10.yaml already carries; the walk finds the table's real extent.
WPP_ANCHOR = 0x27994
TABLE_STRIDE = 12
# The WPP column is a protocol command number (the wpp crate's map tops out in
# the 2500s); the shell table's first column is a large opaque key, which is what
# separates the two tables where they abut.
WPP_ID_MAX = 0x8000

SHELL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def read_row(img, addr):
    """{id, handler, label} for a dispatch row, or None if this is not one."""
    key, handler, namep = img.word(addr), img.word(addr + 4), img.word(addr + 8)
    if None in (key, handler, namep):
        return None
    if not (img.base <= handler < img.end) or not handler & 1:
        return None
    label = img.string(namep, limit=80)
    if label is None:
        return None
    # The label is the line the command logs, so it carries the wlog decoration.
    text = re.sub(r"^\[[^\]]*\]\s*", "", label.decode()).strip()
    if not IDENT.match(text):
        return None
    return {"address": addr, "key": key, "handler": handler & ~1, "text": text}


def walk_table(img, anchor, accept):
    """Every row of the table the anchor sits in, extending both ways."""
    rows = []
    a = anchor
    while True:
        r = read_row(img, a - TABLE_STRIDE)
        if not r or not accept(r):
            break
        a -= TABLE_STRIDE
    while True:
        r = read_row(img, a)
        if not r or not accept(r):
            break
        rows.append(r)
        a += TABLE_STRIDE
    return rows


def dispatch_names(img, rows, cls, prefix, table_name, protocol):
    counts = collections.Counter(r["text"] for r in rows)
    out = []
    for r in rows:
        base = prefix + r["text"]
        # Two rows can carry the same label for different handlers (the workout
        # start/stop pairs); the handler address keeps those apart.
        name = base if counts[r["text"]] == 1 else "%s_0x%x" % (base, r["handler"])
        ev = "%s row 0x%x: key %d, label %r" % (table_name, r["address"], r["key"], r["text"])
        if r["key"] in protocol:
            ev += ", %s in wpp/src/commands.rs" % protocol[r["key"]]
        e = {"address": r["handler"], "name": name, "class": cls, "evidence": ev}
        if cls == "wppcmd":
            e["command"] = r["key"]
        out.append(e)
    return out


def wpp_command_names(img, protocol):
    rows = walk_table(img, WPP_ANCHOR, lambda r: r["key"] < WPP_ID_MAX)
    return dispatch_names(img, rows, "wppcmd", "", "wpp_cmd_table", protocol), rows


def shell_command_names(img):
    """The UART debug shell's command table, which abuts the WPP one.

    Its rows have the same shape but a large opaque key and a lowercase command
    word; the word is the shell command, so the handler is that command's.
    """
    def accept(r):
        return r["key"] >= WPP_ID_MAX and SHELL_NAME.match(r["text"])

    # The table is found, not assumed: the longest run of rows of this shape in
    # the image is it, and nothing else in the image comes close.
    best, addr = [], img.base
    while addr < img.base + 0x8000:
        r = read_row(img, addr)
        if r and accept(r):
            run = walk_table(img, addr, accept)
            if len(run) > len(best):
                best = run
            addr = run[-1]["address"] + TABLE_STRIDE
        else:
            addr += 4
    if len(best) < 20:
        return [], []
    return dispatch_names(img, best, "shell", "shell_cmd_", "shell_cmd_table", {}), best


CMD_CONST = re.compile(r'^\s*(\d+)\s*=>\s*Some\("([A-Za-z_][A-Za-z0-9_]*)"\)', re.M)


def wpp_protocol_constants():
    """{command number: constant} from the repo's own wpp crate."""
    path = os.path.join(os.path.dirname(SIM), "wpp", "src", "commands.rs")
    if not os.path.exists(path):
        return {}
    text = open(path, errors="ignore").read()
    return {int(m.group(1)): m.group(2) for m in CMD_CONST.finditer(text)}


# ------------------------------------------------------------------- output

def cross_check(entries):
    """Compare against the two hand-maintained maps; never overwrite either.

    Returns the agreements, the disagreements, and the entries that are safe to
    emit: an address hwa10.yaml already names is dropped, because the manifest is
    the source of truth for what gets linked and a second name for the same
    function is a finding to read, not an entry to generate.
    """
    hand = {}
    sym = os.path.join(SIM, "symbols.txt")
    for line in open(sym):
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("0x"):
            hand.setdefault(int(parts[0], 16), parts[1])
    import yaml
    manifest = yaml.safe_load(open(os.path.join(HERE, "hwa10.yaml")))
    in_manifest = {fn["address"] & ~1: fn["name"] for fn in manifest["functions"]}
    hand.update(in_manifest)
    agree, disagree, emit = [], [], []
    for e in entries:
        was = hand.get(e["address"])
        if was is not None:
            (agree if was == e["name"] else disagree).append((e, was))
        if e["address"] not in in_manifest:
            emit.append(e)
    return agree, disagree, emit


def write_yaml(path, entries, agree, disagree, stats):
    lines = [
        "# Generated by abi/autonames.py -- do not edit by hand.",
        "# Names that follow mechanically from the image plus a reference the",
        "# repo can point at: SoftDevice SVC numbers, matched library bodies, the",
        "# firmware's own __func__ logging and the WPP dispatch table's name",
        "# column. `evidence` is what produced the name; nothing here is a guess.",
        "",
        "meta:",
        "  image: appl.bin",
        "  app_base: 0x%x" % APP_BASE,
        "  softdevice_headers: s140 7.2.0 (nRF5 SDK 17.1.0)",
        "  toolchain: gcc-arm-none-eabi-9-2020-q2-update (SDK reference build)",
        "  libc_toolchain: %s, multilib %s" % (LIBC_TC_LABEL, MULTILIB),
        "  counts: {%s}" % ", ".join("%s: %d" % kv for kv in sorted(stats.items())),
        "  known_addresses: {agree: %d, disagree: %d}" % (len(agree), len(disagree)),
        "",
        "functions:",
    ]
    for e in sorted(entries, key=lambda e: e["address"]):
        lines.append("  - name: %s" % e["name"])
        lines.append("    address: 0x%x" % e["address"])
        lines.append("    class: %s" % e["class"])
        if "command" in e:
            lines.append("    command: %d" % e["command"])
        lines.append("    evidence: %s" % yaml_str(e["evidence"]))
        if e.get("also_matched"):
            lines.append("    also_matched: [%s]" % ", ".join(e["also_matched"]))
        if e.get("header"):
            lines.append("    header: %s" % e["header"])
        if e.get("proto"):
            lines.append("    proto: %s" % yaml_str(e["proto"]))
    lines += ["", "# Addresses symbols.txt or hwa10.yaml already names, and what this run",
              "# derived for them. A disagreement is a finding: one of the two is wrong."]
    lines.append("agrees_with_hand_map:")
    for e, was in sorted(agree, key=lambda x: x[0]["address"]):
        lines.append("  - {address: 0x%x, name: %s, class: %s}" % (e["address"], e["name"], e["class"]))
    lines.append("disagrees_with_hand_map:")
    for e, was in sorted(disagree, key=lambda x: x[0]["address"]):
        lines.append("  - {address: 0x%x, hand: %s, derived: %s, class: %s}"
                     % (e["address"], was, e["name"], e["class"]))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def yaml_str(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--dis", default=os.path.join(SIM, "out", "appl.dis"))
    ap.add_argument("--out", default=os.path.join(HERE, "autonames.yaml"))
    ap.add_argument("--classes", default="svc,libc,extlib,string,wppcmd,shell")
    ap.add_argument("--threshold", type=float, default=0.90)
    ap.add_argument("--propagate-threshold", type=float, default=0.60)
    ap.add_argument("--min-insns", type=int, default=6)
    args = ap.parse_args()

    if not os.path.exists(args.dis):
        sys.exit("autonames: %s missing (run renode-sim/mkdis.sh)" % args.dis)
    want = set(args.classes.split(","))
    img = Image(args.image, args.dis)
    entries, stats = [], {}

    if "svc" in want:
        found = find_svc_wrappers(img, softdevice_svcs())
        stats["svc"] = len(found)
        entries += found

    if "libc" in want:
        libgcc = ""
        if os.path.isdir(LIBGCC_GLOB):
            for v in sorted(os.listdir(LIBGCC_GLOB)):
                cand = os.path.join(LIBGCC_GLOB, v, MULTILIB, "libgcc.a")
                if os.path.exists(cand):
                    libgcc = cand
        sources = [("%s %s" % (LIBC_TC_LABEL, a), os.path.join(LIBDIR, a))
                   for a in LIBC_ARCHIVES]
        if libgcc:
            sources.append(("%s libgcc.a" % LIBC_TC_LABEL, libgcc))
        found = library_names(img, sources, "libc", args.threshold,
                              args.propagate_threshold, args.min_insns)
        protos = prototypes([e["name"] for e in found],
                            [("arm-none-eabi/include",
                              os.path.join(LIBC_TC, "arm-none-eabi/include"))])
        for e in found:
            e.update(protos.get(e["name"], {}))
        stats["libc"] = len(found)
        entries += found

    if "extlib" in want:
        sources = [(os.path.basename(p), p) for p in EXT_ARCHIVES]
        sources += [(v, os.path.join(ROOT, "build", v, "ref.elf")) for v in EXT_VARIANTS]
        found = library_names(img, sources, "extlib", args.threshold,
                              args.propagate_threshold, args.min_insns)
        protos = prototypes([e["name"] for e in found],
                            [("mbedtls-2.16.10", os.path.join(SDK, "external/mbedtls/include"))])
        for e in found:
            e.update(protos.get(e["name"], {}))
        stats["extlib"] = len(found)
        entries += found

    if "string" in want:
        found = string_names(img)
        stats["string"] = len(found)
        entries += found

    if "wppcmd" in want:
        found, rows = wpp_command_names(img, wpp_protocol_constants())
        stats["wppcmd"] = len(found)
        stats["wpp_table_rows"] = len(rows)
        entries += found

    if "shell" in want:
        found, rows = shell_command_names(img)
        stats["shell"] = len(found)
        stats["shell_table_rows"] = len(rows)
        entries += found

    # One address, one name, and one name, one address: the manifest cannot
    # carry either kind of duplicate, and a collision here is a bug in a class.
    by_addr, by_name, final = {}, {}, []
    for e in sorted(entries, key=lambda e: (e["address"], e["class"])):
        if e["address"] in by_addr:
            other = by_addr[e["address"]]
            if other["name"] != e["name"]:
                sys.exit("autonames: 0x%x is both %s (%s) and %s (%s)"
                         % (e["address"], other["name"], other["class"],
                            e["name"], e["class"]))
            continue
        if e["name"] in by_name:
            # SVCALL is a static naked stub, so a header used by two translation
            # units leaves two identical copies in the image. They are
            # interchangeable; the first address keeps the plain name so the
            # linker script can bind it, the rest say which copy they are.
            e = dict(e, name="%s_copy_0x%x" % (e["name"], e["address"]),
                     evidence=e["evidence"] + "; second copy of %s (0x%x)"
                              % (by_name[e["name"]]["name"], by_name[e["name"]]["address"]))
        by_addr[e["address"]] = e
        by_name[e["name"]] = e
        final.append(e)

    agree, disagree, emit = cross_check(final)
    write_yaml(args.out, emit, agree, disagree, stats)
    print("%s: %d names (%s); %d agree with the hand map, %d disagree"
          % (args.out, len(emit),
             ", ".join("%s %d" % kv for kv in sorted(stats.items())),
             len(agree), len(disagree)))


if __name__ == "__main__":
    main()
