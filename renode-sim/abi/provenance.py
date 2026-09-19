#!/usr/bin/env python3
"""What a body is called with, and the name that follows from it.

    python3 abi/provenance.py                 # the outcomes, as a report

996 of the image's unnamed bodies call nothing at all and 786 of those have
callers, so nothing about them can be read off the call graph below them: they
are the bottom of it. Nor do their callers hand them a name -- a measurement of
the first argument at every call into them found no caller passing a named
global straight in r0. What 690 of those calls do is `add r0, rN, #off` or
`ldr r0, [rN, #k]` off a context the caller keeps in a callee-saved register,
and that register was loaded once in the whole function from a literal-pool
word that names a declared struct. The type the body operates on is therefore
one hop back, and the hop is short.

This walks it. For every call into an unnamed body each argument register is
resolved backwards inside the caller to one of five things:

  global    a named global plus a constant displacement, through the `add`,
            `adds`, `add.w` and `ldr rN,[rM,#k]` chains a calling sequence uses
            and through the callee-saved registers the function loads exactly
            once, which is how abi/autonames.py's `hoisted_format` reasons
            about a format string hoisted out of a loop.
  field     a load off such a base: the value is the field rather than its
            address, and the base is still known.
  param     the caller's own argument register, untouched. The question then
            moves up to the caller's callers, depth-limited, and is only
            answered where every one of them answers it the same way.
  literal   an immediate or a pool word that is not a global.
  unknown   everything else, which is a finding and not a failure.

A body every call into which passes `&<global>` or `&<global>.<field>` in r0
is named for that global. Where the callers say nothing, the body's own
literal pool is asked the same question: a body whose pool holds the address
of exactly one global the map names, and which dereferences it, is about that
object whoever calls it -- which is the reading that reaches the bodies taking
no pointer at all, and the one that answers most often once the rules above
have taken what they can.

Either way the verb is the body's own: `<global>_<field>_<verb>` or
`<global>_<verb>`, with the verbs the map already spells (`get` 45 entries,
`set` 33, `reset` 25, `update` 16, `check` 13, `read` 12, and `copy`, which is
the one this adds). They are read off the body's loads and stores through the
pointer in abi/ghidra/word_uses.py's vocabulary -- a `load_base` is a read, a
`store_base` a write, a `field+d:w` a displacement -- measured here rather than
taken from its export, because word_uses.json publishes a use summary per pool
word and per memory cell and a parameter is neither.

Three refusals carry the discipline. Call sites that disagree about which
global the body is handed name nothing, and neither does a pool that reaches
two named globals: what the body operates on is exactly what the name would
have to say. A displacement into a struct the headers declare that matches no
field of it names nothing either, because the name would claim a member the
header does not. And where the object is established but the body does not
dereference the pointer, the name says the object and no verb, `<global>__on`,
which leaves an honest name rather than a guessed one.

The same resolution runs over every unnamed body and not only the leaves; the
leaves are simply where it answers most often, because a leaf's own body is
the whole of what is done with the pointer.
"""

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import callargs  # noqa: E402
import shapes  # noqa: E402

CLASS = "provenance"

# How far up the callers a `param` is followed. Two levels is what the helper
# nicknames are held to, and for the same reason: past it the chain says only
# that something under something passes a pointer along.
PARAM_DEPTH = 2

# The longest derived name that is still a name; abi/autonames.py's bound.
NAME_MAX = 56

CALLEE_SAVED = ("r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11")
WRITES = re.compile(r"^(r\d+),")
LOAD = ("ldr", "ldr.w", "ldrb", "ldrb.w", "ldrh", "ldrh.w",
        "ldrsb", "ldrsb.w", "ldrsh", "ldrsh.w", "ldrd")
STORE = ("str", "str.w", "strb", "strb.w", "strh", "strh.w", "strd")
BASE_OF = re.compile(r"\[(r\d+)")
# An instruction whose first operand is a register it reads rather than writes.
# Without this every `cmp r4, #1` and every `str r4, [r5]` reads as a write to
# r4, and no register in the image has exactly one writer.
NOT_A_WRITE = ("str", "str.w", "strb", "strb.w", "strh", "strh.w", "strd",
               "strex", "push", "push.w", "stm", "stm.w", "stmia", "stmdb",
               "cmp", "cmp.w", "cmn", "cmn.w", "tst", "tst.w", "teq",
               "vstr", "vstm", "vcmp", "vcmpe", "b", "b.w", "bx", "blx",
               "cbz", "cbnz", "tbb", "tbh")
MOV = re.compile(r"^(r\d+),\s*(r\d+)$")
ADD_IMM = re.compile(r"^(r\d+),\s*(r\d+),\s*#(?:0x)?[0-9a-fA-F]+$")


class Origin(object):
    """Where one argument of one call came from, as far as the caller says."""

    def __init__(self, kind, base=None, offset=0, index=None, value=None,
                 why=None):
        self.kind = kind          # global | field | param | literal | unknown
        self.base = base          # the global's address, for global and field
        self.offset = offset
        self.index = index        # the caller's argument index, for param
        self.value = value
        self.why = why

    def key(self):
        """What two call sites have to agree on for the body to be named."""
        if self.kind in ("global", "field"):
            return (self.kind, self.base, self.offset)
        if self.kind == "literal":
            return ("literal", self.value)
        return (self.kind,)

    def text(self, names):
        if self.kind in ("global", "field"):
            where = names.get(self.base, "0x%x" % self.base)
            at = "+0x%x" % self.offset if self.offset else ""
            return ("&%s%s" if self.kind == "global" else "%s%s")  % (where, at)
        if self.kind == "param":
            return "the caller's arg%d" % self.index
        if self.kind == "literal":
            return "0x%x" % self.value
        return self.why or "unknown"


def function_bases(img, ex, fn, entry_index):
    """What the registers hold throughout a function, where the body says so.

    Two readings, both about the whole function rather than one block. A
    callee-saved register written exactly once in the body holds that one
    value at every call in it, whatever the control flow between them; and an
    argument register the body never writes still holds the caller's argument.
    Together they are what turns `add r0, r7, #0x18` at a call in a loop into
    a displacement off a named global.
    """
    body = img.body(fn, fn + ex.size(fn))
    writers = collections.defaultdict(list)
    for i, (addr, mnem, ops) in enumerate(body):
        m = WRITES.match(ops)
        if m and mnem not in NOT_A_WRITE:
            writers[m.group(1)].append(i)
        # A `pop` is the epilogue restoring what the prologue saved, not a
        # value any call in the body is made with; counting it would leave
        # every callee-saved register of every function with two writers.
        if mnem in ("ldm", "ldm.w", "ldmia"):
            for reg in re.findall(r"r\d+", ops.split("{")[-1]):
                writers[reg].append(i)
    out = {}
    for reg in CALLEE_SAVED:
        at = writers.get(reg, ())
        if len(at) != 1:
            continue
        # The one write, read by the same forward interpretation the calling
        # sequence gets: a pool load, or an add on one.
        # One past the write, so the frame is the one the write lands in, and
        # seeded with the arguments where the run reaches the function's entry:
        # `mov r7, r0` in a prologue is the caller's first argument held for
        # the whole body.
        after = entry_index + at[0] + 1
        frame = callargs.resolve(img, img.insns, after,
                                 initial=_param_seed(img, ex, fn, entry_index, after))
        held = frame.regs.get(reg)
        if isinstance(held, (callargs.Pool, callargs.Imm)):
            out[reg] = held
        elif isinstance(held, callargs.Param):
            out[reg] = held
    for i, reg in enumerate(("r0", "r1", "r2", "r3")):
        if reg not in writers:
            out.setdefault(reg, callargs.Param(i))
    return out


# A branch inside the body, as the addresses something in it jumps to. A
# `tbb`/`tbh` jumps through a table this does not read, so a body with one is
# not widened at all.
BRANCH = ("b", "b.w", "beq", "bne", "bcs", "bcc", "bmi", "bpl", "bvs", "bvc",
          "bhi", "bls", "bge", "blt", "bgt", "ble", "cbz", "cbnz")
UNCONDITIONAL = ("b", "b.w", "bx", "bx.w", "pop", "pop.w")
TABLE_BRANCH = ("tbb", "tbh", "tbb.w", "tbh.w")


def extended_blocks(img, ex, fn, entry_index):
    """For each call in the body, how far back the straight line really runs.

    `callargs` stops at the last control transfer, which is the largest window
    that needs no reasoning. Most of this image's argument setup is further
    back than that: the pointer is built once and the call is two branches
    later. The window may be widened over an instruction as long as nothing
    branches to it -- then the only way to reach the call is through it -- and
    as long as no unconditional transfer stands in the way, which would make
    the fall-through unreachable. A `bl` stops the widening whatever else is
    true, because a call returns in r0 and clobbers r1 to r3.
    """
    body = img.body(fn, fn + ex.size(fn))
    lo, hi = fn, fn + ex.size(fn)
    if any(mnem in TABLE_BRANCH for _, mnem, _ in body):
        return {}
    targets = set()
    for _, mnem, ops in body:
        if mnem not in BRANCH:
            continue
        for text in re.findall(r"0x([0-9a-f]+)", ops.split("@")[0]):
            at = int(text, 16)
            if lo <= at < hi:
                targets.add(at)
    out = {}
    for i, (addr, mnem, _) in enumerate(body):
        if mnem not in ("bl", "bl.w"):
            continue
        j = i
        while j > 0:
            prev = body[j - 1]
            if prev[1] in ("bl", "bl.w", "blx") or prev[1] in UNCONDITIONAL:
                break
            if body[j][0] in targets:
                break
            j -= 1
        out[entry_index + i] = entry_index + j
    return out


def _sole_writers(img, ex, fn, entry_index):
    """{register: the one instruction index in the function that writes it}.

    A register with exactly one definition in the whole body has that value
    wherever it is read, whatever the control flow: a path that skipped the
    definition would read a register the function never set. The caller-saved
    registers are on the list too, held to the extra condition that no call
    stands between the definition and the read, because a call returns in r0
    and clobbers r1 to r3.
    """
    body = img.body(fn, fn + ex.size(fn))
    writers = collections.defaultdict(list)
    calls = []
    for i, (addr, mnem, ops) in enumerate(body):
        if mnem in ("bl", "bl.w", "blx"):
            calls.append(i)
            for reg in ("r0", "r1", "r2", "r3"):
                writers[reg].append(i)
        m = WRITES.match(ops)
        if m and mnem not in NOT_A_WRITE:
            writers[m.group(1)].append(i)
        if mnem in ("ldm", "ldm.w", "ldmia"):
            for reg in re.findall(r"r\d+", ops.split("{")[-1]):
                writers[reg].append(i)
    return dict((reg, at[0]) for reg, at in writers.items() if len(at) == 1)


def _hoisted(img, ex, fn, entry_index, call_index, reg, sole, bases):
    """The value a register holds at a call whose own block never wrote it."""
    at = sole.get(reg)
    if at is None or entry_index + at >= call_index:
        return None
    after = entry_index + at + 1
    initial = dict(bases)
    initial.update(_param_seed(img, ex, fn, entry_index, after))
    return callargs.resolve(img, img.insns, after, initial=initial).regs.get(reg)


def _param_seed(img, ex, fn, entry_index, at):
    """The argument registers, where the run reaches the function's entry."""
    if min(at, callargs.block_start(img.insns, at)) > entry_index:
        return {}
    return dict(("r%d" % i, callargs.Param(i)) for i in range(4))


def classify(value, globals_, extents):
    """One abstract value, as where it came from."""
    if isinstance(value, callargs.Param):
        return Origin("param", index=value.index)
    if isinstance(value, (callargs.Pool, callargs.Imm)):
        base, offset = _in_global(value.value, globals_, extents)
        if base is not None:
            return Origin("global", base=base, offset=offset)
        return Origin("literal", value=value.value)
    if isinstance(value, callargs.Field):
        if isinstance(value.base, str) and value.base.startswith("0x"):
            base, offset = _in_global(int(value.base, 16), globals_, extents)
            if base is not None:
                return Origin("field", base=base, offset=offset + value.offset)
        return Origin("unknown", why="a field of something unnamed")
    if isinstance(value, callargs.Table):
        return Origin("unknown", why="a row of a table at a computed index")
    return Origin("unknown", why=getattr(value, "why", "not modelled"))


def _in_global(address, globals_, extents):
    """(the global the address is in, the displacement into it), or (None, 0).

    A global with no measured extent answers only for its own first byte: a
    pointer a few words past it is as likely to be the next object as a field
    of this one, and the map says which only where it says how long the object
    is.
    """
    if address in globals_:
        return address, 0
    for base, size in extents:
        if base < address < base + size:
            return base, address - base
    return None, 0


def resolve_call(img, ex, fn, entry_index, call_index, globals_, extents,
                 bases, sole, blocks):
    """Every argument of one call, as an Origin."""
    start = blocks.get(call_index)
    initial = dict(bases)
    initial.update(_param_seed(img, ex, fn, entry_index,
                               start if start is not None else call_index))
    frame = callargs.resolve(img, img.insns, call_index, initial=initial,
                             start=start)
    out = []
    for i in range(4):
        value = frame.arg(i)
        if isinstance(value, callargs.Unknown) and "never written" in value.why:
            widened = _hoisted(img, ex, fn, entry_index, call_index,
                               "r%d" % i, sole, bases)
            value = widened if widened is not None else value
        out.append(classify(value, globals_, extents))
    return out


def _entry_index(img, fn):
    import bisect
    return bisect.bisect_left(img._insn_at, fn)


def argument_origins(img, ex, sites, globals_, extents, wanted):
    """{callee: {argument index: [(caller, call address, Origin)]}}.

    One pass over every call in the image whose target is a body the caller
    wants an answer about, so the per-function base reading is done once.
    """
    out = collections.defaultdict(lambda: collections.defaultdict(list))
    for fn in sorted(sites):
        calls = [(i, t) for i, t in sites[fn] if t in wanted]
        if not calls:
            continue
        entry = _entry_index(img, fn)
        bases = function_bases(img, ex, fn, entry)
        sole = _sole_writers(img, ex, fn, entry)
        blocks = extended_blocks(img, ex, fn, entry)
        for i, target in calls:
            args = resolve_call(img, ex, fn, entry, i, globals_, extents,
                                bases, sole, blocks)
            for n, origin in enumerate(args):
                out[target][n].append((fn, img.insns[i][0], origin))
    return out


def agreed(records, callers):
    """The one Origin every call site passes, or None.

    Every call into the body has to be accounted for: a body named for what
    three of its four callers hand it is named for three quarters of the truth.
    """
    if not records or {r[0] for r in records} != callers:
        return None
    keys = {r[2].key() for r in records}
    if len(keys) != 1:
        return None
    return records[0][2]


def follow(origins, callee, index, callers_of, depth):
    """A `param` answered by the callers of the caller, or the Origin itself."""
    seen = origins.get(callee, {}).get(index, [])
    here = agreed(seen, callers_of.get(callee, set()))
    if here is None or here.kind != "param" or depth <= 0:
        return here
    answers = set()
    resolved = None
    for caller, _, _ in seen:
        up = follow(origins, caller, here.index, callers_of, depth - 1)
        if up is None or up.kind == "param":
            return here
        answers.add(up.key())
        resolved = up
    return resolved if len(answers) == 1 else here


# What a body does with the pointer, in abi/ghidra/word_uses.py's vocabulary,
# and the verb the map already spells it with. `copy` is the one verb the map
# does not yet carry; the rest are its own (get 45, set 33, reset 25,
# update 16, check 13, read 12).
class Uses(object):
    """What a body does with one pointer, counted off its own instructions.

    abi/ghidra/word_uses.py's vocabulary, measured here rather than read from
    its export: word_uses.json publishes a use summary per literal-pool word
    and per memory cell, and a pointer a caller hands a body is neither. The
    names are its names -- a `load_base` is a read, a `store_base` a write --
    and the displacements are the `field+d:w` it records.
    """

    def __init__(self):
        self.reads = self.writes = 0
        self.zeroed = self.compared = self.copied = 0
        self.offsets = set()

    def verb(self):
        """The map's own spelling for what that adds up to, or None.

        The vocabulary is the map's: `get` names 45 entries, `set` 33,
        `reset` 25, `update` 16, `check` 13 and `read` 12. `copy` is the one
        this adds, for a body that hands the pointer to memcpy. A body that
        never dereferences the pointer has no verb, which is a refusal.
        """
        if self.copied:
            return "copy"
        if self.writes and not self.reads:
            return "reset" if self.zeroed == self.writes else "set"
        if self.reads and not self.writes:
            return "check" if self.compared else "get"
        if self.reads and self.writes:
            return "update"
        return None

    def field_offset(self):
        """The one displacement every access used, where there is one."""
        return self.offsets.pop() if len(self.offsets) == 1 else None


def uses_of(img, ex, fn, copiers, holds=(), seeds=None):
    """The `Uses` of a pointer a body holds, from `holds` or from `seeds`.

    The pointer is followed through `mov` chains and through displacements of
    it, because `adds r3, r0, #8` then `str r2, [r3]` is a write to the object
    exactly as much as `str r2, [r0, #8]` is, and the displacement is carried
    with it so the two report the same field.

    `holds` is the register the pointer arrives in -- r0 for an argument --
    and `seeds` is {instruction index: register} for a pointer the body loads
    itself, which is where a literal-pool word puts it.
    """
    body = img.body(fn, fn + ex.size(fn))
    seeds = seeds or {}
    at = dict((reg, 0) for reg in holds)
    out = Uses()
    zero = set()
    for i, (addr, mnem, ops) in enumerate(body):
        if i in seeds:
            at[seeds[i]] = 0
            continue
        if mnem in ("bl", "bl.w") and any(t in copiers for t in _targets(ops)):
            if "r0" in at:
                out.copied += 1
            continue
        if mnem in LOAD or mnem in STORE:
            base = BASE_OF.search(ops)
            if base and base.group(1) in at:
                field = re.search(r"\[r\d+,\s*#(-?(?:0x)?[0-9a-fA-F]+)\]", ops)
                delta = callargs._int(field.group(1)) if field else 0
                out.offsets.add(at[base.group(1)] + delta)
                if mnem in LOAD:
                    out.reads += 1
                else:
                    out.writes += 1
                    if ops.split(",")[0].strip() in zero:
                        out.zeroed += 1
        if mnem in ("cmp", "cmp.w", "cbz", "cbnz") and out.reads:
            out.compared += 1
        m = WRITES.match(ops)
        if not m or mnem in NOT_A_WRITE:
            continue
        dest = m.group(1)
        move, displaced = MOV.match(ops), ADD_IMM.match(ops)
        if mnem in ("mov", "movs") and move and move.group(2) in at:
            at[dest] = at[move.group(2)]
            continue
        if mnem.startswith("add") and displaced and displaced.group(2) in at:
            at[dest] = at[displaced.group(2)] + \
                callargs._int(ops.rsplit("#", 1)[1])
            continue
        at.pop(dest, None)
        zero.discard(dest)
        if mnem in ("mov", "movs", "movw") and re.match(r"^r\d+,\s*#0$", ops):
            zero.add(dest)
    return out


def pool_globals(img, ex, fn, names):
    """{global address: {instruction index: the register it was loaded into}}.

    A body reaches a global through a literal-pool word, and the word is in
    the body's own instruction stream, so which global it touches needs no
    caller at all. This is the other half of the same question the arguments
    answer, and it reaches the bodies that take no pointer.
    """
    out = collections.defaultdict(dict)
    for i, (addr, mnem, ops) in enumerate(img.body(fn, fn + ex.size(fn))):
        if not mnem.startswith("ldr") or "[pc" not in ops:
            continue
        pool = ex.pool_reads.get(addr)
        if pool is None:
            continue
        word = img.word(pool)
        if word in names:
            out[word][i] = ops.split(",")[0].strip()
    return out


def _targets(ops):
    return [int(m, 16) for m in re.findall(r"0x([0-9a-f]+)", ops.split("@")[0])]


def _from_argument(img, ex, fn, origins, callers_of, names, fields, copiers,
                   outcome, tag):
    """(base name, verb, evidence) from what every caller passes in r0."""
    origin = follow(origins, fn, 0, callers_of, PARAM_DEPTH)
    if origin is None:
        outcome[tag + ": call sites disagree or say nothing"] += 1
        return None
    outcome["%s: %s" % (tag, origin.kind)] += 1
    if origin.kind != "global":
        return None
    base = name_of(origin, fields, names)
    if base is None:
        outcome[tag + ": the displacement names no declared field"] += 1
        return None
    where = sorted(origins[fn][0], key=lambda r: r[1])
    evidence = ("every call into 0x%x passes %s in r0: %s"
                % (fn, origin.text(names),
                   ", ".join("0x%x from 0x%x" % (at, caller)
                             for caller, at, _ in where[:4])))
    return base, uses_of(img, ex, fn, copiers, holds=("r0",)).verb(), evidence


def _from_own_pool(img, ex, fn, names, fields, declared, copiers, outcome, tag):
    """(base name, verb, evidence) from the one named global the body reaches.

    Where a body's literal pool holds the address of exactly one global the
    map names, and the body dereferences it, the object the body is about is
    settled without asking any caller: the pool word is in the body itself.
    Two named globals is a refusal for the same reason two disagreeing call
    sites are -- the name would have to choose.
    """
    reached = pool_globals(img, ex, fn, names)
    if len(reached) != 1:
        outcome[tag + ": %s named global in its own pool"
                % ("no" if not reached else "more than one")] += 1
        return None
    base_address, seeds = next(iter(reached.items()))
    uses = uses_of(img, ex, fn, copiers, seeds=seeds)
    verb = uses.verb()
    if verb is None:
        outcome[tag + ": reaches one global and dereferences none of it"] += 1
        return None
    name = names[base_address]
    offset = uses.field_offset()
    if offset:
        field = fields.get((base_address, offset))
        # A header that declares the struct answers for every offset in it, so
        # one it does not declare is a claim the name may not make. A global
        # with no declared shape makes no such claim either way, and the name
        # says the object without saying which part of it.
        if field is None and base_address in declared:
            outcome[tag + ": the offset it accesses names no declared field"] += 1
            return None
        if field is not None:
            name = "%s_%s" % (name, field)
    return name, verb, ("0x%x reaches %s through its own literal pool and no"
                        " other named global" % (fn, names[base_address]))


def name_of(origin, fields, names):
    """`<global>` or `<global>_<field>`, or None where the offset names no field.

    A displacement into a declared struct that matches no field is not a field
    of it: the name would claim a member the header does not declare, so the
    body keeps its placeholder rather than take one.
    """
    base = names.get(origin.base)
    if base is None:
        return None
    if not origin.offset:
        return base
    field = fields.get((origin.base, origin.offset))
    if field is None:
        return None
    return "%s_%s" % (base, field)


def entries(img, ex, smap, types, sites, blocked=(), library=()):
    """Every unnamed body the callers' first argument names, as map records."""
    if not ex.ok:
        return [], collections.Counter()
    names = dict((s.address, s.name) for s in smap.of_kind("global", "table"))
    # The extents: a declared struct or table is as long as its declaration
    # says, and a `global` entry is as long as the measurement that wrote it
    # reached. Nothing else answers for an interior address.
    extents, fields, declared = [], {}, set()
    for region in types.typed_regions(smap):
        extents.append((region.address, region.stride * region.count))
        if region.count == 1:
            declared.add(region.address)
            for field in region.row.fields:
                fields[(region.address, field.offset)] = field.name
    for s in smap.of_kind("global", "table"):
        if s.size:
            extents.append((s.address, s.size))
    copiers = {s.address for s in smap.of_kind("function")
               if s.name in ("memcpy", "memmove", "__aeabi_memcpy",
                             "__aeabi_memcpy4", "__aeabi_memcpy8")}
    wanted = {fn for fn in ex.fns
              if ex.unnamed(fn) and fn not in blocked
              and not any(lo <= fn < hi for lo, hi in library)}
    origins = argument_origins(img, ex, sites, names, extents, wanted)
    callers_of = dict((fn, set(ex.callers.get(fn, ()))) for fn in wanted)
    outcome = collections.Counter()
    claimed, weak = {}, {}
    for fn in sorted(wanted):
        tag = "leaf" if not ex.callees.get(fn) else "body"
        reading = _from_argument(img, ex, fn, origins, callers_of, names,
                                 fields, copiers, outcome, tag)
        if reading is None:
            reading = _from_own_pool(img, ex, fn, names, fields, declared,
                                     copiers, outcome, tag)
        if reading is None:
            continue
        base, verb, evidence = reading
        if verb is None:
            weak[fn] = (base, evidence)
            continue
        outcome["named: " + verb] += 1
        claimed[fn] = ("%s_%s" % (base, verb), evidence +
                       "; the body %ss through it" % verb)
    for fn, (base, evidence) in weak.items():
        claimed[fn] = ("%s__on" % base, evidence + "; the body does not"
                       " dereference it, so the verb is not established")
        outcome["named: on (no verb)"] += 1
    # One name per address and one address per name: several bodies do the
    # same thing to the same object, and nothing here tells them apart, so
    # they are numbered by address the way the nicknames are.
    per = collections.defaultdict(list)
    for fn, (name, _) in claimed.items():
        per[name].append(fn)
    found = []
    for fn in sorted(claimed):
        name, evidence = claimed[fn]
        if len(per[name]) > 1:
            name = "%s_%d" % (name, sorted(per[name]).index(fn) + 1)
        if len(name) > NAME_MAX:
            outcome["refused: the name would be longer than a name"] += 1
            continue
        found.append({"address": fn, "name": name, "class": CLASS,
                      "evidence": evidence})
    return found, outcome


def main():
    import autonames
    import symbols as symmap

    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.path.join(autonames.SIM, "appl.bin"))
    ap.add_argument("--dis", default=os.path.join(autonames.SIM, "out", "appl.dis"))
    ap.add_argument("--export", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "out", "ghidra"))
    args = ap.parse_args()
    img = autonames.Image(args.image, args.dis)
    want = set(autonames.DEFAULT_CLASSES.split(","))
    ex = autonames.Export(args.export, autonames.prior_addresses(args.export, want))
    smap = symmap.load()
    # Standalone, the question is what the rule would find over the bodies
    # nothing names at all, so every address the map has a name for is out.
    held = {s.address for s in smap.of_kind("function")}
    found, outcome = entries(img, ex, smap, shapes.load(),
                             autonames.call_sites(img, ex), blocked=held,
                             library=autonames.library_ranges())
    print("%d names from the first argument's provenance" % len(found))
    for key, n in sorted(outcome.items()):
        print("  %-52s %5d" % (key, n))
    for e in found[:10]:
        print("  0x%05x %-40s %s" % (e["address"], e["name"], e["evidence"][:90]))


if __name__ == "__main__":
    main()
