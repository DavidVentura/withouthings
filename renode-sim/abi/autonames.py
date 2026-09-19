#!/usr/bin/env python3
"""Derive HWA10 image symbol names that follow mechanically from evidence.

    python3 abi/autonames.py                  # every class, writes abi/autonames.yaml
    python3 abi/autonames.py --classes svc    # one class

Fifteen classes, in order of certainty:

  svc      a `svc #N; bx lr` body is the SoftDevice call whose SVC number is N,
           and the S140 headers give the number and the exact prototype.
  libc     newlib libc and libgcc bodies, matched the way abi/match.py matches
           the SDK: the toolchain's own archives are disassembled member by
           member and their normalised instruction sequences searched for in
           the image.
  libm     the same against the math library alone. It is its own class because
           it is its own archive: a replacement group derived from the `libc`
           bodies pulls libm's archive code and tables into the link with them,
           and the library region has no room for both.
  extlib   the same against the other open-source libraries the image contains
           (mbedTLS built by abi/refbuild.sh, the SDK's prebuilt crypto archives).
  string   a function that passes a bare snake_case identifier as the first
           vararg of a wlog call whose format holds a %s is naming itself; the
           image's `[MODULE][%s] ...` lines are __func__ logging.
  wppcmd   the WPP dispatch table stores {id, handler, name} per row and the
           name is a C identifier, so each handler is named by its own row.
  shell    the UART debug shell's command table, which abuts the WPP one; its
           row is {name, run, --help} and names both of a command's entries.
  logtag   the same logging, with the format already expanded: a `[tag]` that is
           a snake_case C identifier rather than a module name is __func__.
  logcb    a log line that opens with an identifier ending _cb, _callback,
           _handler or _isr, which is a name no variable carries.
  bleevt   the BLE event dispatcher switches on the SoftDevice event id through
           a `tbh` table, so each case names its handler out of the S140 event
           enumerations (symbols.txt confirms four cases independently).
  slot     a function nothing calls is entered through the word that holds its
           address, so a declared table's pointer field names it by its row.
  accessor a body that is one load or one store through a named global and a
           return is that global's getter or setter.
  shared   a body every caller of which sits in one log-tag module belongs to
           that module, and is numbered by address within it.
  helper   a function only one named function calls is that function's private
           helper and takes its name with an index. The name may come from any
           rule or from the hand map, not just from this run.
  role     a body that hands a call a string the callee's own prototype says is
           a name has the role that string names: the two FreeRTOS creates take
           a task's entry point and its name together, so the body that makes
           the call is that task's creation site.
  wrapper  a straight-line body whose whole content is one call to a named
           function rebinds that call's arguments or keeps its result, and has
           no name but the callee's.
  bymodule the partition below, read downwards: a body that calls only named
           functions of one module, or reaches only named globals of one, is
           that module's whatever calls it, and so is a body all of whose
           callers this rule placed in it.

The same log tags give a module partition (which file a function came from),
which abi/out/ghidra/modules.json carries and every entry above records.

Nothing here guesses: every entry carries the evidence that produced it, and
an entry that contradicts a hand entry of abi/symbols.yaml is reported under
`disagrees_with_hand_map` rather than folded in.

autonames.yaml is the measurement and abi/symbols.yaml is the map: each rule
owns its class there and rewrites it, so the name and the address go to the map
and the prototype, the module partition and the readings stay here.
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
import callargs  # noqa: E402  (the forward reading of a call's own block)
import match  # noqa: E402  (same directory; the normaliser and matcher live there)
import libc_find  # noqa: E402  (the archive-side body search)
import shapes  # noqa: E402  (the declared tables and struct globals, from DWARF)
import symbols as symmap  # noqa: E402  (the address map these names go into)

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
ROOT = os.path.expanduser("~/ref-build")
SDK = os.path.join(ROOT, "sdk", "nRF5_SDK_17.1.0_ddde560")
GCC = os.path.join(ROOT, "tc", "arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi")
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
LIBC_ARCHIVES = ["libc_nano.a", "libc.a"]
LIBM_ARCHIVES = ["libm.a"]
# The prebuilt libm.a is a different build of the same source: the image's
# math is Withings' own newlib compiled with the image's flags, and the libm
# abi/refbuild.sh builds from that source reaches bodies the archive does not
# (sin, cos, exp, log, pow, sqrt and their kernels), so both are offered.
LIBM_VARIANTS = ["toolchain"]  # abi/refbuild.sh's newlib-nano-ll libm.a

# Prebuilt, so they can be matched without building anything.
EXT_ARCHIVES = [
    os.path.join(SDK, "external/nrf_cc310/lib/cortex-m4/hard-float/libnrf_cc310_0.9.13.a"),
    os.path.join(SDK, "external/nrf_oberon/lib/cortex-m4/hard-float/liboberon_3.0.8.a"),
    os.path.join(SDK, "external/nrf_oberon/lib/cortex-m4/hard-float/liboberon_mbedtls_3.0.8.a"),
]
# mbedTLS was built from source here while the WPPS TLS handshake was being
# read; it named nothing the prebuilt archives above do not, so abi/refbuild.sh
# no longer builds it and only the archives are offered.
EXT_VARIANTS = []

# Which class names an address when two reach it; earlier wins. See main().
CLASS_RANK = ["svc", "syscall", "libc", "libm", "extlib", "wppcmd", "shell", "wppobj", "string",
              "logtag", "logcb", "bleevt", "logline", "slot", "accessor",
              "vector", "shared", "helper", "role", "wrapper", "bymodule"]

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
        self._insn_at = [i[0] for i in self.insns]

    def body(self, start, end):
        """The instructions between two addresses, in order."""
        i = bisect.bisect_left(self._insn_at, start)
        out = []
        while i < len(self.insns) and self.insns[i][0] < end:
            out.append(self.insns[i])
            i += 1
        return out

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
        if e < 0 or e - o > limit or e - o < 1:
            return None
        s = self.data[o:e]
        # ESC because most of this image's log lines are ANSI-coloured, and the
        # high bytes because a few are UTF-8; requiring the whole run to decode
        # is what keeps arbitrary data from reading as a string.
        if not all(32 <= c < 127 or c in (9, 10, 13, 27) or c >= 0x80 for c in s):
            return None
        try:
            s.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return s

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


# ------------------------------------------------------------ class: syscall

# newlib's bare syscall stubs. Withings supplies these itself, so no reference
# archive carries them, and every one of them compiles to the same
# `errno = N; return -1` -- the same bytes as a dozen stubs libc_nano.a does
# carry. That is how 0x97fd0 matched the archive's `fcntl`: the body genuinely
# is fcntl's body, and is equally _fstat's, so a body match cannot choose and
# takes whichever name the archive happens to offer.
#
# The caller can choose. newlib pairs each stub with exactly one reentrant
# wrapper _X_r, whose shape is its own -- zero the errno cell, shuffle the
# arguments up by one, call the stub, and on -1 copy errno into the reentrancy
# struct the first argument points at -- and the relinked newlib binds each of
# these call sites to a stub by name. The wrapper address is therefore the
# evidence, and the errno constant in the body corroborates it where the two
# stubs a wrapper could belong to set different ones.
SYSCALL_STUBS = {
    0x97F26: ("_read", 0x9041C, "errno 5 (EIO)"),
    0x97F36: ("_lseek", 0x903F8, "errno 5 (EIO)"),
    0x97F46: ("_close", 0x903D8, "errno 9 (EBADF)"),
    0x97F56: ("_kill", 0x91EB4, "errno 134 (ENOTSUP)"),
    0x97F66: ("_exit", 0xA920E, "`udf #0` -- the stub cannot return"),
    0x97F68: ("_getpid", None, "`movs r0,#1; bx lr`, the stub's constant 1"),
    0x97FD0: ("_fstat", 0x91CD4, "errno 88 (ENOSYS)"),
    0x97FF4: ("_isatty", 0x91D1C, "returns 1 for fd 0..2 and 0 above"),
    0x4CA2C: ("_write", 0x90440, "errno 9 (EBADF) above fd 2"),
}


def syscall_stub_names(img, ex):
    """The nine newlib syscall stubs, named by the wrapper that calls them.

    The call site is looked up in the disassembly rather than in the export's
    call graph: _kill_r (0x91eb4) and _write_r (0x90440) are only reached by a
    tail branch, so they have no function start of their own and the export
    files their call to the stub under the body before them.
    """
    if not ex.ok:
        return []
    sites = collections.defaultdict(set)
    for i, (at, mnem, ops) in enumerate(img.insns):
        if mnem in ("bl", "bl.w", "b.w"):
            t = _call_target(ops)
            if t is not None:
                sites[t].add(at)
    out = []
    for addr, (name, wrapper, why) in sorted(SYSCALL_STUBS.items()):
        callers = sites.get(addr, set())
        if wrapper is not None and not any(wrapper <= c < wrapper + 0x40
                                           for c in callers):
            # The wrapper is the whole of the evidence, so losing it is a
            # refusal rather than a name taken on the strength of the note.
            print("autonames: %s: 0x%x does not call 0x%x (callers %s)"
                  % (name, wrapper, addr,
                     ", ".join("0x%x" % c for c in sorted(callers)) or "none"),
                  file=sys.stderr)
            continue
        ev = ("newlib syscall stub: %s; the body is an errno stub every such "
              "stub in libc_nano.a matches, so the archive's own name cannot "
              "be taken here" % why)
        if wrapper is not None:
            ev = ("called by the reentrant wrapper %s_r at 0x%x, which the "
                  "relinked newlib binds to %s; " % (name, wrapper, name)) + ev
        else:
            ev = "no caller in the image; " + ev
        out.append({"address": addr, "name": name, "class": "syscall",
                    "evidence": ev})
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


# -------------------------------------------------- the Ghidra partition

class Export:
    """The partition abi/ghidra/analyze.sh exports, as the call graph and the
    function boundaries the logging classes attribute their evidence to.

    The `bl`-target ownership Image gives is a fallback: it merges a function
    into the one before it whenever nothing calls the later one with a `bl`,
    which is how a tail-called or table-only handler loses its log lines to its
    neighbour. The export's function ranges are the real boundaries.
    """

    def __init__(self, outdir, prior=()):
        import json
        # The export is seeded from the last run of this script, so a name it
        # already carries may be one of ours; those addresses are still open to
        # derivation, or every class would name a function once and never again.
        self.prior = set(prior)
        self.outdir = outdir
        self.ok = os.path.isdir(outdir) and os.path.exists(os.path.join(outdir, "items.json"))
        if not self.ok:
            return
        items = json.load(open(os.path.join(outdir, "items.json")))
        refs = json.load(open(os.path.join(outdir, "references.json")))
        self.fns = {f["start"]: f for f in items["functions"]}
        self._starts, self._ends, self._fn = [], [], []
        for r in sorted(items["function_ranges"], key=lambda r: r["start"]):
            self._starts.append(r["start"])
            self._ends.append(r["end"])
            self._fn.append(r["function"])
        self.callers = collections.defaultdict(set)
        self.callees = collections.defaultdict(set)
        # How many branches a body takes that are not calls. A wrapper is a
        # straight line by definition, so this is what separates one from a
        # body that happens to reach a single function down one of its paths.
        self.branches = collections.Counter()
        for c in refs["calls"]:
            a, b = c["function"], c["to"]
            if c["kind"] == "jump" and a is not None:
                self.branches[a] += 1
            if c["kind"] != "call" or a == b or a not in self.fns or b not in self.fns:
                continue
            self.callers[b].add(a)
            self.callees[a].add(b)
        try:
            self.word_uses = json.load(open(os.path.join(outdir, "word_uses.json")))["uses"]
        except (OSError, KeyError):
            self.word_uses = []
        self.pool_reads = {p["site"]: p["target"] for p in refs["pool_reads"]}
        # The literal-pool words a body reads, by body: a global the code
        # touches is reached through one of them.
        self.pool_targets = collections.defaultdict(list)
        for p in refs["pool_reads"]:
            self.pool_targets[p["function"]].append(p["target"])
        # Which word holds a function's Thumb address. A function nothing calls
        # is reached through one of these or not at all.
        self.holders = collections.defaultdict(list)
        for w in refs["words"]:
            if w["class"] == "thumb_function_start":
                self.holders[w["item"]].append(w["addr"])

    def owner(self, addr):
        i = bisect.bisect_right(self._starts, addr) - 1
        if i < 0 or addr >= self._ends[i]:
            return None
        return self._fn[i]

    def unnamed(self, fn):
        """Whether the image, not a previous run of this script, names it.

        Ghidra's own placeholder, or an address the last run of this script
        derived a name for."""
        name = self.fns.get(fn, {}).get("name", "")
        return fn in self.prior or name.startswith(("FUN_", "thunk_FUN_"))

    def size(self, fn):
        return self.fns.get(fn, {}).get("bytes", 0)


# ------------------------------------------------- the wlog call sites

# fmt register per entry point, from symbols.txt: 0x50ca8 takes the format in
# r2 (a level and a tag precede it), the rest in r0.
WLOG_FMT_REG = {0x8F460: 0, 0x8F494: 0, 0x9B1C8: 0, 0x5E7A8: 0, 0x50CA8: 2}
# %-conversion, less the literal %%; the group is the conversion character.
SPEC = re.compile(r"%(?:[-+ #0]*)(?:\*|\d+)?(?:\.(?:\*|\d+))?(?:hh|h|ll|l|j|z|t|L)?"
                  r"([diouxXeEfgGaAcspn%])")
LOG_TAG = re.compile(r"^\[([^\]]{1,24})\]")


def wlog_sites(img, ex):
    """Every wlog call whose format string the straight-line setup resolves.

    One pass back over the run of instructions that sets up the call, recording
    the pool string each argument register was last loaded with; a prior call
    ends the walk because AAPCS lets it clobber the argument registers.

    What that walk cannot answer, abi/callargs.py's forward reading of the same
    block can: a register loaded before a `mov` chain, an argument the block
    computes from a pool word, and an argument stored into an outgoing stack
    slot. Where it finds a string the back-walk missed the string is taken;
    where it finds that the value is a field of a struct or a row of a table it
    is recorded as a shape and nothing is claimed about the value, because
    where a value comes from is a fact about the image and what it happens to
    be at that call is not.
    """
    sites = []
    for i, (addr, mnem, ops) in enumerate(img.insns):
        if mnem not in ("bl", "bl.w"):
            continue
        t = re.search(r"0x([0-9a-f]+)", ops.split("@")[0])
        if not t or int(t.group(1), 16) not in WLOG_FMT_REG:
            continue
        entry = int(t.group(1), 16)
        regs = {}
        for j in range(i - 1, max(-1, i - 24), -1):
            _, m2, o2 = img.insns[j]
            if m2 in ("bl", "bl.w", "blx", "b", "b.w", "bx", "pop"):
                break
            r = re.match(r"^(r\d+),", o2)
            if not r or r.group(1) in regs:
                continue
            regs[r.group(1)] = img.pool_string(o2) if m2.startswith("ldr") else None
        fmtreg = "r%d" % WLOG_FMT_REG[entry]
        fmt = regs.pop(fmtreg, None)
        fn = ex.owner(addr) if ex.ok else img.owner(addr)
        if fmt is None:
            fmt = hoisted_format(img, i, fmtreg, fn, addr)
        frame = callargs.resolve(img, img.insns, i)
        if fmt is None:
            fmt = callargs.string_of(img, frame.arg(WLOG_FMT_REG[entry]))
        shapes = {}
        for k in range(4):
            reg = "r%d" % k
            if reg == fmtreg or regs.get(reg) is not None:
                continue
            value = frame.arg(k)
            text = callargs.string_of(img, value)
            if text is None:
                shapes[reg] = {"kind": value.kind, "detail": value.text()}
            else:
                regs[reg] = text
        sites.append({"site": addr, "fn": fn,
                      "entry": entry, "fmt": fmt.decode("latin1") if fmt else None,
                      "args": regs, "shapes": shapes, "how": "calling sequence"})
    return sites


CALLEE_SAVED = frozenset("r4 r5 r6 r7 r8 r9 r10 r11".split())
WRITES = re.compile(r"^(r\d+),")


def hoisted_format(img, i, fmtreg, fn, site):
    """The format string of a call whose format register was loaded earlier.

    A logger call in a loop has its format hoisted: the calling sequence only
    does `mov r0, r7`, and the pool load that set r7 is outside the loop, past a
    branch the straight-line walk stops at. Following the `mov` chain to the
    callee-saved register it came from turns that into a question about the
    whole function, and a callee-saved register with exactly one writer in the
    function has that writer's value at every call in it, whatever the control
    flow between them.
    """
    want = fmtreg
    for j in range(i - 1, max(-1, i - 24), -1):
        _, mnem, ops = img.insns[j]
        m = WRITES.match(ops)
        if m and m.group(1) == want:
            if mnem != "mov":
                return None
            nxt = re.match(r"^r\d+,\s*(r\d+)$", ops.strip())
            if not nxt:
                return None
            want = nxt.group(1)
            continue
        if mnem in ("bl", "bl.w", "blx") and want not in CALLEE_SAVED:
            return None
        if mnem in ("b", "b.w", "bx", "pop") and want not in CALLEE_SAVED:
            return None
    if want == fmtreg or want not in CALLEE_SAVED or fn is None:
        return None
    writers = [ops for addr, mnem, ops in img.insns
               if fn <= addr < site and WRITES.match(ops)
               and WRITES.match(ops).group(1) == want]
    if len(writers) != 1:
        return None
    return img.pool_string(writers[0])


WLOG_USE = re.compile(r"^wlog_a(\d):0x([0-9a-f]+)$")


def wlog_arg_words(img, ex):
    """{(call site, argument index): {pool word: string}} from word_uses.py.

    The back-walk above sees only what the calling sequence itself loads, which
    leaves every site whose format or vararg arrives as a parameter, as a
    returned value or out of a struct field unresolved. word_uses.py records the
    argument slot of a logger call as a use of whatever value lands in it, and
    its fixpoint carries that use back along the argument, return and field
    edges to the literal-pool word the string was loaded from, which is the
    whole answer this needs.
    """
    reach = collections.defaultdict(dict)
    for u in ex.word_uses:
        text = img.string(img.word(u["addr"]) or 0, limit=200)
        if text is None:
            continue
        for use in u["uses"]:
            m = WLOG_USE.match(use)
            if m:
                reach[(int(m.group(2), 16), int(m.group(1)))][u["addr"]] = text
    return reach


def pool_loaders(ex):
    """{pool word: the functions that load it}, from the export's pool reads."""
    import json
    path = os.path.join(ex.outdir, "references.json")
    loaders = collections.defaultdict(set)
    for r in json.load(open(path))["pool_reads"]:
        loaders[r["target"]].add(r["function"])
    return loaders


def widen_sites(img, ex, sites, reach, loaders):
    """The sites the calling sequence could not resolve, resolved by the edges.

    A word that reaches a logger's format slot was loaded somewhere, and where
    exactly one function loads it that function is the one that logs the line --
    which is the honest attribution when the call itself is in a wrapper several
    modules share. A wrapper site therefore yields one record per calling
    function rather than one per site, and the varargs are paired with the
    format by the site they meet at and the function that loaded both.
    """
    out, seen = [], {(s["site"], s["fn"], s["fmt"]) for s in sites if s["fmt"]}
    for s in sites:
        base = 1 if WLOG_FMT_REG[s["entry"]] == 0 else 3
        if s["fmt"] is not None:
            # A resolved format with a vararg the sequence did not load: the
            # identifier came in as a parameter, and the same edges supply it.
            for k in range(base, 4):
                if s["args"].get("r%d" % k) is not None:
                    continue
                mine = [w for w in reach.get((s["site"], k), ())
                        if loaders.get(w) == {s["fn"]}]
                if len(mine) == 1:
                    s["args"]["r%d" % k] = reach[(s["site"], k)][mine[0]]
            continue
        by_loader = collections.defaultdict(dict)
        for w, text in reach.get((s["site"], WLOG_FMT_REG[s["entry"]]), {}).items():
            who = loaders.get(w)
            if who and len(who) == 1:
                by_loader[next(iter(who))][w] = text
        for fn, words in sorted(by_loader.items()):
            if len(words) != 1:
                continue
            text = next(iter(words.values())).decode("latin1")
            if (s["site"], fn, text) in seen:
                continue
            seen.add((s["site"], fn, text))
            args = {}
            for k in range(base, 4):
                mine = [w for w in reach.get((s["site"], k), ())
                        if loaders.get(w) == {fn}]
                if len(mine) == 1:
                    args["r%d" % k] = reach[(s["site"], k)][mine[0]]
            out.append({"site": s["site"], "fn": fn, "entry": s["entry"],
                        "fmt": text, "args": args, "shapes": {},
                        "how": "word_uses edges"})
    return out


def string_argument_shapes(sites):
    """What the block says about every `%s` argument of a resolved format.

    `string` is a value the image carries at that call; the other buckets are
    where the value comes from and not what it is, which is as far as anything
    static goes for an argument the caller was handed.
    """
    counts = collections.Counter()
    for s in sites:
        base = 1 if WLOG_FMT_REG[s["entry"]] == 0 else 3
        convs = [c for c in (m.group(1) for m in SPEC.finditer(s["fmt"]))
                 if c != "%"]
        for k, conv in enumerate(convs):
            reg = "r%d" % (base + k)
            if conv != "s" or base + k > 3:
                continue
            if s["args"].get(reg) is not None:
                counts["string"] += 1
            else:
                shape = s.get("shapes", {}).get(reg)
                counts[shape["kind"] if shape else "unknown"] += 1
    return counts


def log_tag(fmt):
    """The `[MODULE]` a format string opens with, normalised, or None."""
    m = LOG_TAG.match(fmt)
    if not m or "%" in m.group(1):
        return None
    tag = re.sub(r"[^A-Za-z0-9]+", "_", m.group(1)).strip("_").upper()
    return tag or None


def log_modules(ex, sites):
    """Which module each function belongs to, from the log tags, on a fixpoint.

    A function that logs under a tag belongs to that module. A function that
    logs under none belongs to the module of its callers when every caller is in
    one and the same module: a private helper is part of whatever calls it, and
    a function two modules share is claimed by neither. Nothing else propagates,
    so the partition is what the tags plus the call graph prove and no more.
    """
    tags = collections.defaultdict(collections.Counter)
    for s in sites:
        tag = log_tag(s["fmt"])
        if tag and s["fn"] is not None:
            tags[s["fn"]][tag] += 1
    mod, direct = {}, {}
    for fn, counts in tags.items():
        top = counts.most_common()
        # A function that logs under two tags equally is on a boundary; only a
        # strict majority claims it.
        if len(top) == 1 or top[0][1] > top[1][1]:
            mod[fn] = direct[fn] = top[0][0]
    while True:
        grew = 0
        for fn in ex.fns:
            if fn in mod:
                continue
            callers = ex.callers.get(fn)
            if not callers or not all(c in mod for c in callers):
                continue
            ms = {mod[c] for c in callers}
            if len(ms) == 1:
                mod[fn] = next(iter(ms))
                grew += 1
        if not grew:
            return mod, direct


def write_modules(path, ex, mod, direct):
    import json
    per = collections.defaultdict(lambda: {"functions": 0, "bytes": 0,
                                           "unnamed_functions": 0, "unnamed_bytes": 0})
    for fn, m in mod.items():
        row = per[m]
        row["functions"] += 1
        row["bytes"] += ex.size(fn)
        if ex.unnamed(fn):
            row["unnamed_functions"] += 1
            row["unnamed_bytes"] += ex.size(fn)
    doc = {"_generated": "by abi/autonames.py: the module each function logs under,"
                         " propagated to private helpers over the call graph",
           "modules": dict(sorted(per.items(), key=lambda kv: -kv[1]["bytes"])),
           "functions": {"0x%x" % fn: {"module": m, "evidence":
                                       "own log tag" if fn in direct else "every caller"}
                         for fn, m in sorted(mod.items())}}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=False)
    return per


# ------------------------------------------------------------- class: string

def string_names(img, ex, sites):
    """Functions that log their own name as a `%s` argument.

    The image's logging convention is `wlog("[MODULE][%s] ...", __func__)`, and
    the same string turns up further into the format as often as first, so the
    format's own conversions decide which register holds it: the nth `%s` is the
    nth vararg, which is r1 upwards (r3 upwards for the logger that takes its
    format in r2). Only identifiers that exactly one function uses this way are
    taken, and only from a function that uses exactly one: a shared string is a
    value, not a name.
    """
    cand, where = collections.defaultdict(set), {}
    for s in sites:
        base = 1 if WLOG_FMT_REG[s["entry"]] == 0 else 3
        varargs = [c for c in (m.group(1) for m in SPEC.finditer(s["fmt"])) if c != "%"]
        for k, conv in enumerate(varargs):
            if conv != "s" or base + k > 3 or s["fn"] is None:
                continue
            arg = s["args"].get("r%d" % (base + k))
            if arg is None or not FUNC_IDENT.match(arg):
                continue
            cand[s["fn"]].add(arg.decode())
            where.setdefault((s["fn"], arg.decode()), s)
    return _unique_names(cand, where, "string",
                         lambda s, n: "wlog(%r, %r) at 0x%x"
                                      % (s["fmt"].rstrip("\n"), n, s["site"]))


# ------------------------------------------------------------- class: logtag

def logtag_names(img, ex, sites):
    """Functions whose log tag is their own name rather than their module's.

    A handful of translation units write `wlog("[%s] ...", __func__)` with the
    format already expanded, so the tag itself is a C identifier of function
    shape. Distinguished from a module tag by that shape alone: module tags are
    upper case or spaced words, never snake_case.
    """
    cand, where = collections.defaultdict(set), {}
    for s in sites:
        m = LOG_TAG.match(s["fmt"])
        if not m or s["fn"] is None or not FUNC_IDENT.match(m.group(1).encode()):
            continue
        cand[s["fn"]].add(m.group(1))
        where.setdefault((s["fn"], m.group(1)), s)
    return _unique_names(cand, where, "logtag",
                         lambda s, n: "wlog(%r) at 0x%x, tag is a C identifier"
                                      % (s["fmt"].rstrip("\n"), s["site"]))


# -------------------------------------------------------------- class: logcb

# A leading identifier in a log line is as often a variable being printed
# ("[M] reset_reason = %d") as the function's own name, so only the suffixes
# that cannot name a variable are taken.
CALLBACK_SUFFIX = ("_cb", "_callback", "_handler", "_isr")
LEADING_IDENT = re.compile(r"^\[[^\]]{1,24}\]\s*([a-z][a-z0-9_]*)\b")


def logcb_names(img, ex, sites):
    """Callbacks that name themselves at the head of their own log line."""
    cand, where = collections.defaultdict(set), {}
    for s in sites:
        m = LEADING_IDENT.match(s["fmt"])
        if not m or s["fn"] is None:
            continue
        ident = m.group(1)
        if not ident.endswith(CALLBACK_SUFFIX) or not FUNC_IDENT.match(ident.encode()):
            continue
        cand[s["fn"]].add(ident)
        where.setdefault((s["fn"], ident), s)
    return _unique_names(cand, where, "logcb",
                         lambda s, n: "wlog(%r) at 0x%x, leading identifier is a callback name"
                                      % (s["fmt"].rstrip("\n"), s["site"]))


def _unique_names(cand, where, cls, evidence):
    """The candidates that are one function's name and no other function's."""
    by_ident = collections.defaultdict(set)
    for fn, idents in cand.items():
        for i in idents:
            by_ident[i].add(fn)
    out = []
    for fn, idents in sorted(cand.items()):
        if len(idents) != 1:
            continue
        name = next(iter(idents))
        if len(by_ident[name]) != 1:
            continue
        out.append({"address": fn, "name": name, "class": cls,
                    "evidence": evidence(where[(fn, name)], name)})
    return out


# ------------------------------------------------------------- class: bleevt

# `subs r3, #1; cmp r3, #N; bhi default; tbh [pc, r3, lsl #1]` -- the dispatcher
# switches on the SoftDevice event id, biased by one because id 0 is not an
# event. symbols.txt records the same table and four of its cases independently.
BLE_EVT_DISPATCH = 0x37594
BLE_EVT_ENUM_PREFIXES = ("BLE_EVT_", "BLE_GAP_EVT_", "BLE_GATTC_EVT_",
                         "BLE_GATTS_EVT_", "BLE_L2CAP_EVT_")


def ble_event_enums():
    """{event id: enum name} for the S140 BLE event enumerations.

    The enumerators are C enums over the per-module bases in ble_ranges.h, so
    they are read by compiling them with the SDK's own toolchain and reading the
    array back out rather than by parsing the headers.
    """
    if not os.path.isdir(SD_HEADERS) or not os.path.isdir(GCC):
        return {}
    names = set()
    for f in sorted(os.listdir(SD_HEADERS)):
        if not f.endswith(".h"):
            continue
        src = open(os.path.join(SD_HEADERS, f), errors="ignore").read()
        for m in re.finditer(r"\b(BLE_(?:GAP|GATTC|GATTS|L2CAP)?_?EVT_[A-Z0-9_]+)\b", src):
            names.add(m.group(1))
    # Not enumerators: sizes, range markers and the reason enum that shares the
    # BLE_GAP_EVT_ prefix without being an event id.
    names = sorted(n for n in names
                   if n.startswith(BLE_EVT_ENUM_PREFIXES)
                   and not n.endswith(("_BASE", "_LAST", "_LEN_MAX", "_PTR_ALIGNMENT",
                                       "_INVALID"))
                   and "_TERMINATED_REASON" not in n)
    inc = [os.path.join(SDK, p) for p in
           ("components/softdevice/s140/headers",
            "components/softdevice/s140/headers/nrf52",
            "modules/nrfx/mdk", "components/toolchain/cmsis/include")]
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        c = os.path.join(tmp, "evt.c")
        with open(c, "w") as f:
            f.write("#include \"ble.h\"\n#include \"ble_gap.h\"\n#include \"ble_gattc.h\"\n"
                    "#include \"ble_gatts.h\"\n#include \"ble_l2cap.h\"\n"
                    "const int vals[]={%s};\n" % ",".join(names))
        obj, binf = os.path.join(tmp, "evt.o"), os.path.join(tmp, "evt.bin")
        tool = os.path.join(GCC, "bin", "arm-none-eabi-")
        cmd = [tool + "gcc", "-mcpu=cortex-m4", "-mthumb", "-c", "-O0", "-DNRF52840_XXAA",
               "-o", obj, c] + sum((["-I", d] for d in inc), [])
        if subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
            return {}
        if subprocess.call([tool + "objcopy", "-O", "binary", "-j", ".rodata", obj, binf],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
            return {}
        vals = struct.unpack("<%di" % (os.path.getsize(binf) // 4), open(binf, "rb").read())
    out = {}
    for name, val in zip(names, vals):
        out.setdefault(val, name)
    return out


def thunk_target(img, idx, fn, depth=4):
    """Past a function that is one unconditional branch, to what it branches to."""
    while fn is not None and depth:
        i = idx.get(fn)
        if i is None:
            return fn
        _, mnem, ops = img.insns[i]
        if mnem not in ("b", "b.w"):
            return fn
        m = re.search(r"0x([0-9a-f]+)", ops.split("@")[0])
        if not m:
            return fn
        fn, depth = int(m.group(1), 16), depth - 1
    return fn


def ble_event_names(img, ex):
    """The per-event handlers the BLE event dispatcher's jump table reaches.

    The table is a Thumb `tbh` of halfword offsets, so each case lands inside the
    dispatcher; the handler is the call that case makes, through however many
    branch-only thunks the linker put between them. A case block that only logs,
    or whose call already has a name, yields nothing.
    """
    if not ex.ok:
        return []
    ids = ble_event_enums()
    if not ids:
        return []
    idx = {a: i for i, (a, _, _) in enumerate(img.insns)}
    # Locate the tbh from the dispatcher's own instructions rather than trusting
    # a constant: the table follows it and its bound gives the case count.
    i = idx.get(BLE_EVT_DISPATCH)
    if i is None:
        return []
    table = bound = None
    for j in range(i, i + 40):
        addr, mnem, ops = img.insns[j]
        if mnem == "cmp":
            m = re.search(r"#(0x[0-9a-f]+|\d+)", ops)
            if m:
                bound = int(m.group(1), 0)
        if mnem == "tbh" and "pc, r" in ops:
            table = addr + 4
            break
    if table is None or bound is None:
        return []
    targets = {}
    for k in range(bound + 1):
        off = struct.unpack("<H", img.data[table + 2 * k - img.base:
                                           table + 2 * k + 2 - img.base])[0]
        targets[k + 1] = table + 2 * off
    default = collections.Counter(targets.values()).most_common(1)[0][0]
    handlers, where = collections.defaultdict(set), {}
    for evt, block in sorted(targets.items()):
        if block == default or evt not in ids:
            continue
        j = idx.get(block)
        if j is None:
            continue
        callee = None
        for k in range(j, j + 40):
            _, mnem, ops = img.insns[k]
            if mnem in ("bl", "bl.w"):
                m = re.search(r"0x([0-9a-f]+)", ops.split("@")[0])
                callee = int(m.group(1), 16) if m else None
                break
            if mnem in ("pop", "bx", "b", "b.w"):
                break
        callee = thunk_target(img, idx, callee)
        if callee is None or callee not in ex.fns or not ex.unnamed(callee):
            continue
        name = "%s_handler" % ids[evt].lower()
        handlers[callee].add(name)
        where.setdefault((callee, name), (evt, block))
    out = []
    by_name = collections.defaultdict(set)
    for fn, names in handlers.items():
        for n in names:
            by_name[n].add(fn)
    for fn, names in sorted(handlers.items()):
        # One function serving two events is that function's name for neither.
        if len(names) != 1 or len(by_name[next(iter(names))]) != 1:
            continue
        name = next(iter(names))
        evt, block = where[(fn, name)]
        out.append({"address": fn, "name": name, "class": "bleevt",
                    "evidence": "ble_evt_dispatch (0x%x) jump table at 0x%x, case %d "
                                "(%s), block 0x%x calls it"
                                % (BLE_EVT_DISPATCH, table, evt,
                                   ids[evt], block)})
    return out


# ------------------------------------------------------------ class: logline

# A word that carries no information about which function this is.
LOGLINE_STOP = ("the", "a", "an", "to", "for", "of", "in", "on", "at", "as",
                "is", "was", "and", "or", "with", "from", "by", "this", "that",
                "s", "t", "d", "ll", "re")
LOGLINE_WORDS = 5
LOGLINE_MAX = 48


def logline_slug(fmt):
    """The C identifier a log line is, or None if the line does not make one.

    Everything in the name is in the string: the module tag, then the line's own
    words with the %-conversions removed, because a conversion is the value not
    the message. A line of fewer than two words left is a continuation fragment
    ("%02x", " | ") and names nothing.
    """
    tag = log_tag(fmt)
    body = SPEC.sub(" ", re.sub(r"^\[[^\]]*\]\s*", "", fmt)).replace("'", "")
    words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", body)
             if w and not w.isdigit()]
    while words and words[0] in LOGLINE_STOP:
        words.pop(0)
    words = words[:LOGLINE_WORDS]
    while words and words[-1] in LOGLINE_STOP:
        words.pop()
    if len(words) < 2:
        return None
    parts = ([tag.lower()] if tag else []) + words
    # "[factory_state] factory_state=%d" would otherwise say it twice.
    while len(parts) > 1 and parts[0] == parts[1]:
        parts.pop(0)
    name = "_".join(parts)
    if not re.match(r"^[a-z][a-z0-9_]*$", name) or len(name) > LOGLINE_MAX:
        return None
    return name


def logline_names(img, ex, sites):
    """Functions that log exactly one line, named by the line they log.

    The `string`, `logtag` and `logcb` classes take a name the firmware wrote as
    a name; this takes the milestone itself, which is what a function with one
    log line and no __func__ convention has instead. Only one distinct format
    per function counts -- a function that logs two things is doing two things
    and neither line names it -- and only a slug no other function produces.
    """
    if not ex.ok:
        return []
    fmts = collections.defaultdict(set)
    where = {}
    for s in sites:
        if s["fn"] is not None:
            fmts[s["fn"]].add(s["fmt"])
            where.setdefault((s["fn"], s["fmt"]), s)
    cand, by_name = {}, collections.defaultdict(set)
    for fn, seen in fmts.items():
        if len(seen) != 1 or not ex.unnamed(fn):
            continue
        fmt = next(iter(seen))
        name = logline_slug(fmt)
        if name:
            cand[fn] = (name, fmt)
            by_name[name].add(fn)
    return [{"address": fn, "name": name, "class": "logline",
             "evidence": "the only line 0x%x logs, wlog(%r) at 0x%x"
                         % (fn, fmt.rstrip("\n"), where[(fn, fmt)]["site"])}
            for fn, (name, fmt) in sorted(cand.items()) if len(by_name[name]) == 1]


# ------------------------------------------------------------- class: wppobj

# The WPP wire object is {u16 type, u16 length, data}. 0x9a954 writes a
# big-endian u16 into the frame under construction (0x9a966 reads one back,
# 0x9a982 and 0x9a9a0 are the 32-bit pair), so a function whose first two calls
# write a constant type and a constant length is that type's encoder, and the
# field writes that follow are its layout.
WPP_WRITE_BE16 = 0x9A954
# 0x504b4 walks a TLV stream comparing each object's type against its own r3
# and calls the parser it is handed on the stack when they match, so the fifth
# argument of a call with a literal type is that type's parser.
WPP_OBJ_PARSE = 0x504B4

OBJ_DECL = re.compile(
    r"impl WppObjectCodec for (\w+)\s*\{\s*"
    r"const TYPE_ID: u16 = (\d+);\s*"
    r"const TYPE_NAME: &'static str = \"(\w+)\";\s*"
    r"const CLASS_NAME: &'static str = \"(\w+)\";\s*"
    r"const FIXED_DATA_SIZE: Option<usize> = (Some\((\d+)\)|None);")


def wpp_objects():
    """{type id: (class name, TYPE_ constant, fixed data size or None)}.

    From wpp/src/objects.rs, this repo's own decoder for the protocol: it is the
    reference the names are read off, the way the S140 headers are for the SVCs.
    """
    path = os.path.join(os.path.dirname(SIM), "wpp", "src", "objects.rs")
    if not os.path.exists(path):
        return {}
    out = {}
    for m in OBJ_DECL.finditer(open(path, errors="ignore").read()):
        out[int(m.group(2))] = (m.group(1), m.group(3),
                                int(m.group(6)) if m.group(6) else None)
    return out


def _imm_before(img, i, reg, back=16):
    """The literal `reg` was last set to before instruction i, or None."""
    for j in range(i - 1, max(-1, i - back), -1):
        _, mnem, ops = img.insns[j]
        m = re.match(r"^%s,\s*#(0x[0-9a-f]+|\d+)$" % reg, ops)
        if m and mnem.startswith("mov"):
            return int(m.group(1), 0)
        if re.match(r"^%s[,\s]" % reg, ops) or mnem in ("bl", "bl.w", "b", "b.w",
                                                        "pop", "bx"):
            return None
    return None


def _call_target(ops):
    m = re.search(r"0x([0-9a-f]+)", ops.split("@")[0])
    return int(m.group(1), 16) if m else None


def wpp_object_names(img, ex):
    """The per-object encoders and parsers of the WPP wire format.

    An encoder opens with the object's type and its data length, both literal,
    written through the frame's big-endian u16 primitive. A parser is the
    callback the generic type-matching walk is handed alongside a literal type.
    Both name the function after the Rust type in wpp/src/objects.rs, which is
    the same protocol read from the other side.
    """
    objs = wpp_objects()
    if not objs or not ex.ok:
        return [], {}
    sizes, out, checked = {}, [], set()
    idx = {a: k for k, (a, _, _) in enumerate(img.insns)}
    for fn in sorted(ex.fns):
        k = idx.get(fn)
        if k is None:
            continue
        calls = []
        for j in range(k, min(k + 16, len(img.insns))):
            addr, mnem, ops = img.insns[j]
            if ex.owner(addr) != fn:
                break
            if mnem in ("bl", "bl.w", "b.w", "b"):
                calls.append((j, _call_target(ops)))
                if len(calls) == 2:
                    break
        if len(calls) != 2 or any(t != WPP_WRITE_BE16 for _, t in calls):
            continue
        oid = _imm_before(img, calls[0][0], "r1")
        size = _imm_before(img, calls[1][0], "r1")
        if oid not in objs:
            continue
        cls, type_name, fixed = objs[oid]
        ev = ("writes type %d (%s) then length %s through the frame's u16 "
              "writer (0x%x) as its first two calls" %
              (oid, type_name, size, WPP_WRITE_BE16))
        if fixed is not None and size is not None:
            checked.add(cls)
            if fixed != size:
                sizes[cls] = (oid, size, fixed)
                ev += "; wpp/src/objects.rs FIXED_DATA_SIZE is %d" % fixed
        out.append({"address": fn, "name": "wpp_obj_%s_encode" % cls,
                    "class": "wppobj", "evidence": ev})
    parsers = collections.defaultdict(set)
    where = {}
    for j, (addr, mnem, ops) in enumerate(img.insns):
        if mnem not in ("bl", "bl.w") or _call_target(ops) != WPP_OBJ_PARSE:
            continue
        oid = _imm_before(img, j, "r3")
        if oid not in objs:
            continue
        cb = None
        for m in range(j - 1, max(-1, j - 16), -1):
            _, m2, o2 = img.insns[m]
            if m2 in ("bl", "bl.w", "b", "b.w", "pop"):
                break
            r = re.match(r"^(r\d+),\s*\[sp\]$", o2)
            if not m2.startswith("str") or not r:
                continue
            for n in range(m - 1, max(-1, m - 16), -1):
                _, m3, o3 = img.insns[n]
                if m3.startswith("ldr") and o3.startswith(r.group(1) + ",") \
                        and "[pc" in o3:
                    p = re.search(r"@ 0x([0-9a-f]+)", o3)
                    cb = img.word(int(p.group(1), 16) + img.base) if p else None
                    break
                if re.match(r"^%s[,\s]" % r.group(1), o3):
                    break
            break
        if cb and cb & 1 and img.base <= cb < img.end:
            parsers[cb & ~1].add(oid)
            where.setdefault((cb & ~1, oid), addr)
    for fn, ids in sorted(parsers.items()):
        if len(ids) != 1 or fn not in ex.fns:
            continue
        oid = next(iter(ids))
        cls, type_name, _ = objs[oid]
        out.append({"address": fn, "name": "wpp_obj_%s_parse" % cls,
                    "class": "wppobj",
                    "evidence": "the parser 0x%x hands the object walk (0x%x) "
                                "for type %d (%s)"
                                % (where[(fn, oid)], WPP_OBJ_PARSE, oid, type_name)})
    return out, {"mismatch": sizes, "crate_types": len(objs), "size_checked": len(checked),
                 "encode": sum(1 for e in out if e["name"].endswith("_encode")),
                 "parse": sum(1 for e in out if e["name"].endswith("_parse")),
                 "image_types": len({e["name"].split("wpp_obj_")[1].rsplit("_", 1)[0]
                                     for e in out})}


# ------------------------------------------------------------- class: helper

HELPER_MAX = 56
# Two steps down from a name the image itself supplies. A longer chain is
# arithmetic rather than evidence: the third index says only that something
# under something under a named function exists.
HELPER_DEPTH = 2

# A name that stands for where a body sits in the call graph rather than for
# what it does. One may not be the stem of another.
NICKNAMES = ("helper", "shared", "prose")


def map_seed(smap):
    """Every name in the map strong enough to lend itself to a helper.

    A helper's name is its caller's, so the seed is every settled or hand name,
    whatever rule wrote it -- a `match`, a `kernel` create argument, a `wuiview`
    slot, a `store` descriptor, a `codec` type, an `svc` number, a `vendor`
    entry. The nicknames are not on the list, or one would breed another past
    the depth limit, and neither is `prose`, which names an address without
    saying what is there.
    """
    return dict((s.address, s.name) for s in smap.of_kind("function")
                if s.klass not in NICKNAMES
                and (s.klass == symmap.HAND or symmap.outranks(s.klass, "helper")))


def helper_names(ex, named, blocked=(), reserved=()):
    """The private helpers under a named function.

    A function one named function calls that nothing else in the image calls is
    part of that function's implementation and nothing else's; it has no name of
    its own, so it takes its caller's with an index, numbered by address so the
    name is stable across runs. The chain is followed down as long as each step
    is still called by exactly one function, and stops where the derived name
    would be longer than a name is useful.

    `blocked` are addresses the map already names under a class this rule may
    not displace, and `reserved` the names it already spends elsewhere: the map
    holds one name per address and one address per name, so a nickname that
    would take either is not derived at all rather than refused at the write.
    """
    if not ex.ok:
        return []
    out, taken = [], dict(named)
    spent = set(reserved) | set(named.values())
    frontier, depth = dict(named), 0
    while frontier and depth < HELPER_DEPTH:
        depth += 1
        nxt = {}
        for owner, oname in sorted(frontier.items()):
            if len(oname) > HELPER_MAX:
                continue
            private = sorted(f for f in ex.callees.get(owner, ())
                             if ex.unnamed(f) and ex.callers.get(f) == {owner}
                             and f not in taken and f not in blocked)
            for n, fn in enumerate(private, 1):
                name = "%s__%d" % (oname, n)
                if len(name) > HELPER_MAX or name in spent:
                    continue
                spent.add(name)
                taken[fn] = nxt[fn] = name
                out.append({"address": fn, "name": name, "class": "helper",
                            "evidence": "only 0x%x (%s) calls 0x%x; %d bytes"
                                        % (owner, oname, fn, ex.size(fn))})
        frontier = nxt
    return out


# --------------------------------------------------------------- class: slot

def declared_slots(smap, types):
    """Every pointer field of a declared table row or struct global, by address.

    A function nothing calls is entered through the word that holds its Thumb
    address, so that word is what names it -- but only where the word is a slot
    the headers declare, because a bare pointer in an untyped run says where the
    function is and nothing about what it is for.
    """
    out = {}
    for tab in types.typed_regions(smap):
        for index, row in enumerate(tab.rows()):
            for field in tab.row.fields:
                if field.kind != shapes.POINTER:
                    continue
                name = ("%s_%s" % (tab.name, field.name) if tab.count == 1
                        else "%s_%d_%s" % (tab.name, index, field.name))
                out[row + field.offset] = (name, tab.name, index, field.name)
    return out


def slot_names(ex, smap, types, blocked=()):
    """The handlers a declared table reaches that nothing calls.

    No call site names these: they are vtable and registry entries, so the
    evidence is the slot itself. A function held by two slots of one table is
    named by the first; one held by slots of two different tables is refused,
    because the two claims are about different things.
    """
    if not ex.ok:
        return []
    slots = declared_slots(smap, types)
    out = []
    for fn in sorted(ex.fns):
        if not ex.unnamed(fn) or ex.callers.get(fn) or fn in blocked:
            continue
        held = sorted(w for w in ex.holders.get(fn, ()) if w in slots)
        if not held or len({slots[w][1] for w in held}) != 1:
            continue
        name, table, index, field = slots[held[0]]
        out.append({"address": fn, "name": name, "class": "slot",
                    "evidence": "0x%x is the %s slot of %s row %d (word 0x%x);"
                                " nothing calls it"
                                % (fn, field, table, index, held[0])})
    return out


# ------------------------------------------------------------- class: vector

# The Cortex-M vector table at the app's base: word 0 is the initial stack
# pointer and the 63 words after it are handler entries. The count is the
# nRF52840's, 16 system exceptions plus 48 IRQs, and it is the number
# abi/ghidra/seed_symbols.py lays the same table out with.
VECTOR_WORDS = 64


def vector_names(img, blocked=()):
    """The exception handlers nothing else names, named by their vector number.

    A handler is entered by the core and by nothing else, so no call site and
    no table row will ever name one; the word that holds it is the whole of the
    evidence and the number of that word is the whole of what it says. The
    spelling is the one abi/ghidra/seed_symbols.py already gives them, so the
    export and the map agree on these addresses instead of the analysis
    carrying a name the map never heard of.
    """
    out = []
    for i in range(1, VECTOR_WORDS):
        word = img.word(APP_BASE + 4 * i)
        if not word or not word & 1 or img.word(word & ~1) is None:
            continue
        addr = word & ~1
        if addr in blocked:
            continue
        out.append({"address": addr,
                    "name": "Reset_Handler" if i == 1
                            else "vector_%d_handler" % i,
                    "class": "vector",
                    "evidence": "word %d of the vector table at 0x%x holds"
                                " 0x%x" % (i, APP_BASE, word)})
    return out


# ----------------------------------------------------------- class: accessor

# `ldr r3,[pc,#N]; ldr r0,[r3{,#K}]; bx lr` and its store twin: the whole body
# is one access to one global, so the global names it.
ACCESS = re.compile(r"^r0, \[r3(?:, #(0x[0-9a-f]+))?\]$")
LOAD = ("ldr", "ldr.w", "ldrb", "ldrb.w", "ldrh", "ldrh.w",
        "ldrsb", "ldrsb.w", "ldrsh", "ldrsh.w")
STORE = ("str", "str.w", "strb", "strb.w", "strh", "strh.w")


def library_ranges():
    """The spans of the image abi/match.py's body matches bracket.

    A body inside one of them is library code whose name comes from the
    reference build, whatever the app's globals around it are called:
    `xTaskGetTickCount` and `xTaskGetTickCountFromISR` are two identical loads
    of FreeRTOS's own `xTickCount` and are not two accessors of it.
    """
    import yaml
    path = os.path.join(HERE, "matches.yaml")
    if not os.path.exists(path):
        return []
    doc = yaml.safe_load(open(path))
    return [(r["start"], r["end"]) for r in doc.get("library_ranges") or []]


def accessor_names(img, ex, smap, types, blocked=()):
    """A body that is one load or one store through a named global.

    The role comes from the global, never from the body: an identical three
    instructions sit at nineteen addresses in this image, so the pool word is
    the only thing that tells them apart. A global the map does not name, or an
    offset its declaration gives no field, leaves the body unnamed.
    """
    if not ex.ok:
        return []
    library = library_ranges()
    globals_ = dict((s.address, s.name) for s in smap.of_kind("global", "table"))
    fields = {}
    for region in types.typed_regions(smap):
        if region.count != 1:
            continue
        for field in region.row.fields:
            fields[(region.address, field.offset)] = field.name
    out = []
    for fn in sorted(ex.fns):
        if not ex.unnamed(fn) or fn in blocked:
            continue
        if any(lo <= fn < hi for lo, hi in library):
            continue
        body = img.body(fn, fn + ex.size(fn))
        if len(body) != 3 or body[2][1] != "bx" or body[2][2] != "lr":
            continue
        if body[0][1] != "ldr" or not body[0][2].startswith("r3, [pc"):
            continue
        access = ACCESS.match(body[1][2])
        pool = ex.pool_reads.get(body[0][0])
        if access is None or pool is None:
            continue
        base = img.word(pool)
        name = globals_.get(base)
        if name is None:
            continue
        offset = int(access.group(1), 16) if access.group(1) else 0
        if offset:
            field = fields.get((base, offset))
            if field is None:
                continue
            name = "%s_%s" % (name, field)
        verb = ("get" if body[1][1] in LOAD else
                "set" if body[1][1] in STORE else None)
        if verb is None:
            continue
        out.append({"address": fn, "name": "%s_%s" % (name, verb),
                    "class": "accessor",
                    "evidence": "the body is one %s through %s (pool word 0x%x"
                                " = 0x%x) and a return"
                                % (body[1][1], name, pool, base)})
    return out


# ------------------------------------------------------------- class: shared

def shared_names(ex, named, modules, blocked=()):
    """A body every caller of which sits in one module belongs to that module.

    Several named callers give no one caller to take a name from, but where they
    all come from the same log-tag module the body is that module's, so it is
    named for the module and numbered by address. Callers spanning two modules
    are refused: what the body is shared between is exactly what the name would
    have to say.
    """
    if not ex.ok:
        return []
    per = collections.defaultdict(list)
    for fn in sorted(ex.fns):
        callers = ex.callers.get(fn, set())
        if not ex.unnamed(fn) or len(callers) < 2 or fn in blocked:
            continue
        if not all(c in named for c in callers):
            continue
        where = {modules.get(c) for c in callers}
        if len(where) != 1 or None in where:
            continue
        per[where.pop()].append(fn)
    out = []
    for module, fns in sorted(per.items()):
        for n, fn in enumerate(sorted(fns), 1):
            callers = sorted(named[c] for c in ex.callers[fn])
            out.append({"address": fn, "name": "%s__shared_%d"
                                               % (module.lower(), n),
                        "class": "shared", "module": module,
                        "evidence": "every caller of 0x%x is in %s: %s"
                                    % (fn, module, ", ".join(callers))})
    return out


# ------------------------------------- classes: role, wrapper and bymodule

# The classes whose names come from a reference build or a SoftDevice header
# rather than from this image: a body that calls only these has called into a
# library, which belongs to no module of Withings' own partition.
LIBRARY_CLASSES = ("libc", "libm", "extlib", "svc", "syscall", "match",
                   "kernel", "vendor")
# The classes the three rules below write themselves. A name one of them left
# in the map is this run's own output rather than something the image
# established, so none of them reads one back: a rule that took its own last
# answer as evidence would derive one more name every time the map is
# rewritten instead of settling.
CALLEE_SIDE_CLASSES = ("role", "wrapper", "bymodule")
# A wrapper is a call and the instructions that set it up; past this many the
# body is doing something of its own that the callee's name would not say.
WRAPPER_MAX = 12
# The longest derived name that is still a name rather than a sentence; the
# same bound the helper nicknames are held to.
DERIVED_MAX = HELPER_MAX


def call_sites(img, ex):
    """Every `bl` in the image as (enclosing body, instruction index, target)."""
    out = collections.defaultdict(list)
    for i, (addr, mnem, ops) in enumerate(img.insns):
        if mnem not in ("bl", "bl.w"):
            continue
        fn = ex.owner(addr)
        target = _call_target(ops)
        if fn is None or target is None:
            continue
        out[fn].append((i, target))
    return out


def in_library(fn, library):
    return any(lo <= fn < hi for lo, hi in library)


# FreeRTOS's two task creates, and which argument is the entry point and which
# the name. Both are on record from the kernel headers, which is what makes the
# string a role rather than a datum: `abi/kernel_objects.py` reads the same two
# calls to name the task's entry function, its stack and its control block.
TASK_CREATE_ARGS = {"xTaskCreate": (0, 1), "xTaskCreateStatic": (0, 1)}


def role_names(img, ex, smap, blocked=(), sites=None):
    """A body whose role is the string it hands a call whose role is known.

    A pool string that is not a log format and goes into a call is a datum
    about the call, and where the call's own role says what the datum is for,
    it says what the body is for: the body that hands FreeRTOS a task entry
    point and the task's name is that task's creation site, so it is
    `<task>_create` after the entry function the map already names.

    The refusal is the whole of the discipline here. A string a call is handed
    is a role only where the callee's prototype says the argument is a name:
    the `snprintf` unit strings, `memchr`'s conversion sets and `setenv`'s
    variable are data the call consumes, and none of them names its caller. A
    body that creates two tasks names neither, because the name would have to
    choose between them.
    """
    if not ex.ok:
        return []
    library = library_ranges()
    named = dict((s.address, s.name) for s in smap.of_kind("function"))
    creates = {named[a]: TASK_CREATE_ARGS[named[a]] for a in named
               if named[a] in TASK_CREATE_ARGS}
    sites = sites if sites is not None else call_sites(img, ex)
    out = []
    for fn in sorted(ex.fns):
        if not ex.unnamed(fn) or fn in blocked or in_library(fn, library):
            continue
        made = []
        for i, target in sites.get(fn, ()):
            call = named.get(target)
            if call not in creates:
                continue
            entry_arg, name_arg = creates[call]
            frame = callargs.resolve(img, img.insns, i)
            text = callargs.string_of(img, frame.arg(name_arg), limit=32)
            entry = frame.arg(entry_arg)
            if text is None or not isinstance(entry, (callargs.Imm, callargs.Pool)):
                continue
            made.append((call, text.decode("utf-8", "replace"),
                         named.get(entry.value & ~1)))
        if len(made) != 1:
            continue
        call, text, entry = made[0]
        if entry is None:
            continue
        name = "%s_create" % entry
        if len(name) > DERIVED_MAX:
            continue
        out.append({"address": fn, "name": name, "class": "role",
                    "evidence": "0x%x hands %s the entry %s and the name '%s'"
                                % (fn, call, entry, text)})
    return out


def _stored_global(img, ex, smap, body, call_at):
    """The named global a body stores the call's result into, or None.

    `str r0, [r3]` after the call with `r3` loaded from the pool is the shape:
    the call made an object and the body is where the image keeps it.
    """
    globals_ = dict((s.address, s.name) for s in smap.of_kind("global", "table"))
    bases = {}
    for addr, mnem, ops in body:
        if mnem == "ldr" and "[pc" in ops:
            reg = ops.split(",")[0]
            pool = ex.pool_reads.get(addr)
            if pool is not None:
                bases[reg] = img.word(pool)
        if addr <= call_at or mnem not in STORE:
            continue
        m = re.match(r"^r0, \[(r\d+)\]$", ops)
        if m and bases.get(m.group(1)) in globals_:
            return globals_[bases[m.group(1)]]
    return None


def wrapper_names(img, ex, smap, blocked=(), sites=None):
    """A body that is one call to a named function and nothing else.

    A straight line with a single `bl` in it does not compute: it rebinds the
    arguments, or it keeps the result. Its name has to come from the call,
    because the call is all there is, and the convention this rule writes -- no
    earlier one exists in the map, which carries two hand-written `_wrap` names
    and no `_and_store` -- is `<callee>_wrap`, or `<callee>_and_store_<global>`
    where the body keeps the result in a global the map names. Where several
    bodies wrap the same function the plain name goes to the first by address
    and the rest carry their index, since nothing in the image tells them apart.

    A nickname callee is refused: `<caller>__1_wrap` says only that two bodies
    nobody has named sit next to each other.
    """
    if not ex.ok:
        return []
    library = library_ranges()
    named = dict((s.address, s) for s in smap.of_kind("function"))
    sites = sites if sites is not None else call_sites(img, ex)
    found = []
    for fn in sorted(ex.fns):
        if not ex.unnamed(fn) or fn in blocked or in_library(fn, library):
            continue
        callees = ex.callees.get(fn, set())
        if len(callees) != 1 or ex.branches.get(fn):
            continue
        callee = next(iter(callees))
        target = named.get(callee)
        if target is None or target.klass in NICKNAMES + CALLEE_SIDE_CLASSES:
            continue
        body = img.body(fn, fn + ex.size(fn))
        at = [a for a, mnem, _ in body if mnem in ("bl", "bl.w")]
        if len(body) > WRAPPER_MAX or len(at) != 1:
            continue
        held = _stored_global(img, ex, smap, body, at[0])
        found.append((fn, target.name, held, len(body)))
    per = collections.Counter(name for _, name, held, _ in found
                              if held is None)
    seen = collections.Counter()
    out = []
    for fn, callee, held, insns in found:
        if held is not None:
            name = "%s_and_store_%s" % (callee, held)
            why = "and keeps the result in %s" % held
        else:
            seen[callee] += 1
            name = callee + "_wrap" if per[callee] == 1 else \
                "%s_wrap_%d" % (callee, seen[callee])
            why = "and returns"
        if len(name) > DERIVED_MAX:
            continue
        out.append({"address": fn, "name": name, "class": "wrapper",
                    "evidence": "0x%x is %d instructions with one call, to %s,"
                                " %s" % (fn, insns, callee, why)})
    return out


def _module_of_callees(ex, modules, fn, named):
    """The one module every non-library function a body calls belongs to."""
    callees = sorted(ex.callees.get(fn, ()))
    if not callees:
        return None, None
    own = []
    for c in callees:
        symbol = named.get(c)
        if symbol is None or symbol.klass in CALLEE_SIDE_CLASSES:
            return None, None          # an unnamed callee says nothing yet
        if symbol.klass not in LIBRARY_CLASSES:
            own.append(symbol)
    if not own:
        return None, None              # every call went into a library
    where = {modules.get(s.address) for s in own}
    if len(where) != 1 or None in where:
        return None, None
    return where.pop(), "calls only %s" % ", ".join(sorted(s.name for s in own))


def _module_of_globals(img, ex, smap, fn):
    """The one module every named global a body reaches belongs to."""
    globals_ = dict((s.address, s) for s in smap.of_kind("global", "table")
                    if s.module)
    touched = {}
    for target in ex.pool_targets.get(fn, ()):
        word = img.word(target)
        if word is None:
            continue
        symbol = globals_.get(word)
        if symbol is not None:
            touched[symbol.name] = symbol.module
    if not touched:
        return None, None
    where = {m.upper() for m in touched.values()}
    if len(where) != 1:
        return None, None
    return where.pop(), "reaches only %s" % ", ".join(sorted(touched))


def bymodule_names(img, ex, smap, modules, blocked=()):
    """A body its own callees and globals place in one module, and its helpers.

    The partition abi/out/ghidra/modules.json carries reads the call graph
    upwards: a body that logs under a tag is that module's, and so is one all
    of whose callers are. This rule reads it downwards. A body that calls only
    named functions of one module, or reaches only named globals of one, is
    part of that module's implementation whatever its callers are, which is
    what the bodies with callers in two modules have been waiting for. The
    module is all the evidence establishes, so the name says the module and the
    rule that found it, and numbers by address within the module.

    One step of propagation follows: a body a settled body of this rule calls
    privately is in the same module, at depth 1 only. Chaining further would be
    deriving a nickname from a nickname, which is what the helper rule already
    refuses to do.

    Callees spanning two modules are refused, and so are callees that are all
    library: a `memcpy` and a `xQueueSend` place a body in FreeRTOS's code, not
    in any module of Withings'.
    """
    if not ex.ok:
        return []
    library = library_ranges()
    named = dict((s.address, s) for s in smap.of_kind("function"))
    settled, evidence, contested = {}, {}, []
    for fn in sorted(ex.fns):
        if not ex.unnamed(fn) or fn in blocked or in_library(fn, library):
            continue
        module, why = _module_of_callees(ex, modules, fn, named)
        if module is None:
            module, why = _module_of_globals(img, ex, smap, fn)
        if module is None:
            continue
        module = module.upper()
        # The partition reads the call graph the other way, so where it
        # already places the body the two readings are about the same thing
        # and have to agree: a body the tags put in one module whose callees
        # and globals are another's is a boundary, and neither module names it.
        placed = modules.get(fn)
        if placed is not None and placed.upper() != module:
            contested.append((fn, placed.upper(), module))
            continue
        settled[fn], evidence[fn] = module, why
    direct = dict(settled)
    for fn in sorted(ex.fns):
        if fn in settled or not ex.unnamed(fn) or fn in blocked:
            continue
        if in_library(fn, library):
            continue
        callers = ex.callers.get(fn, set())
        if not callers or not all(c in direct for c in callers):
            continue
        where = {direct[c] for c in callers}
        if len(where) != 1:
            continue
        settled[fn] = where.pop()
        evidence[fn] = ("every caller is a body this rule placed in %s: %s"
                        % (settled[fn], ", ".join("0x%x" % c
                                                  for c in sorted(callers))))
    per = collections.defaultdict(list)
    for fn, module in settled.items():
        per[module].append(fn)
    out = []
    for module, fns in sorted(per.items()):
        for n, fn in enumerate(sorted(fns), 1):
            name = "%s__by_callees_%d" % (module.lower(), n)
            if len(name) > DERIVED_MAX:
                continue
            out.append({"address": fn, "name": name, "class": "bymodule",
                        "module": module,
                        "evidence": "0x%x %s" % (fn, evidence[fn])})
    return out, contested

# --------------------------------------------------------------- prototypes

# What abi/ghidra/word_uses.py saw done with a value, and what that makes it.
POINTER_USES = ("load_base", "store_base", "call", "field")
INTEGER_USES = ("compare", "scaled", "stride")


def derived_prototypes(ex, entries):
    """A prototype for a derived name, where the argument uses decide every one.

    word_uses.py runs its data flow once with each function's argument registers
    as seeds, so a parameter's own use summary is on record: a dereferenced
    argument is a pointer, one only compared or scaled is an integer. The
    prototype is emitted only when the parameters used run from the first
    without a gap and each is unambiguously one or the other; a parameter both
    dereferenced and compared is a tagged pointer or a union, and is refused.

    The return type is not derivable this way and is declared uint32_t, which is
    the direction AAPCS forgives: a caller that ignores r0 is correct either
    way, while declaring void a function that returns would throw the value
    away. Every such prototype says so in proto_derived.
    """
    if not ex.ok or not ex.word_uses:
        return 0
    params = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    for u in ex.word_uses:
        for c in u.get("callees", ()):
            part = c.split(":")
            if len(part) != 3 or part[0] != "param":
                continue
            fn, i = int(part[1], 16), int(part[2], 16)
            for use, n in u["uses"].items():
                params[fn][i][use.split("+")[0].split("-")[0].split(":")[0]] += n
    n = 0
    for e in entries:
        seen = params.get(e["address"])
        if not seen or e.get("proto"):
            continue
        idx = sorted(seen)
        if idx != list(range(len(idx))) or len(idx) > 4:
            continue
        args = []
        for i in idx:
            uses = seen[i]
            ptr = any(uses.get(u) for u in POINTER_USES)
            num = any(uses.get(u) for u in INTEGER_USES)
            if ptr == num:
                args = None
                break
            args.append("void *" if ptr else "uint32_t")
        if not args:
            continue
        e["proto"] = "uint32_t %s(%s);" % (e["name"], ", ".join(args))
        e["proto_derived"] = ("abi/ghidra/word_uses.py argument uses: %s; return type "
                              "not derived, declared uint32_t"
                              % "; ".join("arg%d %s" % (i, "dereferenced" if a == "void *"
                                                        else "compared only")
                                          for i, a in zip(idx, args)))
        n += 1
    return n


# ---------------------------------------------------- classes: wppcmd, shell

# The anchor the address map already carries; the walk finds the table's real extent.
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


def read_shell_row(img, addr):
    """{name, run, help} for a shell row, or None if this is not one.

    The shell's row is not the WPP one: 0x5a224's walk compares `[r5]` against
    the typed word with strcmp, calls `[r5+4]` with (argc, argv) and `[r5+8]`
    when an argument is "--help", so the columns are {name, run, help}.
    """
    namep, run, help_ = img.word(addr), img.word(addr + 4), img.word(addr + 8)
    if None in (namep, run, help_):
        return None
    for fn in (run, help_):
        if not (img.base <= fn < img.end) or not fn & 1:
            return None
    label = img.string(namep, limit=80)
    if label is None or not SHELL_NAME.match(label.decode(errors="replace")):
        return None
    return {"address": addr, "text": label.decode(),
            "run": run & ~1, "help": help_ & ~1}


def walk_shell_table(img, anchor):
    """Every shell row of the table the anchor sits in, extending both ways."""
    a = anchor
    while read_shell_row(img, a - TABLE_STRIDE):
        a -= TABLE_STRIDE
    rows = []
    while True:
        r = read_shell_row(img, a)
        if not r:
            break
        rows.append(r)
        a += TABLE_STRIDE
    return rows


def shell_command_names(img):
    """The UART debug shell's command table, which abuts the WPP one.

    Its rows carry the command word and the command's two entry points, so each
    row names both. The table is found, not assumed: the longest run of rows of
    this shape in the image is it, and nothing else comes close.
    """
    best, addr = [], img.base
    while addr < img.base + 0x8000:
        if read_shell_row(img, addr):
            run = walk_shell_table(img, addr)
            if len(run) > len(best):
                best = run
            addr = run[-1]["address"] + TABLE_STRIDE
        else:
            addr += 4
    if len(best) < 20:
        return [], []
    counts = collections.Counter(r["text"] for r in best)
    if counts.most_common(1)[0][1] != 1:
        sys.exit("autonames: shell command %r appears twice"
                 % counts.most_common(1)[0][0])
    cand = []
    for r in best:
        ev = "shell_cmd_table row 0x%x: command %r" % (r["address"], r["text"])
        cand.append((r["run"], "shell_cmd_" + r["text"], ev + ", run slot (+4)"))
        cand.append((r["help"], "shell_help_" + r["text"], ev + ", --help slot (+8)"))
    # Several rows share one entry point (help and ls, exit and factory_reset):
    # a shared body is not any one command's, so it goes unnamed here.
    uses = collections.Counter(a for a, _, _ in cand)
    shared = {a for a, n in uses.items() if n > 1}
    return [{"address": a, "name": n, "class": "shell", "evidence": e}
            for a, n, e in cand if a not in shared], best


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
    """Compare against the hand half of the address map; never overwrite it.

    Returns the agreements, the disagreements, and the entries that are safe to
    emit: an address a hand entry or a codec entry already names is dropped,
    because a hand entry is the source of truth for what gets linked and a second name for the same
    function is a finding to read, not an entry to generate. A prose entry names
    an address without saying what kind of thing is there, so it is compared
    against but does not stop a derivation.
    """
    smap = symmap.load()
    # abi/protocol.py owns the codec bodies, and its reading of them is the
    # stronger one, so they are compared against here and not emitted.
    owned = {s.address: s.name
             for s in smap.of_class("codec") if s.kind == "function"}
    by_hand = {s.address: s.name
               for s in smap.of_class(symmap.HAND) if s.kind == "function"}
    hand = {s.address: s.name for s in smap.of_class("prose")}
    hand.update(owned)
    hand.update(by_hand)
    agree, disagree, emit = [], [], []
    for e in entries:
        was = hand.get(e["address"])
        if was is not None:
            (agree if was == e["name"] else disagree).append((e, was))
        if e["address"] not in owned:
            emit.append(e)
    return agree, disagree, emit


def map_entries(entries):
    """Every derived name as an abi/symbols.yaml row.

    The map carries the name, the address, the rule that produced it and the
    evidence; the prototype, the module partition and the also_named readings
    stay in abi/autonames.yaml, which is the measurement and not the map.
    """
    rows = []
    for e in sorted(entries, key=lambda e: e["address"]):
        row = {"address": e["address"], "name": e["name"], "kind": "function",
               "class": e["class"]}
        # The module a derivation was made in is part of what it established,
        # and a rule that reads the callee side needs it on the callee's entry
        # rather than in a second file beside the map.
        if e.get("module"):
            row["module"] = e["module"]
        if e.get("evidence"):
            row["evidence"] = " ".join(str(e["evidence"]).split())
        rows.append(row)
    return rows


def write_yaml(path, entries, agree, disagree, stats):
    lines = [
        "# Generated by abi/autonames.py -- do not edit by hand.",
        "# Names that follow mechanically from the image plus a reference the",
        "# repo can point at: SoftDevice SVC numbers, matched library bodies, the",
        "# firmware's own __func__ logging and the WPP dispatch table's name",
        "# column. `evidence` is what produced the name; nothing here is a guess.",
        "# `module` is which log tag's module the function belongs to, from the",
        "# partition abi/out/ghidra/modules.json carries.",
        "",
        "meta:",
        "  image: appl.bin",
        "  app_base: 0x%x" % APP_BASE,
        "  softdevice_headers: s140 7.2.0 (nRF5 SDK 17.1.0)",
        "  toolchain: arm-gnu-toolchain-13.2.Rel1 (abi/refbuild.sh)",
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
        if e.get("module"):
            lines.append("    module: %s" % e["module"])
        if e.get("also_named"):
            lines.append("    also_named: [%s]" % ", ".join(e["also_named"]))
        if e.get("also_matched"):
            lines.append("    also_matched: [%s]" % ", ".join(e["also_matched"]))
        if e.get("header"):
            lines.append("    header: %s" % e["header"])
        if e.get("proto"):
            lines.append("    proto: %s" % yaml_str(e["proto"]))
        if e.get("proto_derived"):
            lines.append("    proto_derived: %s" % yaml_str(e["proto_derived"]))
    lines += ["", "# Addresses abi/symbols.yaml already names by hand, and what this run",
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


# Every rule this script runs, as the class each writes into abi/symbols.yaml.
DEFAULT_CLASSES = ("svc,syscall,libc,libm,extlib,string,logtag,logcb,wppcmd,"
                   "shell,bleevt,wppobj,logline,slot,accessor,vector,shared,"
                   "helper,role,wrapper,bymodule")


# The map classes a derivation owns. A function the seed carries under one of
# them is named by a rule and not by the image, so the rules may re-derive it;
# anything else in the seed is a hand or a body-match name and is left alone.
DERIVED_CLASSES = frozenset(DEFAULT_CLASSES.split(",")) | {"codec"}


def prior_addresses(export, classes=None):
    """The addresses a derivation named in the run that seeded the export.

    Read from the export's own seed.json rather than from autonames.yaml, so it
    is the seed the partition was actually built with; taking it from the file
    this run is about to overwrite would make the answer depend on run order.
    The seed records each name's map class, so the classes a rule owns are what
    says a seeded name came from a rule: without this the second analysis run
    after a name reaches the map sees its own output as the image's own naming,
    stops deriving it, and the rewrite then takes it out of the map.
    """
    import json
    path = os.path.join(export, "seed.json")
    if not os.path.exists(path):
        return set()
    doc = json.load(open(path))
    classes = DERIVED_CLASSES if classes is None else set(classes)
    return {fn["address"] for fn in doc.get("functions", ())
            if fn.get("source") in classes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--dis", default=os.path.join(SIM, "out", "appl.dis"))
    ap.add_argument("--out", default=os.path.join(HERE, "autonames.yaml"))
    ap.add_argument("--classes", default=DEFAULT_CLASSES)
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"),
                    help="abi/ghidra/analyze.sh's export; the partition and call graph")
    ap.add_argument("--modules", default=os.path.join(HERE, "out", "ghidra", "modules.json"))
    ap.add_argument("--libc-build", default="newlib-nano-ll",
                    help="the abi/refbuild.sh newlib whose archives the"
                         " archive-side body search walks")
    ap.add_argument("--threshold", type=float, default=0.90)
    ap.add_argument("--propagate-threshold", type=float, default=0.60)
    ap.add_argument("--min-insns", type=int, default=6)
    args = ap.parse_args()

    if not os.path.exists(args.dis):
        sys.exit("autonames: %s missing (run renode-sim/mkdis.sh)" % args.dis)
    want = set(args.classes.split(","))
    img = Image(args.image, args.dis)
    ex = Export(args.export, prior_addresses(args.export, want))
    if not ex.ok:
        print("autonames: no export under %s; the classes that need the partition "
              "and the call graph are skipped" % args.export, file=sys.stderr)
    all_sites = wlog_sites(img, ex)
    sites = [s for s in all_sites if s["fmt"] is not None]
    stats = {"wlog_call_sites": len(all_sites), "wlog_sites": len(sites)}
    if ex.ok and ex.word_uses:
        widened = widen_sites(img, ex, all_sites, wlog_arg_words(img, ex),
                              pool_loaders(ex))
        stats["wlog_sites_edges"] = len(widened)
        sites += widened
    for shape, n in string_argument_shapes(sites).items():
        stats["wlog_string_arg_" + shape] = n
    entries = []
    modules, direct = (log_modules(ex, sites) if ex.ok else ({}, {}))
    if ex.ok:
        per = write_modules(args.modules, ex, modules, direct)
        stats["modules"] = len(per)
        stats["module_functions"] = len(modules)

    if "svc" in want:
        found = find_svc_wrappers(img, softdevice_svcs())
        stats["svc"] = len(found)
        entries += found

    if "syscall" in want:
        found = syscall_stub_names(img, ex)
        stats["syscall"] = len(found)
        entries += found

    # libc and libm are matched the same way against different archives, and the
    # archive is what decides the class: `libm` is the one a link may leave out.
    for cls in ("libc", "libm"):
        if cls not in want:
            continue
        if cls == "libc":
            sources = [("%s %s" % (LIBC_TC_LABEL, a), os.path.join(LIBDIR, a))
                       for a in LIBC_ARCHIVES]
            libgcc = ""
            if os.path.isdir(LIBGCC_GLOB):
                for v in sorted(os.listdir(LIBGCC_GLOB)):
                    cand = os.path.join(LIBGCC_GLOB, v, MULTILIB, "libgcc.a")
                    if os.path.exists(cand):
                        libgcc = cand
            if libgcc:
                sources.append(("%s libgcc.a" % LIBC_TC_LABEL, libgcc))
        else:
            sources = [("%s %s" % (LIBC_TC_LABEL, a), os.path.join(LIBDIR, a))
                       for a in LIBM_ARCHIVES]
            sources += [("newlib libm (%s)" % v,
                         os.path.join(ROOT, "build", v, "ref.elf"))
                        for v in LIBM_VARIANTS]
        found = library_names(img, sources, cls, args.threshold,
                              args.propagate_threshold, args.min_insns)
        # The matcher only ever asks where a reference symbol went, so it names
        # nothing the k-grams miss and nothing shorter than min_insns. The
        # archive-side search asks the image the opposite question -- for every
        # body the built archives define, is it here byte for byte -- and that
        # reaches the closure the relink has to bring its own copies of. Byte
        # evidence beats normalised evidence, so where the two disagree about
        # where a symbol lives the search wins and the matcher's entry goes.
        bodies = libc_find.entries(argparse.Namespace(
            build=args.libc_build, root=ROOT, scratch="/tmp/libc_find",
            export=args.export), cls)
        moved = {e["name"] for e in bodies}
        at = {e["address"] for e in bodies}
        found = [e for e in found
                 if e["address"] in at or e["name"] not in moved]
        found += [e for e in bodies if e["address"] not in
                  {f["address"] for f in found}]
        # The bytes and the score both say what a body looks like; neither says
        # where its calls go. Every entry is held to the call graph, the
        # partition and the shape before it may name an address.
        found, refused = libc_find.screen(argparse.Namespace(
            build=args.libc_build, root=ROOT, scratch="/tmp/libc_find",
            export=args.export), found)
        stats[cls + "_refused"] = len(refused)
        for e, why in refused:
            print("  refused %-24s 0x%05x  %s" % (e["name"], e["address"], why),
                  file=sys.stderr)
        protos = prototypes([e["name"] for e in found],
                            [("arm-none-eabi/include",
                              os.path.join(LIBC_TC, "arm-none-eabi/include"))])
        for e in found:
            e.update(protos.get(e["name"], {}))
        stats[cls] = len(found)
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

    for cls, fn in (("string", string_names), ("logtag", logtag_names),
                    ("logcb", logcb_names)):
        if cls not in want:
            continue
        found = fn(img, ex, sites)
        stats[cls] = len(found)
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

    if "bleevt" in want:
        found = ble_event_names(img, ex)
        stats["bleevt"] = len(found)
        entries += found

    if "wppobj" in want:
        found, info = wpp_object_names(img, ex)
        stats["wppobj"] = len(found)
        for k in ("encode", "parse", "image_types", "crate_types", "size_checked"):
            stats["wppobj_" + k] = info[k]
        stats["wppobj_length_mismatch"] = len(info["mismatch"])
        for cls, (oid, got, want_) in sorted(info["mismatch"].items()):
            print("autonames: %s (type %d) encodes %d data bytes, "
                  "wpp/src/objects.rs says %d" % (cls, oid, got, want_),
                  file=sys.stderr)
        entries += found

    if "logline" in want:
        found = logline_names(img, ex, sites)
        stats["logline"] = len(found)
        entries += found

    # The map is the other half of the image's naming: a rule this run does not
    # own named the sole caller, the table row or the global that the rules
    # below take their names from, so the seed is the map's settled names with
    # this run's own derivations over them.
    smap = symmap.load()
    types = shapes.load() if want & {"slot", "accessor"} else None
    seeded = map_seed(smap)
    # What the map holds that this run does not own, and so may not take: the
    # classes in `want` are this run's own output from the last one and are
    # about to be rewritten, which is what makes a second run a fixed point.
    held = {s.address for s in smap.of_kind("function") if s.klass not in want}
    reserved = {s.name for s in smap.symbols if s.klass not in want}
    # The four rules below all read the call graph rather than the body, so
    # they run last and over what the rules above have not already named.
    taken = held | {e["address"] for e in entries}

    if "slot" in want:
        found = [e for e in slot_names(ex, smap, types, blocked=taken)
                 if e["name"] not in reserved]
        stats["slot"] = len(found)
        entries += found
        taken |= {e["address"] for e in found}

    if "vector" in want:
        # Every address the map holds, whatever kind it holds it as: five of
        # the handlers are `prose` labels rather than functions, and a vector
        # number is not a better name than the one a hand wrote against the
        # line the handler logs.
        found = [e for e in vector_names(
            img, blocked=taken | {s.address for s in smap.symbols})
                 if e["name"] not in reserved]
        stats["vector"] = len(found)
        entries += found
        taken |= {e["address"] for e in found}

    if "accessor" in want:
        found = [e for e in accessor_names(img, ex, smap, types, blocked=taken)
                 if e["name"] not in reserved]
        stats["accessor"] = len(found)
        entries += found
        taken |= {e["address"] for e in found}

    if "shared" in want:
        named = dict(seeded)
        named.update((e["address"], e["name"]) for e in entries)
        found = shared_names(ex, named, modules, blocked=taken)
        stats["shared"] = len(found)
        entries += found
        taken |= {e["address"] for e in found}

    if "helper" in want:
        # A helper is named under whatever names its sole caller, wherever that
        # name came from: seeding only from this run's own output left every
        # body under a `match`, `kernel`, `wuiview`, `store` or hand name
        # nameless.
        named = dict(seeded)
        named.update((e["address"], e["name"]) for e in entries)
        found = helper_names(ex, named, blocked=taken,
                             reserved=reserved | {e["name"] for e in entries})
        stats["helper"] = len(found)
        entries += found
        taken |= {e["address"] for e in found}

    # The three rules below read the callee side: what a body calls, what it
    # reaches and what it hands a call, rather than what calls it. They run
    # last because each of them is weaker than every rule above, and each takes
    # only addresses the rules above left.
    sites = call_sites(img, ex) if ex.ok and want & {"role", "wrapper"} else {}
    for cls, rule in (("role", role_names), ("wrapper", wrapper_names)):
        if cls not in want:
            continue
        found = [e for e in rule(img, ex, smap, blocked=taken, sites=sites)
                 if e["name"] not in reserved]
        stats[cls] = len(found)
        entries += found
        taken |= {e["address"] for e in found}

    if "bymodule" in want:
        found, contested = bymodule_names(img, ex, smap, modules, blocked=taken)
        found = [e for e in found if e["name"] not in reserved]
        stats["bymodule"] = len(found)
        stats["bymodule_contested"] = len(contested)
        for pair, n in sorted(collections.Counter(
                (placed, seen) for _, placed, seen in contested).items()):
            print("autonames: %d bodies the tags put in %s call and read only"
                  " %s's; neither module names them" % (n, pair[0], pair[1]),
                  file=sys.stderr)
        entries += found

    # One address, one name, and one name, one address: the manifest cannot
    # carry either kind of duplicate.
    #
    # Two classes reaching the same address with different names is not a bug in
    # either: a dispatch table labels a handler with the protocol command it
    # serves, and the handler's own __func__ is the C symbol Withings wrote, so
    # WPP_CMD_INACTIVITY_CFG_SET and process_cmd_inactivity_cfg_set are both
    # right. The table row wins, because the rest of the repo (roots.yaml,
    # prunes.yaml, reach.py's per-command costs) addresses commands by it, and
    # the other name is kept on the entry rather than thrown away.
    by_addr, by_name, final = {}, {}, []
    for e in sorted(entries, key=lambda e: (e["address"], CLASS_RANK.index(e["class"]))):
        if e["address"] in by_addr:
            other = by_addr[e["address"]]
            if other["name"] != e["name"]:
                other.setdefault("also_named", []).append("%s (%s)" % (e["name"], e["class"]))
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

    for e in final:
        if e["address"] in modules:
            # A rule that derived the module derived its name from it, so the
            # partition fills in the entries that have none rather than
            # replacing what a rule established.
            e.setdefault("module", modules[e["address"]])
    stats["prototypes_derived"] = derived_prototypes(ex, final)
    agree, disagree, emit = cross_check(final)
    write_yaml(args.out, emit, agree, disagree, stats)
    print("%s: %d names (%s); %d agree with the hand map, %d disagree"
          % (args.out, len(emit),
             ", ".join("%s %d" % kv for kv in sorted(stats.items())),
             len(agree), len(disagree)))

    # The record above is the measurement; the map is where a name goes. Each
    # rule owns its class there and rewrites it, so a rule that stops producing
    # a name takes it out of the map too, and a name that collides with what the
    # map already carries is a refusal rather than a second entry.
    try:
        # The rules that settle an address rather than read it: an archive
        # body found where the call graph says it is, a `svc #N` whose number
        # is the SoftDevice call, and a dispatch-table row that names its own
        # handler. The rest -- a log tag, a caller's nickname -- are readings,
        # and a hand reading may stand beside them.
        settled = set(e["address"] for e in emit if e["class"] in symmap.SETTLED)
        added = symmap.load().rewrite(map_entries(emit),
                                      set(e["class"] for e in emit),
                                      verified=settled)
    except symmap.Refusal as err:
        sys.exit("abi/autonames.py: abi/symbols.yaml: %s" % err)
    print("wrote %d derived entries to abi/symbols.yaml" % added)


if __name__ == "__main__":
    main()
